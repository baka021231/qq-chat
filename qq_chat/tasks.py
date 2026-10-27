"""Durable background tasks; only confirmed outcomes release execution capacity."""
import asyncio
from collections import defaultdict, deque
from dataclasses import asdict
import contextlib
import os
import json
import logging
from pathlib import Path
import time

from .agents.base import TASK, FULL, TurnRequest, Interrupted, ExecutionUncertain
from . import prompts

LIVE = ('queued', 'running', 'stopping', 'orphaned')
TERMINAL = ('done', 'failed', 'stopped', 'interrupted')
LABELS = {'done': '完成', 'failed': '失败', 'stopped': '已停止',
          'interrupted': '已中断', 'orphaned': '执行状态待确认', 'queued': '排队中',
          'running': '运行中', 'stopping': '等待停止确认'}


class TaskManager:
    def __init__(self, bridge, root):
        self.bridge, self.db, self.root = bridge, bridge.db, Path(root)
        self.back = bridge.cfg.get('agents', {}).get('back', [])
        self.config = bridge.cfg.get('tasks', {})
        self.runners, self.watchers = {}, set()
        self.progress = defaultdict(lambda: deque(maxlen=50))
        self.condition = asyncio.Condition()
        self.db.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
            source TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT, runtime_ref TEXT,
            writes INTEGER NOT NULL DEFAULT 1,
            goal TEXT NOT NULL, status TEXT NOT NULL, created REAL, started REAL, finished REAL,
            result TEXT, error TEXT, report_text TEXT, reported INTEGER DEFAULT 0)''')
        if 'writes' not in {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute('ALTER TABLE tasks ADD COLUMN writes INTEGER NOT NULL DEFAULT 1')
        if 'receipt_sent' not in {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute('ALTER TABLE tasks ADD COLUMN receipt_sent INTEGER NOT NULL DEFAULT 1')
        self.db.execute('CREATE TABLE IF NOT EXISTS task_inputs (id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL, text TEXT NOT NULL, consumed INTEGER DEFAULT 0)')
        self.db.commit()

    def get(self, ident):
        cur = self.db.execute('SELECT * FROM tasks WHERE id=?', (ident,))
        row = cur.fetchone()
        return dict(zip((c[0] for c in cur.description), row)) if row else None

    def _set(self, ident, **values):
        self.db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                        (*values.values(), ident))
        self.db.commit()

    def list(self, conversation, *, live=False):
        cur = self.db.execute('SELECT id FROM tasks WHERE conversation=? ORDER BY id DESC', (conversation,))
        rows = [self.get(row[0]) for row in cur.fetchall()]
        return [r for r in rows if r['status'] in LIVE] if live else rows[:10]

    def status(self, conversation, ident):
        row = self.get(ident)
        if not row or row['conversation'] != conversation:
            return None
        return {**row, 'progress': list(self.progress[ident])[-10:]}

    def writes(self, name):
        # Unknown profiles from older configurations fail closed.
        return self.bridge.cfg.get('agents', {}).get('profiles', {}).get(name, {}).get('writes', True)

    def _occupied(self, name):
        rows = self.db.execute("SELECT agent,writes FROM tasks WHERE status IN ('running','stopping','orphaned')").fetchall()
        return (any(n == name for n, _ in rows) or
                (self.writes(name) and any(writes for _, writes in rows)))

    async def dispatch(self, conversation, source, goal, agent=None):
        from .bridge import owner_request
        if not owner_request(source, self.bridge.cfg['owner_qq']) or conversation != self.bridge.conversation(source):
            raise ValueError('任务操作仅限当前主人会话')
        name = agent or (self.back[0] if self.back else None)
        if name not in self.back:
            raise ValueError('当前未配置该后台；请先配置 agents.back 并重启')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 6000:
            raise ValueError('任务目标应为 1～6000 字')
        count = self.db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running','stopping','orphaned')").fetchone()[0]
        if count >= self.config.get('max_running', 4) or len(self.list(conversation, live=True)) >= self.config.get('max_per_conversation', 3):
            raise ValueError('任务数量已达上限，请等现有任务结束')
        cur = self.db.execute('INSERT INTO tasks(conversation,source,agent,goal,status,created,writes,receipt_sent) VALUES(?,?,?,?,?,?,?,0)',
                              (conversation, json.dumps(asdict(source), ensure_ascii=False), name, goal.strip(), 'queued', time.time(), int(self.writes(name))))
        self.db.commit()
        ident = cur.lastrowid
        runner = asyncio.create_task(self._run(ident))
        self.runners[ident] = runner
        runner.add_done_callback(lambda t: self.runners.pop(ident, None))
        logging.info('Task created id=%s agent=%s', ident, name)
        return {'id': ident, 'agent': name, 'status': 'queued'}

    def _background(self, coro):
        task = asyncio.create_task(coro)
        self.watchers.add(task)
        task.add_done_callback(self.watchers.discard)
        return task

    def _progress(self, ident, text):
        self.progress[ident].append(text[:1200])

    async def _relay_progress(self, ident, source):
        # Keep backend callbacks synchronous; at most one sender per task.
        sent = None
        while True:
            await asyncio.sleep(3)
            text = self.progress[ident][-1] if self.progress[ident] else None
            if text and text != sent:
                sent = text
                with contextlib.suppress(Exception):
                    await self.bridge.send(source, f'【#{ident} 进度】\n{text}')

    async def _run(self, ident):
        from .bridge import Source
        row = self.get(ident)
        source = Source(**json.loads(row['source']))
        execution, progress_sender = None, None
        try:
            async with self.condition:
                await self.condition.wait_for(lambda: self.get(ident)['status'] != 'queued' or not self._occupied(row['agent']))
                if self.get(ident)['status'] != 'queued':
                    return
                self._set(ident, status='running', started=time.time())
            agent = self.bridge.agents[row['agent']]
            profile = getattr(agent, 'profile', {})
            req = TurnRequest(f'task:{ident}', row['goal'], TASK, FULL, prompts.WORKER,
                              model=profile.get('model'), effort=profile.get('effort'),
                              on_progress=lambda text: self._progress(ident, text),
                              on_runtime=lambda ref: self._set(ident, runtime_ref=json.dumps(ref)))
            req.session_id = await agent.prepare(req)
            self._set(ident, session_id=req.session_id)
            if self.config.get('progress', 'direct') == 'direct':
                progress_sender = asyncio.create_task(self._relay_progress(ident, source))
            deadline = time.monotonic() + 60 * self.config.get('timeout_minutes', 60)
            results = []
            while True:
                execution = asyncio.create_task(agent.run(req))
                done, _ = await asyncio.wait({execution}, timeout=max(0, deadline - time.monotonic()))
                if not done:
                    self._set(ident, error='任务超时，已请求停止；未确认结束前仍占用执行锁')
                    outcome = await self.stop(row['conversation'], ident)
                    notice = '停止请求失败，执行锁保留，请重试 /stop。' if outcome == 'failed' else '已请求停止，等待执行端确认。'
                    with contextlib.suppress(Exception):
                        await self.bridge.send(source, f'【任务#{ident} 超时】{notice}')
                results.append(await execution)
                pending = self.db.execute('SELECT id,text FROM task_inputs WHERE task=? AND consumed=0 ORDER BY id', (ident,)).fetchall()
                if not pending or self.get(ident)['status'] == 'stopping' or time.monotonic() >= deadline:
                    break
                req.text = '主人追加要求（按顺序）：\n' + '\n'.join(text for _, text in pending)
                self.db.executemany('UPDATE task_inputs SET consumed=1 WHERE id=?', [(i,) for i, _ in pending])
                self.db.commit()
            result = '\n\n'.join(results)[-12000:]
            if pending:
                result += '\n未执行的追加要求：' + str(len(pending)) + ' 条（停止或超时后不再启动下一轮）。'
            self._set(ident, status='done', result=result, finished=time.time())

        except Interrupted as error:
            previous = self.get(ident)['error'] or ''
            self._set(ident, status='stopped', error=(previous + '\n' + str(error)).strip(), finished=time.time())
        except (ExecutionUncertain, asyncio.CancelledError) as error:
            if self.get(ident)['status'] == 'queued':
                self._set(ident, status='interrupted', finished=time.time(), error='执行开始前调度已取消')
                raise
            # Cancellation/disconnection is not proof that a remote writer ended.
            self._set(ident, status='orphaned', error=type(error).__name__ + '：执行结束未确认，需人工 release')
            if execution is not None:
                execution.cancel()
                with contextlib.suppress(BaseException):
                    await execution
            if isinstance(error, asyncio.CancelledError):
                raise
        except Exception as error:
            self._set(ident, status='failed', error=str(error)[:4000], finished=time.time())
        finally:
            if progress_sender:
                progress_sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_sender
            async with self.condition:
                self.condition.notify_all()
            try:
                self._write_report(ident)
            except OSError:
                logging.warning('Task report file pending id=%s', ident)
            await self.bridge.report_task(ident)

    def _write_report(self, ident):
        row = self.get(ident)
        folder = self.root / 'workspace/tasks'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink():
            raise OSError('Task reports directory must not be a symlink')
        folder.chmod(0o700)
        path = folder / f'{ident}.md'
        # O_NOFOLLOW prevents a task from redirecting the bridge report write.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(f"# 任务 {ident} · {LABELS[row['status']]}\n\n{row['goal']}\n\n{row['result'] or row['error'] or ''}\n")
            pending = self.db.execute('SELECT count(*) FROM task_inputs WHERE task=? AND consumed=0', (ident,)).fetchone()[0]
            if pending:
                out.write(f'\n尚未执行的追加要求：{pending} 条，不会自动恢复运行。\n')

    async def stop(self, conversation, ident):
        row = self.status(conversation, ident)
        if not row:
            return 'not_found'
        if row['status'] == 'queued':
            self._set(ident, status='interrupted', error='开始执行前取消', finished=time.time())
            async with self.condition:
                self.condition.notify_all()
            self._background(self.bridge.report_task(ident))
            return 'requested'
        if row['status'] not in ('running', 'stopping'):
            return 'not_running'
        if row['status'] == 'stopping':
            return 'requested'
        self._set(ident, status='stopping')
        self._background(self._stop_watchdog(ident))
        try:
            delivered = await self.bridge.agents[row['agent']].interrupt(f'task:{ident}')
            if not delivered:
                raise RuntimeError('No active execution')
        except Exception:
            if self.get(ident)['status'] == 'stopping':
                self._set(ident, status='running')
            return 'failed'
        return 'requested'

    async def _stop_watchdog(self, ident):
        await asyncio.sleep(30)
        row = self.get(ident)
        if row['status'] == 'stopping':
            from .bridge import Source
            with contextlib.suppress(Exception):
                await self.bridge.send(Source(**json.loads(row['source'])), f'【任务#{ident}】停止尚未确认，继续等待；执行锁仍保留。')

    def send_message(self, conversation, ident, text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 6000:
            raise ValueError('追加要求应为 1～6000 字')
        row = self.status(conversation, ident)
        if not row or row['status'] != 'running':
            raise ValueError('当前会话没有这个运行中的任务')
        count = self.db.execute('SELECT count(*) FROM task_inputs WHERE task=? AND consumed=0', (ident,)).fetchone()[0]
        if count >= 10:
            raise ValueError('追加要求已达上限，请等待下一轮')
        self.db.execute('INSERT INTO task_inputs(task,text) VALUES(?,?)', (ident, text))
        self.db.commit()
        return {'id': ident, 'status': 'queued_for_next_turn'}

    async def check(self, conversation, ident):
        row = self.status(conversation, ident)
        if not row:
            raise ValueError('当前会话没有该任务')
        agent = self.bridge.agents.get(row['agent'])
        check = getattr(agent, 'check_execution', None)
        if not check or not row['session_id']:
            return 'unknown'
        try:
            ref = json.loads(row['runtime_ref']) if row['runtime_ref'] else None
            return await asyncio.wait_for(check(row['session_id'], ref), 10)
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
            return 'unknown'

    async def release(self, conversation, ident):
        row = self.status(conversation, ident)
        if not row or row['status'] != 'orphaned':
            raise ValueError('当前会话没有这个待确认任务')
        self._set(ident, status='stopped', finished=time.time(), error='主人确认已结束，人工释放执行锁',
                  report_text=None, reported=0)
        async with self.condition:
            self.condition.notify_all()
        self._write_report(ident)
        self._background(self.bridge.report_task(ident))

    async def recover_after_restart(self):
        # Deliberately no process queries, signals, automatic reconciliation or polling.
        self.db.execute("UPDATE tasks SET status='interrupted',finished=?,error='重启前尚未开始执行' WHERE status='queued'", (time.time(),))
        self.db.execute("UPDATE tasks SET status='orphaned',error='重启前执行未结算；请确认结束后使用 /task release 编号' WHERE status IN ('running','stopping')")
        self.db.commit()
        for (ident,) in self.db.execute("SELECT id FROM tasks WHERE reported=0 AND status IN ('done','failed','stopped','interrupted','orphaned')").fetchall():
            try:
                self._write_report(ident)
            except OSError:
                logging.warning('Recovered report file unavailable id=%s', ident)

    async def close(self):
        for task in [*self.runners.values(), *self.watchers]:
            task.cancel()
        await asyncio.gather(*self.runners.values(), *self.watchers, return_exceptions=True)

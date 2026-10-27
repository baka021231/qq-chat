"""QQ task commands and durable delivery, separate from execution scheduling."""
import asyncio
from collections import defaultdict
import contextlib
from contextlib import asynccontextmanager
from functools import wraps
import json
import logging
import time

from . import prompts
from .agents.base import CHAT, NONE, Interrupted
from .tasks import LABELS, TERMINAL


def serialized(method):
    @wraps(method)
    async def wrapped(self, source, *args, **kwargs):
        async with self.conversation_lock(source):
            return await method(self, source, *args, **kwargs)
    return wrapped


class TaskCommands:
    @asynccontextmanager
    async def conversation_lock(self, source):
        key = self.conversation(source)
        locks = self.__dict__.setdefault('locks', defaultdict(asyncio.Lock))
        owners = self.__dict__.setdefault('_lock_owners', {})
        task = asyncio.current_task()
        if owners.get(key) is task:
            yield
            return
        async with locks[key]:
            owners[key] = task
            try:
                yield
            finally:
                owners.pop(key, None)

    @asynccontextmanager
    async def front_execution(self, source, agent):
        key = self.conversation(source)
        active = self.__dict__.setdefault('_active_front', {})
        tokens = self.__dict__.setdefault('_front_tokens', {})
        stops = self.__dict__.setdefault('_front_stops', {})
        token = object()
        active[key], tokens[key] = agent, token
        try:
            yield
        except Interrupted:
            with contextlib.suppress(Exception):
                await self.send(source, '当前前台轮次已确认停止。')
            raise
        except Exception:
            if stops.get(key) is token:
                with contextlib.suppress(Exception):
                    await self.send(source, '当前前台轮次停止前已失败。')
            raise
        else:
            if stops.get(key) is token:
                with contextlib.suppress(Exception):
                    await self.send(source, '当前前台轮次停止前已完成。')
        finally:
            active.pop(key, None)
            tokens.pop(key, None)
            stops.pop(key, None)

    async def front_stop_watchdog(self, source, token):
        await asyncio.sleep(30)
        if self.__dict__.get('_front_stops', {}).get(self.conversation(source)) is token:
            with contextlib.suppress(Exception):
                await self.send(source, '前台停止尚未确认，仍在等待执行端；当前会话轮次尚未释放。')

    async def deliver(self, source, text, user_text, *, streamed=False):
        if not streamed:
            await self.send(source, text)
        await self.enqueue_voice(source, text)
        key = self.conversation(source)
        saved = self.__dict__.setdefault('_history_replies', {}).pop(key, text)
        self.db.executemany('INSERT INTO history(role,content,conversation) VALUES(?,?,?)',
                            [('user', user_text, key), ('assistant', saved, key)])
        self.db.commit()

    def report_input(self, row):
        duration = max(0, int((row['finished'] or time.time()) - (row['started'] or row['created'])))
        body = (row['result'] or row['error'] or '')[:self.cfg.get('tasks', {}).get('report_chars', 4000)]
        return (f"[系统：后台任务回报 #{row['id']} | agent：{row['agent']} | 状态：{LABELS[row['status']]} | 用时：{duration} 秒]\n"
                f"任务：{row['goal'][:200]}\n报告（节选，全文在 workspace/tasks/{row['id']}.md）：\n{body}")

    async def report_task(self, ident):
        from .bridge import Source
        report_locks = self.__dict__.setdefault('_report_locks', defaultdict(asyncio.Lock))
        async with report_locks[ident]:
            row = self.tasks.get(ident)
            if not row or row['reported'] or row['status'] not in (*TERMINAL, 'orphaned'):
                return
            source = Source(**json.loads(row['source']))
            async with self.conversation_lock(source):
                row = self.tasks.get(ident)
                raw = self.report_input(row)
                try:
                    if not row['report_text']:
                        if row['status'] == 'orphaned':
                            # Recovery warnings must be deterministic and never minimized by a model.
                            text = (f"【任务#{ident} 执行状态待确认】重启或连接中断前的执行未结算。"
                                    + ('新的写任务会排队。' if row['writes'] else '')
                                    + f'请先人工确认已结束，再用 /task release {ident} 释放。')
                        else:
                            try:
                                request = await self.turn_request(source, True)
                                request.level, request.role = NONE, CHAT
                                request.instructions += '\n' + prompts.TASK_REPORT
                                agent = self.agent_for(source)
                                request.session_id = await agent.prepare(request)
                                header, _, body = raw.partition('\n')
                                request.text = header + '\n' + self.turn_text(source, request.session_id, body, None)
                                # No callbacks: report_text must be durable before the first QQ send.
                                async with self.front_execution(source, agent):
                                    text = await agent.run(request)
                                self.db.execute('INSERT OR IGNORE INTO bootstrap VALUES (?)', (request.session_id,))
                                self.db.commit()
                            except Exception:
                                text = f"【任务#{ident} {LABELS[row['status']]}】\n" + (row['result'] or row['error'] or '没有额外报告')[:1500]
                        self.tasks._set(ident, report_text=text)
                    else:
                        text = row['report_text']
                    # Discard stale full-text side channels from an interrupted normal chat.
                    self.__dict__.setdefault('_history_replies', {}).pop(self.conversation(source), None)
                    await self.deliver(source, text, raw)
                    self.tasks._set(ident, reported=1)
                except Exception:
                    logging.warning('Task report pending id=%s', ident)

    async def flush_task_reports(self):
        rows = self.db.execute("SELECT id FROM tasks WHERE reported=0 AND status IN ('done','failed','stopped','interrupted','orphaned') ORDER BY id").fetchall()
        # A stuck report in one conversation must not block delivery to another.
        jobs = [asyncio.create_task(self.report_task(ident)) for (ident,) in rows]
        try:
            await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def task_command(self, source, text):
        from .bridge import owner_request
        if not owner_request(source, self.cfg['owner_qq']):
            return
        parts = text.split(maxsplit=2)
        try:
            if len(parts) >= 2 and parts[1] == 'release':
                if len(parts) != 3 or not parts[2].isdigit():
                    raise ValueError('用法：/task release 编号；仅在你确认旧任务已结束后使用')
                await self.tasks.release(self.conversation(source), int(parts[2]))
                await self.send(source, '已按主人确认释放任务锁。')
            else:
                goal = text.split(maxsplit=1)[1].strip() if len(parts) > 1 else ''
                if not goal:
                    raise ValueError('用法：/task 完整目标；后台看不到聊天记录，请把要求写全')
                task = await self.tasks.dispatch(self.conversation(source), source, goal)
                await self.send(source, f"【已下发任务 #{task['id']} · {task['agent']}】{goal[:200]}")
        except ValueError as error:
            await self.send(source, str(error))

    async def fast_command(self, source, text):
        from .bridge import owner_request
        if not owner_request(source, self.cfg['owner_qq']):
            return
        try:
            parts = text.split()
            head = parts[0].casefold()
            key = self.conversation(source)
            if head == '/tasks':
                if len(parts) != 1:
                    await self.send(source, '用法：/tasks')
                    return
                rows = self.tasks.list(key)
                await self.send(source, '\n'.join(f"#{r['id']} · {r['agent']} · {LABELS[r['status']]} · {r['goal'][:100]}" for r in rows) or '当前会话还没有后台任务。')
                return
            if len(parts) == 1:
                active = self.__dict__.setdefault('_active_front', {}).get(key)
                if active is None:
                    await self.send(source, '当前会话没有正在执行的前台轮次。')
                    return
                token = self._front_tokens[key]
                self.__dict__.setdefault('_front_stops', {})[key] = token
                self.tasks._background(self.front_stop_watchdog(source, token))
                await self.send(source, '已请求停止当前前台轮次，等待执行端确认。')
                try:
                    delivered = await active.interrupt(key)
                except Exception:
                    delivered = False
                if not delivered and self._front_stops.get(key) is token:
                    self._front_stops.pop(key, None)
                    await self.send(source, '停止失败：执行端未接收请求，可能已经结束或尚未开始。')
                return
            if len(parts) != 2 or (parts[1] != 'all' and not parts[1].isdigit()):
                await self.send(source, '用法：/stop、/stop 编号 或 /stop all')
                return
            ids = ([r['id'] for r in self.tasks.list(key, live=True) if r['status'] != 'orphaned']
                   if parts[1] == 'all' else [int(parts[1])])
            if not ids:
                await self.send(source, '当前会话没有可停止的后台任务。')
                return
            cancelled = set()
            for ident in ids:
                row = self.tasks.status(key, ident)
                if row and row['status'] == 'queued':
                    await self.tasks.stop(key, ident)
                    cancelled.add(ident)
            await self.send(source, '已请求停止后台任务，等待执行端确认。')
            for ident in ids:
                if ident in cancelled:
                    continue
                outcome = await self.tasks.stop(key, ident)
                if outcome != 'requested':
                    labels = {'failed': '停止失败', 'not_found': '当前会话没有该任务', 'not_running': '任务当前不可停止'}
                    await self.send(source, f'【任务#{ident}】{labels[outcome]}。')
        except Exception:
            logging.warning('Fast task command failed')
            with contextlib.suppress(Exception):
                await self.send(source, '任务管理操作未完成，请稍后重试。')

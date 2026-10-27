"""Task execution, recovery and delivery using controlled agents, no network."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.agents.base import CHAT, TASK, NONE, FULL, Interrupted, ExecutionUncertain
from qq_chat.agents.config import settings
from qq_chat.bridge import Bridge, Incoming, Source
from tests.test_agents import FakeAgent


class Backend(FakeAgent):
    def __init__(self):
        super().__init__('backend result')
        self.release.clear()
        self.interrupt_mode = 'stopped'
        self.interrupts = []

    async def interrupt(self, key):
        self.interrupts.append(key)
        if self.interrupt_mode == 'failed':
            raise RuntimeError('RPC failed')
        if self.interrupt_mode == 'waiting':
            return True
        if self.interrupt_mode == 'stopped':
            self.error = Interrupted('confirmed')
        elif self.interrupt_mode == 'failed_outcome':
            self.error = RuntimeError('execution failed')
        self.release.set()
        return True


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'workspace').mkdir()
        (self.root / 'workspace/SOUL.md').write_text('persona')
        (self.root / 'workspace/OWNER.md').write_text('owner private data')
        self.patcher = patch('qq_chat.bridge.BASE', self.root)
        self.patcher.start()
        self.cfg = {'owner_qq': 10001, 'agents': {'front': 'front', 'guest': 'front',
                    'back': ['work'], 'profiles': {
                        'front': {'type': 'codex_app', 'path': 'unused'},
                        'work': {'type': 'codex_app', 'path': 'unused', 'writes': True}}},
                    'tasks': {'progress': 'silent'}}
        self.bridge = Bridge(self.cfg)
        self.front, self.back = FakeAgent('front reply'), Backend()
        self.front.name, self.back.name = 'front', 'work'
        self.front.profile = self.cfg['agents']['profiles']['front']
        self.back.profile = self.cfg['agents']['profiles']['work']
        self.bridge.agents.update(front=self.front, work=self.back)
        self.owner = Source('private', 10001, 10001)
        self.group = Source('group', 20001, 10001)
        self.guest = Source('group', 20001, 30001, '主人')
        self.sent, self.voices = [], []
        self.fail_send = False

        async def send(source, text):
            if self.fail_send:
                raise RuntimeError('offline')
            self.sent.append((source, text))

        async def voice(source, text):
            self.voices.append((source, text))

        self.bridge.send, self.bridge.enqueue_voice = send, voice

    async def asyncTearDown(self):
        self.front.release.set()
        self.back.release.set()
        await self.bridge.tasks.close()
        self.bridge.db.close()
        self.patcher.stop()
        self.folder.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(.001)

    async def create(self, goal='repair code', source=None):
        source = source or self.owner
        return (await self.bridge.tasks.dispatch(self.bridge.conversation(source), source, goal))['id']

    def row(self, ident):
        return self.bridge.tasks.get(ident)

    async def worker_command(self, text, source=None):
        job = asyncio.create_task(self.bridge.worker())
        try:
            await self.bridge.queue.put(Incoming(source or self.owner, text))
            await asyncio.wait_for(self.bridge.queue.join(), 2)
        finally:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job

    async def test_front_does_not_wait_for_background_and_roles_are_separate(self):
        ident = await self.create()
        await self.until(lambda: self.back.requests)
        await self.bridge.chat(self.owner, 'chat now')
        self.assertEqual(self.row(ident)['status'], 'running')
        req = self.back.requests[0]
        self.assertEqual((req.role, req.level, req.key), (TASK, FULL, f'task:{ident}'))
        self.assertNotIn('owner private data', req.instructions)
        self.assertNotIn('persona', req.instructions)
        self.assertIsNone(req.on_answer)
        front = self.front.requests[-1]
        self.assertEqual(front.level, NONE)
        self.assertIn('需要操作电脑的事，请主人用 /task 目标 交给后台', front.instructions)
        self.assertIn('owner private data', front.instructions)

    async def test_same_conversation_front_turns_are_serial(self):
        self.front.release.clear()
        first = asyncio.create_task(self.bridge.chat(self.owner, 'first'))
        await self.front.entered.wait()
        second = asyncio.create_task(self.bridge.chat(self.owner, 'second'))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(len(self.front.requests), 1)
            self.front.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            self.assertEqual(len(self.front.requests), 2)
        finally:
            first.cancel(); second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_cancel_worker_while_waiting_for_report_lock_does_not_leak_queue_count(self):
        started = asyncio.Event()
        async def hold():
            async with self.bridge.conversation_lock(self.owner):
                started.set()
                await asyncio.Future()
        holder = asyncio.create_task(hold())
        await started.wait()
        worker = asyncio.create_task(self.bridge.worker())
        await self.bridge.queue.put(Incoming(self.owner, '/new'))
        await self.until(lambda: self.bridge.queue.empty())
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        try:
            await asyncio.wait_for(self.bridge.queue.join(), 1)
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    async def test_writers_wait_and_limits_reject_instead_of_growing_queue(self):
        one = await self.create()
        await self.until(lambda: self.back.requests)
        two = await self.create('second')
        await asyncio.sleep(.01)
        self.assertEqual(self.row(two)['status'], 'queued')
        await self.create('third')
        with self.assertRaisesRegex(ValueError, '上限'):
            await self.create('fourth')
        self.back.release.set()
        await self.until(lambda: self.row(two)['reported'])
        self.assertEqual(self.row(one)['status'], 'done')

    async def test_dispatch_enforces_verified_owner_and_current_conversation(self):
        for source, conversation in ((self.guest, 'group:20001'), (self.owner, 'group:20001:owner')):
            with self.assertRaises(ValueError):
                await self.bridge.tasks.dispatch(conversation, source, 'goal')
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    async def test_recovery_does_not_inspect_or_interrupt_old_executions(self):
        ids = []
        for state in ('queued', 'running', 'stopping'):
            ident = await self.create(state)
            self.bridge.tasks._set(ident, status=state)
            ids.append(ident)
        # No await since create: scheduled runners have not begun. Simulate a new manager.
        for runner in self.bridge.tasks.runners.values():
            runner.cancel()
        await asyncio.gather(*self.bridge.tasks.runners.values(), return_exceptions=True)
        await self.bridge.tasks.recover_after_restart()
        self.assertEqual([self.row(i)['status'] for i in ids], ['interrupted', 'orphaned', 'orphaned'])
        self.assertEqual(self.back.interrupts, [])
        await self.bridge.flush_task_reports()
        self.assertTrue(all(self.row(i)['reported'] for i in ids))
        self.assertTrue(any('新的写任务会排队' in text for _, text in self.sent))
        await self.bridge.tasks.recover_after_restart()
        self.assertEqual(self.back.interrupts, [])

    async def test_orphan_holds_global_write_lock_across_profiles_until_release(self):
        one = await self.create()
        await self.until(lambda: self.back.requests)
        self.bridge.tasks._set(one, status='orphaned')
        # A different writing profile must be blocked too, even if the old profile changes.
        other = Backend(); other.name = 'other'; other.profile = {'writes': True}
        self.bridge.agents['other'] = other
        self.cfg['agents']['profiles']['other'] = {'writes': True}
        self.bridge.tasks.back.append('other')
        self.cfg['agents']['profiles']['work']['writes'] = False
        two = (await self.bridge.tasks.dispatch(self.bridge.conversation(self.owner), self.owner, 'second', 'other'))['id']
        await asyncio.sleep(.01)
        self.assertEqual(self.row(two)['status'], 'queued')
        self.assertFalse(other.requests)
        # Releasing is a human assertion; model/other conversations cannot perform it.
        with self.assertRaises(ValueError):
            await self.bridge.tasks.release(self.bridge.conversation(self.group), one)
        # Simulate the old execution having ended outside this process.
        self.back.release.set()
        await self.until(lambda: self.row(one)['status'] == 'done')
        self.bridge.tasks._set(one, status='orphaned')
        await self.bridge.tasks.release(self.bridge.conversation(self.owner), one)
        await self.until(lambda: other.requests)
        other.release.set()
        await self.until(lambda: self.row(two)['reported'])

    async def test_readonly_can_run_while_old_writer_is_orphaned(self):
        one = await self.create()
        self.bridge.tasks._set(one, status='orphaned')
        other = Backend(); other.name = 'readonly'; other.profile = {'writes': False}
        other.release.set()
        self.bridge.agents['readonly'] = other
        self.cfg['agents']['profiles']['readonly'] = {'writes': False}
        self.bridge.tasks.back.append('readonly')
        two = (await self.bridge.tasks.dispatch(self.bridge.conversation(self.owner), self.owner, 'read', 'readonly'))['id']
        await self.until(lambda: self.row(two)['reported'])
        self.assertEqual(self.row(one)['status'], 'orphaned')
        self.assertEqual(self.row(two)['status'], 'done')

    async def test_stop_queued_never_executes_and_cannot_cross_conversations(self):
        one = await self.create()
        await self.until(lambda: self.back.requests)
        two = await self.create('queued')
        self.assertEqual(await self.bridge.tasks.stop(self.bridge.conversation(self.group), one), 'not_found')
        self.assertEqual(await self.bridge.tasks.stop(self.bridge.conversation(self.owner), two), 'requested')
        await self.until(lambda: self.row(two)['reported'])
        self.assertEqual(self.row(two)['status'], 'interrupted')
        self.assertEqual(len(self.back.requests), 1)

    async def test_stop_outcomes_follow_execution_not_interrupt_ack(self):
        for mode, status in (('stopped', 'stopped'), ('completed', 'done'), ('failed_outcome', 'failed')):
            with self.subTest(mode=mode):
                self.back.entered.clear(); self.back.release.clear(); self.back.error = None
                self.back.interrupt_mode = mode
                ident = await self.create(mode)
                await self.back.entered.wait()
                self.assertEqual(await self.bridge.tasks.stop(self.bridge.conversation(self.owner), ident), 'requested')
                await self.until(lambda: self.row(ident)['reported'])
                self.assertEqual(self.row(ident)['status'], status)

    async def test_stop_rpc_failure_restores_running_and_wait_keeps_lock(self):
        ident = await self.create()
        await self.until(lambda: self.back.requests)
        self.back.interrupt_mode = 'failed'
        self.assertEqual(await self.bridge.tasks.stop(self.bridge.conversation(self.owner), ident), 'failed')
        self.assertEqual(self.row(ident)['status'], 'running')
        self.back.interrupt_mode = 'waiting'
        self.assertEqual(await self.bridge.tasks.stop(self.bridge.conversation(self.owner), ident), 'requested')
        self.assertEqual(self.row(ident)['status'], 'stopping')
        two = await self.create('next writer')
        await asyncio.sleep(.01)
        self.assertEqual(self.row(two)['status'], 'queued')
        with patch('qq_chat.tasks.asyncio.sleep', return_value=None):
            await self.bridge.tasks._stop_watchdog(ident)
        self.assertTrue(any('停止尚未确认' in text for _, text in self.sent))
        self.back.release.set()
        await self.until(lambda: self.row(ident)['status'] == 'done')

    async def test_timeout_requests_interruption_and_waits_for_confirmed_outcome(self):
        self.cfg['tasks']['timeout_minutes'] = .000001
        ident = await self.create()
        await self.until(lambda: self.row(ident)['reported'])
        self.assertEqual(self.row(ident)['status'], 'stopped')
        self.assertEqual(self.back.interrupts, [f'task:{ident}'])

    async def test_uncertain_transport_retains_lock_and_manual_release_is_final(self):
        self.back.error = ExecutionUncertain('disconnected')
        self.back.release.set()
        ident = await self.create()
        await self.until(lambda: self.row(ident)['reported'])
        self.assertEqual(self.row(ident)['status'], 'orphaned')
        self.assertIsNone(self.row(ident)['finished'])
        await self.bridge.tasks.release(self.bridge.conversation(self.owner), ident)
        await self.until(lambda: self.row(ident)['reported'])
        self.assertEqual(self.row(ident)['status'], 'stopped')
        self.assertIn('主人确认', self.row(ident)['error'])

    async def test_report_pending_retries_saved_text_without_model_and_only_to_origin(self):
        self.fail_send = True
        self.back.release.set()
        ident = await self.create(source=self.group)
        await self.until(lambda: self.row(ident)['report_text'])
        await self.until(lambda: ident not in self.bridge.tasks.runners)
        self.assertEqual(self.row(ident)['reported'], 0)
        count = len(self.front.requests)
        self.fail_send = False
        await self.bridge.flush_task_reports()
        self.assertEqual(len(self.front.requests), count)
        self.assertEqual(self.row(ident)['reported'], 1)
        self.assertEqual({source for source, _ in self.sent}, {self.group})
        await self.bridge.flush_task_reports()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.voices), 1)
        self.assertIn('后台任务回报', self.bridge.db.execute("SELECT content FROM history WHERE role='user'").fetchone()[0])

    async def test_failed_report_fallback_uses_failed_label_and_no_stream(self):
        self.back.error = RuntimeError('backend error'); self.back.release.set()
        self.front.error = RuntimeError('front unavailable')
        ident = await self.create()
        await self.until(lambda: self.row(ident)['reported'])
        self.assertIn('失败', self.row(ident)['report_text'])
        self.assertNotIn('完成', self.row(ident)['report_text'])
        req = self.front.requests[-1]
        self.assertEqual((req.role, req.level), (CHAT, NONE))
        self.assertIsNone(req.on_answer)
        self.assertIsNone(req.on_progress)
        self.assertTrue(req.text.startswith('[系统：后台任务回报'))
        self.assertTrue((self.root / f'workspace/tasks/{ident}.md').exists())

    async def test_new_keeps_task_and_report_uses_current_session(self):
        ident = await self.create()
        await self.until(lambda: self.back.requests)
        self.front.sessions[self.bridge.conversation(self.owner)] = 'old-session'
        await self.worker_command('/new')
        self.assertNotEqual(self.row(ident)['status'], 'stopped')
        self.assertIn('1 个后台任务', self.sent[-1][1])
        self.back.release.set()
        await self.until(lambda: self.row(ident)['reported'])
        self.assertNotEqual(self.front.requests[-1].session_id, 'old-session')

    async def test_conversation_lock_covers_report_model_command_and_new(self):
        self.front.release.clear()
        self.back.release.set()
        ident = await self.create()
        await self.front.entered.wait()
        worker = asyncio.create_task(self.bridge.worker())
        try:
            await self.bridge.queue.put(Incoming(self.owner, '/new'))
            await asyncio.sleep(.01)
            self.assertFalse(self.bridge.queue.empty() and self.bridge.queue._unfinished_tasks == 0)
            self.assertEqual(len(self.front.requests), 1)
            self.front.release.set()
            await asyncio.wait_for(self.bridge.queue.join(), 2)
            self.assertTrue(self.row(ident)['reported'])
            self.assertIsNone(self.front.session_id(self.bridge.conversation(self.owner)))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_stop_all_only_stops_tasks_from_current_conversation(self):
        self.back.interrupt_mode = 'waiting'
        one = await self.create()
        await self.until(lambda: self.back.requests)
        two = await self.create('same chat')
        three = await self.create('other chat', self.group)
        await self.bridge.fast_command(self.owner, '/stop all')
        self.assertEqual(self.row(one)['status'], 'stopping')
        self.assertEqual(self.row(two)['status'], 'interrupted')
        self.assertEqual(self.row(three)['status'], 'queued')

    async def test_task_command_works_without_model_and_silent_guest_commands(self):
        for command in ('/task goal', '/task release 1', '/tasks', '/stop', '/stop all'):
            await self.worker_command(command, self.guest)
        self.assertEqual(self.front.requests, [])
        self.assertEqual(self.sent, [])
        await self.worker_command('/task\trepair readme')
        self.assertTrue(any('已下发任务 #1' in text for _, text in self.sent))

    async def test_front_stop_distinguishes_completed_and_failed_outcomes(self):
        for fail in (False, True):
            self.front.entered.clear(); self.front.release.clear(); self.front.error = None
            async def interrupt(key):
                if fail: self.front.error = RuntimeError('front failed')
                self.front.release.set()
                return True
            self.front.interrupt = interrupt
            turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
            await self.front.entered.wait()
            await self.bridge.fast_command(self.owner, '/stop')
            await asyncio.gather(turn, return_exceptions=True)
            expected = '停止前已失败' if fail else '停止前已完成'
            self.assertTrue(any(expected in text for _, text in self.sent))
        self.front.error = None

    async def test_front_stop_timeout_does_not_release_conversation(self):
        self.front.release.clear()
        async def interrupt(key): return True
        self.front.interrupt = interrupt
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        await self.bridge.fast_command(self.owner, '/stop')
        token = self.bridge._front_tokens[self.bridge.conversation(self.owner)]
        try:
            with patch('qq_chat.task_commands.asyncio.sleep', return_value=None):
                await self.bridge.front_stop_watchdog(self.owner, token)
            self.assertFalse(turn.done())
            self.assertTrue(any('前台停止尚未确认' in text for _, text in self.sent))
        finally:
            self.front.release.set()
            await turn

    async def test_fast_commands_bypass_busy_worker_and_do_not_stop_backend_by_default(self):
        ident = await self.create()
        await self.until(lambda: self.back.requests)
        self.front.release.clear()
        events = asyncio.Queue()

        class Socket:
            def __aiter__(self): return self
            async def __anext__(self): return await events.get()

        def event(ident, text, user=10001):
            return json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 40001,
                               'user_id': user, 'message_id': ident, 'message': [{'type': 'text', 'data': {'text': text}}]})

        async def interrupt(key):
            self.front.error = Interrupted('front ended'); self.front.release.set()
            return True
        self.front.interrupt = interrupt
        session = asyncio.create_task(self.bridge.session(Socket()))
        try:
            await events.put(event(1, 'hello'))
            await self.front.entered.wait()
            await events.put(event(2, '/tasks'))
            await self.until(lambda: any('运行中' in text for _, text in self.sent))
            await events.put(event(3, '/stop'))
            await self.until(lambda: any('已确认停止' in text for _, text in self.sent))
            self.assertEqual(self.row(ident)['status'], 'running')
            self.assertEqual(self.back.interrupts, [])
            self.assertEqual(self.bridge.queue.qsize(), 0)
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)
        # Losing the QQ socket cancels the foreground worker, never the background runner.
        self.assertEqual(self.row(ident)['status'], 'running')


class TaskConfigTests(unittest.TestCase):
    def test_reject_unsafe_writes_and_unimplemented_parallelism(self):
        base = {'agents': {'front': 'work', 'back': ['work'], 'profiles': {
            'work': {'type': 'codex_app', 'path': 'unused', 'writes': True}}}}
        settings(base)
        for changes in ({'writes': False}, {'writes': 'yes'}, {'max_parallel': 2}, {'sandbox': 'read-only'}):
            cfg = json.loads(json.dumps(base)); cfg['agents']['profiles']['work'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): settings(cfg)
        for limits in ({'max_writers': 2}, {'max_running': 0}, {'timeout_minutes': 0}, {'progress': 'bad'}):
            cfg = {**base, 'tasks': limits}
            with self.subTest(limits=limits), self.assertRaises(ValueError): settings(cfg)

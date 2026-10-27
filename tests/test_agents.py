"""Exercise the agent boundary without model calls, QQ, or live databases."""
import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.agents import Agent, CHAT, FULL, NONE, TurnRequest, build_agents
from qq_chat.agents.base import MAX_REPLY
from qq_chat.agents.codex import CodexAgent
from qq_chat.bridge import Bridge, Incoming, Source
from qq_chat.codex_app import AppServer


class FakeAgent(Agent):
    name = 'codex'

    def __init__(self, reply='完成。'):
        self.reply = reply
        self.prepared, self.requests = [], []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.error = None
        self.sessions = {}

    async def start(self):
        pass

    async def prepare(self, req):
        self.prepared.append(replace(req))
        return self.sessions.setdefault(req.key, 'fake-' + req.key)

    async def run(self, req):
        self.requests.append(replace(req))
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise self.error
        if req.on_answer:
            req.on_answer(self.reply)
        return self.reply

    def session_id(self, key):
        return self.sessions.get(key)

    def model_state(self, session_id):
        return 'fake-model', 'medium'

    async def reset(self, key):
        self.sessions.pop(key, None)

    async def models(self):
        return [{'model': 'fake-model'}]

    async def default_model(self):
        return 'fake-model'

    async def close(self):
        pass


class AgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        (root / 'workspace').mkdir()
        (root / 'workspace/SOUL.md').write_text('shared persona')
        (root / 'workspace/OWNER.md').write_text('private owner profile')
        patcher = patch('qq_chat.bridge.BASE', root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = Bridge({'owner_qq': 10001, 'codex_path': 'unused'})
        self.addCleanup(self.bridge.db.close)
        self.fake = FakeAgent()
        self.bridge.agents['codex'] = self.fake
        self.sent, self.voiced = [], []

        async def send(source, text):
            self.sent.append((source, text))

        async def voice(source, text):
            self.voiced.append((source, text))

        self.bridge.send, self.bridge.enqueue_voice = send, voice
        self.owner = Source('private', 10001, 10001)

    async def dispatch(self, source, text):
        worker = asyncio.create_task(self.bridge.worker())
        try:
            self.bridge.queue.put_nowait(Incoming(source, text))
            await asyncio.wait_for(self.bridge.queue.join(), 2)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def test_roles_permissions_instructions_and_image_callbacks(self):
        sources = (self.owner, Source('group', 20001, 10001),
                   Source('group', 20001, 30001, '心糖'), Source('private', 30001, 30001))
        for source in sources:
            await self.bridge.chat(source, 'hello', image_paths=('picture.png',))
        self.assertEqual([req.level for req in self.fake.requests], [FULL, FULL, NONE, NONE])
        self.assertEqual([req.role for req in self.fake.requests], [CHAT] * 4)
        self.assertEqual([req.key for req in self.fake.requests],
                         ['private:10001', 'group:20001:owner', 'group:20001', 'private:30001'])
        for index, req in enumerate(self.fake.requests):
            self.assertIn('shared persona', req.instructions)
            self.assertEqual('private owner profile' in req.instructions, index < 2)
            self.assertEqual('[[BLOCK_CURRENT_GROUP_SENDER]]' in req.instructions, index == 2)
            self.assertEqual(req.images, ('picture.png',))
            self.assertEqual(req.on_answer is not None, index < 2)
            self.assertEqual(req.on_progress is not None, index < 2)
        self.assertTrue(self.sent)

    async def test_all_legacy_history_is_imported_once(self):
        entries = [('user', f'old-{i}-' + 'x' * 1100, 'private:10001') for i in range(24)]
        self.bridge.db.executemany('INSERT INTO history VALUES (?, ?, ?)', entries)
        self.bridge.db.commit()
        await self.bridge.chat(self.owner, 'new-message')
        text = self.fake.requests[-1].text
        for _, content, _ in entries:
            self.assertIn(content, text)
        await self.bridge.chat(self.owner, 'next-message')
        self.assertEqual(self.fake.requests[-1].text, 'next-message')
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 24)

    async def test_controlled_agent_failure_does_not_bootstrap_or_persist_reply(self):
        self.fake.error = RuntimeError('backend failed')
        self.fake.release.clear()
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await asyncio.wait_for(self.fake.entered.wait(), 1)
        self.assertFalse(turn.done())
        self.fake.release.set()
        with self.assertRaisesRegex(RuntimeError, 'backend failed'):
            await turn
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM bootstrap').fetchone()[0], 0)
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)

    async def test_worker_keeps_guest_bounding_voice_and_history(self):
        guest = Source('group', 20001, 30001)
        self.fake.reply = '长' * 700
        await self.dispatch(guest, 'hello')
        expected = '长' * 500 + '…'
        self.assertEqual(self.sent, [(guest, expected)])
        self.assertEqual(self.voiced, [(guest, expected)])
        self.assertEqual(self.bridge.db.execute("SELECT content FROM history WHERE role='assistant'").fetchone()[0], expected)

    async def test_management_uses_agent_state_but_guests_cannot_reach_it(self):
        await self.dispatch(Source('private', 30001, 30001), '/models')
        self.assertIn('仅限主人', self.sent[-1][1])
        self.assertEqual(self.fake.prepared, [])
        await self.dispatch(self.owner, '/model')
        self.assertIn('fake-model', self.sent[-1][1])
        await self.dispatch(self.owner, '/session')
        self.assertIn('fake-private:10001', self.sent[-1][1])


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE threads (conversation TEXT PRIMARY KEY, thread_id TEXT);
            CREATE TABLE old_threads (conversation TEXT, thread_id TEXT, time REAL);
        ''')

    async def test_cap_only_return_value_and_preserve_explicit_none_effort(self):
        captured, streamed = [], []

        class Transport:
            async def turn(self, thread, text, **options):
                captured.append((thread, text, options))
                full = '好' * (MAX_REPLY + 100)
                options['on_answer'](full)
                return full

        agent = CodexAgent(self.db, '/unused', 'unused', app=Transport())
        req = TurnRequest('private:10001', 'hello', CHAT, FULL, '',
                          model='chosen', effort=None, session_id='thread', on_answer=streamed.append)
        self.assertEqual(len(await agent.run(req)), MAX_REPLY)
        self.assertEqual(len(streamed[0]), MAX_REPLY + 100)
        self.assertEqual(captured[-1][2]['model'], 'chosen')
        self.assertIn('effort', captured[-1][2])
        self.assertIsNone(captured[-1][2]['effort'])
        req.model = None
        await agent.run(req)
        self.assertNotIn('model', captured[-1][2])
        self.assertNotIn('effort', captured[-1][2])

    async def test_reset_archives_only_target_in_callers_transaction(self):
        self.db.executemany('INSERT INTO threads VALUES (?, ?)', [('one', 't1'), ('two', 't2')])
        self.db.commit()
        agent = CodexAgent(self.db, '/unused', 'unused')
        await agent.reset('one')
        self.assertIsNone(agent.session_id('one'))
        self.assertEqual(agent.session_id('two'), 't2')
        self.assertEqual(self.db.execute('SELECT conversation, thread_id FROM old_threads').fetchall(), [('one', 't1')])
        self.db.rollback()
        self.assertEqual(agent.session_id('one'), 't1')

    async def test_transport_retains_noncompleted_error_semantics(self):
        for status in ('interrupted', 'failed'):
            app = AppServer('unused')

            async def call(method, params):
                if method == 'turn/start':
                    app.events['thread'].put_nowait({'method': 'turn/completed', 'params': {
                        'turn': {'id': 'turn', 'status': status, 'error': 'original error'}}})
                    return {'turn': {'id': 'turn'}}
                self.assertEqual(method, 'turn/interrupt')
                return {}

            app.call = call
            agent = CodexAgent(self.db, '/unused', 'unused', app=app)
            with self.assertRaisesRegex(RuntimeError, 'Codex turn failed'):
                await agent.run(TurnRequest('key', 'text', CHAT, FULL, '', session_id='thread'))

    def test_default_factory_and_future_configuration_fail_closed(self):
        agents = build_agents({'codex_path': 'unused'}, self.db, '/unused')
        self.assertEqual(list(agents), ['codex'])
        self.assertIsInstance(agents['codex'], Agent)
        self.assertEqual(agents['codex'].app.executable, 'unused')
        with self.assertRaisesRegex(ValueError, 'not implemented in PR1'):
            build_agents({'codex_path': 'unused', 'agents': {'front': 'missing'}}, self.db, '/unused')

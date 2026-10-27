import asyncio
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.agents import build_agents
from qq_chat.agents.base import MissingSession
from qq_chat.agents.config import legacy_codex_profile, settings
from qq_chat.agents.state import AgentState, recent_history
from qq_chat.bridge import Bridge, Incoming, Source
from tests.test_agents import FakeAgent


def configuration():
    return {'owner_qq': 10001, 'agents': {
        'front': 'claude', 'guest': 'guest', 'back': [], 'profiles': {
            'claude': {'type': 'claude_code', 'path': '/usr/bin/python3', 'model': 'sonnet',
                       'models': ['sonnet', 'haiku'], 'permission_mode': 'dontAsk'},
            'guest': {'type': 'claude_code', 'path': '/usr/bin/python3', 'model': 'haiku', 'models': ['haiku']},
            'codex': {'type': 'codex_app', 'path': 'unused'}}}}


class ProfileConfigTests(unittest.TestCase):
    def test_defaults_and_mixed_profiles(self):
        self.assertEqual(settings({'codex_path': 'unused'})[:2], ('codex', 'codex'))
        cfg = configuration()
        self.assertEqual(settings(cfg)[:2], ('claude', 'guest'))
        self.assertEqual(legacy_codex_profile(cfg), 'codex')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        agents = build_agents(cfg, db, '/tmp/workspace')
        self.assertEqual(set(agents), {'claude', 'guest', 'codex'})

    def test_codex_path_inheritance_does_not_mutate_configuration(self):
        cfg = {'codex_path': 'inherited', 'agents': {'front': 'codex', 'profiles': {'codex': {'type': 'codex_app'}}}}
        self.assertEqual(settings(cfg)[2]['codex']['path'], 'inherited')
        self.assertNotIn('path', cfg['agents']['profiles']['codex'])

    def test_invalid_configuration_fails_before_launch(self):
        variants = []
        cfg = configuration(); cfg['agents']['front'] = 'missing'; variants.append(cfg)
        cfg = configuration(); cfg['agents']['profiles']['guest']['type'] = 'unknown'; variants.append(cfg)
        cfg = configuration(); del cfg['agents']['profiles']['claude']['permission_mode']; variants.append(cfg)
        cfg = configuration(); cfg['agents']['back'] = ['codex']; variants.append(cfg)
        cfg = configuration(); cfg['agents']['profiles']['guest']['model'] = 'unknown'; variants.append(cfg)
        cfg = configuration(); cfg['agents']['profiles']['guest']['max_turns'] = 0; variants.append(cfg)
        for cfg in variants:
            with self.subTest(config=cfg), self.assertRaises(ValueError):
                settings(cfg)

    def test_codex_profiles_share_transport_but_not_session_namespace(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        cfg = {'agents': {'front': 'codex', 'guest': 'readonly', 'profiles': {
            'codex': {'type': 'codex_app', 'path': 'unused'},
            'readonly': {'type': 'codex_app', 'path': 'unused', 'sandbox': 'read-only'}}}}
        agents = build_agents(cfg, db, '/tmp/workspace')
        self.assertIs(agents['codex'].app, agents['readonly'].app)
        self.assertTrue(agents['codex'].legacy)
        self.assertFalse(agents['readonly'].legacy)


class AgentStateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE conversation_models(conversation TEXT PRIMARY KEY, model TEXT, effort TEXT);
            CREATE TABLE history(role TEXT, content TEXT, conversation TEXT);
        ''')
        self.db.execute('INSERT INTO conversation_models VALUES (?, ?, ?)', ('private:10001', 'codex-model', 'high'))
        self.db.commit()

    def test_migration_and_model_preferences_are_profile_isolated(self):
        state = AgentState(self.db, 'codex', {'codex': 'codex_app', 'claude': 'claude_code'})
        self.assertEqual(state.selection('private:10001', 'codex'), ('codex-model', 'high'))
        self.assertIsNone(state.selection('private:10001', 'claude'))
        state.save('private:10001', 'claude', 'haiku', 'low')
        self.assertEqual(self.db.execute('SELECT model, effort FROM conversation_models').fetchone(), ('codex-model', 'high'))
        restarted = AgentState(self.db, 'other', {'other': 'codex_app'})
        self.assertEqual(restarted.legacy_profile, 'codex')
        self.assertIsNone(restarted.selection('private:10001', 'other'))
        self.assertEqual(restarted.selection('private:10001', 'claude'), ('haiku', 'low'))
        state.save('private:10001', 'codex', 'changed', 'low')
        AgentState(self.db, 'codex')
        self.assertEqual(state.selection('private:10001', 'codex'), ('changed', 'low'))

    def test_reusing_profile_name_for_different_backend_is_rejected(self):
        AgentState(self.db, 'codex', {'codex': 'codex_app'})
        with self.assertRaisesRegex(ValueError, 'Profile type changed'):
            AgentState(self.db, 'codex', {'codex': 'claude_code'})

    def test_history_count_size_order_and_conversation_isolation(self):
        self.db.executemany('INSERT INTO history VALUES (?, ?, ?)', [('user', str(i), 'one') for i in range(30)])
        self.db.execute('INSERT INTO history VALUES (?, ?, ?)', ('user', 'private elsewhere', 'two'))
        history = recent_history(self.db, 'one')
        self.assertEqual(history, [('user', str(i)) for i in range(10, 30)])
        self.db.executemany('INSERT INTO history VALUES (?, ?, ?)', [('user', '长' * 2000, 'long') for _ in range(30)])
        bounded = recent_history(self.db, 'long')
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), 8000)
        self.assertTrue(all(len(content) <= 1000 for _, content in bounded))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 61)


class ProfileBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'workspace').mkdir()
        patcher = patch('qq_chat.bridge.BASE', self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        self.cfg = configuration()
        self.bridge = Bridge(self.cfg)
        self.addCleanup(self.bridge.db.close)
        self.front, self.guest = FakeAgent(), FakeAgent()
        self.front.name, self.guest.name = 'claude', 'guest'
        self.front.profile = self.cfg['agents']['profiles']['claude']
        self.guest.profile = self.cfg['agents']['profiles']['guest']
        self.bridge.agents.update(claude=self.front, guest=self.guest)
        self.owner = Source('private', 10001, 10001)
        self.sent, self.voiced = [], []

        async def send(source, text): self.sent.append((source, text))
        async def voice(source, text): self.voiced.append((source, text))
        self.bridge.send, self.bridge.enqueue_voice = send, voice

    async def dispatch(self, source, text):
        worker = asyncio.create_task(self.bridge.worker())
        try:
            self.bridge.queue.put_nowait(Incoming(source, text))
            await asyncio.wait_for(self.bridge.queue.join(), 2)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError): await worker

    async def test_owner_and_guests_use_different_profiles_and_defaults(self):
        await self.bridge.chat(self.owner, 'hello')
        await self.bridge.chat(Source('private', 30001, 30001), 'hello')
        self.assertEqual(self.front.requests[-1].model, 'sonnet')
        self.assertEqual(self.guest.requests[-1].model, 'haiku')
        self.assertEqual(len(self.front.requests), 1)
        self.assertEqual(len(self.guest.requests), 1)
        self.assertIn('非主人数学题不提供答案', self.guest.requests[-1].instructions)
        self.assertIn('QQ回复不发送链接', self.guest.requests[-1].instructions)

    async def test_persona_changes_are_loaded_on_the_next_turn(self):
        soul = self.root / 'workspace/SOUL.md'
        soul.write_text('persona version one')
        await self.bridge.chat(self.owner, 'hello')
        self.assertIn('persona version one', self.front.requests[-1].instructions)
        soul.write_text('persona version two')
        await self.bridge.chat(self.owner, 'hello again')
        self.assertIn('persona version two', self.front.requests[-1].instructions)
        self.assertNotIn('persona version one', self.front.requests[-1].instructions)

    async def test_model_command_persists_only_front_profile_and_survives_new(self):
        async def models(): return [{'model': 'sonnet'}, {'model': 'haiku'}]
        self.front.models = models
        await self.dispatch(self.owner, '/model haiku')
        self.assertEqual(self.bridge.selection(self.owner)[0], 'haiku')
        self.assertEqual(self.bridge.db.execute('SELECT profile, model FROM profile_models').fetchall(), [('claude', 'haiku')])
        self.assertIsNone(self.bridge.db.execute('SELECT model FROM conversation_models').fetchone())
        await self.dispatch(self.owner, '/new')
        self.assertEqual(self.bridge.selection(self.owner)[0], 'haiku')
        restarted = Bridge(copy.deepcopy(self.cfg))
        try:
            self.assertEqual(restarted.selection(self.owner)[0], 'haiku')
        finally:
            restarted.db.close()

    async def test_missing_session_reimports_bounded_history_once(self):
        self.bridge.db.execute('INSERT INTO history VALUES (?, ?, ?)', ('user', 'previous message', 'private:10001'))
        self.bridge.db.execute('INSERT INTO bootstrap VALUES (?)', ('fake-private:10001',))
        self.bridge.db.commit()
        run = self.front.run
        count = 0

        async def missing(req):
            nonlocal count
            count += 1
            if count == 1:
                raise MissingSession('gone')
            return await run(req)

        async def reset(key):
            self.front.sessions[key] = 'replacement'

        self.front.run, self.front.reset = missing, reset
        await self.bridge.chat(self.owner, 'new question')
        self.assertEqual(count, 2)
        self.assertIn('previous message', self.front.requests[-1].text)
        self.assertTrue(self.bridge.db.execute("SELECT 1 FROM bootstrap WHERE thread_id='replacement'").fetchone())

    async def test_owner_history_uses_validated_all_text_not_final_result_only(self):
        async def run(req):
            req.history_text = '第一句\n\n最后一句'
            req.on_answer(req.history_text)
            return '最后一句'

        self.front.run = run
        await self.dispatch(self.owner, 'hello')
        saved = self.bridge.db.execute("SELECT content FROM history WHERE role='assistant'").fetchone()[0]
        self.assertEqual(saved, '第一句\n\n最后一句')

"""Native tools cross the host boundary without giving guest shells more access."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import patch

from qq_chat.agents.base import CHAT, NONE, TurnRequest
from qq_chat.agents.codex import CodexAgent
from qq_chat.codex_app import AppServer
from qq_chat.bridge import Source
from qq_chat.tool_server import GUEST_SPEECHES
from tests import test_speak as speech_fixtures

LINE = speech_fixtures.LINE


class NativeSpeechTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = speech_fixtures.SpeakToolTests.asyncSetUp
    asyncTearDown = speech_fixtures.SpeakToolTests.asyncTearDown
    setup_tools = speech_fixtures.SpeakToolTests.setup_tools
    full_codex = speech_fixtures.SpeakToolTests.full_codex
    enable_voice = speech_fixtures.SpeakToolTests.enable_voice

    async def test_guest_native_binding_has_only_speak_and_preserves_all_checks(self):
        server = await self.setup_tools()
        self.full_codex()
        self.enable_voice()
        self.cfg['voice']['group_enabled'] = True
        guest = Source('group', 20001, 30001)
        args = {'text': LINE, 'style': '开心', 'scene': '欢迎回来的群友'}
        async with server.grant(guest, names=('speak',)) as grant:
            tools, invoke = server.native_binding(grant)
            self.assertEqual([x['name'] for x in tools], ['speak'])
            self.assertEqual(tools[0]['type'], 'function')
            for name, arguments in [('memory_search', {'query': 'private'}),
                                    ('dispatch_task', {'goal': 'no'}),
                                    ('speak', {**args, 'group_id': 99})]:
                with self.assertRaises(ValueError):
                    await invoke(name, arguments)
            with self.assertRaisesRegex(ValueError, '太短'):
                await invoke('speak', {**args, 'text': '嗯'})
            bound = server.grants[grant.token]
            server.grants[grant.token] = (*bound[:2], 0)
            with self.assertRaisesRegex(ValueError, '失效'):
                await invoke('speak', args)
            server.grants[grant.token] = bound
            with patch.object(self.bridge, 'blocked', return_value=True):
                with self.assertRaisesRegex(ValueError, '限制'):
                    await invoke('speak', args)
            for _ in range(GUEST_SPEECHES):
                self.assertEqual(await invoke('speak', args), {'status': 'queued'})
            with self.assertRaisesRegex(ValueError, f'最多{GUEST_SPEECHES}段'):
                await invoke('speak', args)
            self.cfg['voice']['group_enabled'] = False
            with self.assertRaises(ValueError):
                await invoke('speak', args)
        with self.assertRaisesRegex(ValueError, '失效'):
            await invoke('speak', args)
        await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(self.audio, [guest] * GUEST_SPEECHES)
        self.assertFalse(server.speech_counts)
        await self.bridge.voice.close()

    async def test_bridge_passes_native_tool_without_command_or_capability_file(self):
        await self.setup_tools()
        self.full_codex()
        self.enable_voice()
        self.cfg['voice']['group_enabled'] = True
        guest = Source('group', 20001, 30001)
        await self.bridge.chat(guest, '说句话')
        req = self.front.requests[-1]
        self.assertEqual(req.level, NONE)
        self.assertEqual([x['name'] for x in req.dynamic_tools], ['speak'])
        self.assertNotIn('--grant', req.runtime_instructions)
        self.assertNotIn('python3', req.runtime_instructions)
        with self.assertRaisesRegex(ValueError, '失效'):
            await req.on_tool_call('speak', {'text': LINE, 'style': '日常'})
        await self.bridge.chat(self.owner, 'hello')
        owner_req = self.front.requests[-1]
        self.assertEqual(owner_req.dynamic_tools, ())
        self.assertIn('--grant', owner_req.runtime_instructions)
        await self.bridge.voice.close()

    async def test_queue_failure_is_not_reported_as_queued(self):
        server = await self.setup_tools()
        self.enable_voice()
        self.cfg['voice']['group_enabled'] = True
        async with server.grant(Source('group', 20001, 30001), names=('speak',)) as grant:
            _, invoke = server.native_binding(grant)
            with patch.object(self.bridge.voice, 'say', return_value=False):
                with self.assertRaisesRegex(ValueError, '队列已满'):
                    await invoke('speak', {'text': LINE, 'style': '日常'})
            self.assertFalse(server.speech_counts)
        await self.bridge.voice.close()


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    def params(self, **changes):
        return {'threadId': 't', 'turnId': 'u', 'callId': 'c', 'tool': 'speak',
                'namespace': None, 'arguments': {'text': LINE, 'style': '日常'}, **changes}

    async def test_turn_scope_errors_and_duplicate_requests(self):
        app = AppServer('unused')
        calls, cache = [], {}
        async def handler(name, arguments):
            calls.append((name, arguments))
            return {'status': 'queued'}
        for params in [self.params(threadId='other'), self.params(turnId='old'),
                       self.params(namespace='untrusted'), self.params(callId='')]:
            self.assertFalse((await app.tool_call(params, 't', 'u', handler, cache))['success'])
        self.assertFalse((await app.tool_call(self.params(), 't', 'u', None, cache))['success'])
        first = await app.tool_call(self.params(), 't', 'u', handler, cache)
        self.assertTrue(first['success'])
        self.assertEqual(await app.tool_call(self.params(), 't', 'u', handler, cache), first)
        self.assertEqual(len(calls), 1)
        self.assertFalse((await app.tool_call(self.params(arguments={}), 't', 'u', handler, cache))['success'])
        async def fail(*args): raise RuntimeError('private backend details')
        result = await app.tool_call(self.params(callId='fail'), 't', 'u', fail, cache)
        self.assertFalse(result['success'])
        self.assertNotIn('private backend', str(result))

    async def test_rpc_reader_routes_tools_but_never_approves_other_requests(self):
        app = AppServer('unused')
        class Socket:
            def __aiter__(self): return self.generate()
            async def generate(self):
                yield json.dumps({'id': 1, 'method': 'item/tool/call', 'params': self_params})
                yield json.dumps({'id': 2, 'method': 'item/tool/call', 'params': {**self_params, 'threadId': 'missing'}})
                yield json.dumps({'id': 3, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 't'}})
        self_params = self.params()
        app.ws = Socket()
        q = app.events['t'] = asyncio.Queue()
        written = []
        async def write(data): written.append(data)
        app.write = write
        await app.read()
        self.assertEqual((await q.get())['id'], 1)
        self.assertFalse(written[0]['result']['success'])
        self.assertIn('error', written[1])

    async def test_turn_delivers_tool_receipt_and_completes(self):
        app = AppServer('unused')
        written = []
        async def call(method, params):
            self.assertEqual(method, 'turn/start')
            q = app.events['t']
            q.put_nowait({'id': 8, 'method': 'item/tool/call', 'params': self.params()})
            q.put_nowait({'method': 'item/completed', 'params': {'item': {
                'type': 'agentMessage', 'id': 'a', 'text': '排队了', 'phase': 'final_answer'}}})
            q.put_nowait({'method': 'turn/completed', 'params': {'turn': {'id': 'u', 'status': 'completed'}}})
            return {'turn': {'id': 'u'}}
        async def write(data): written.append(data)
        async def handler(*args): return {'status': 'queued'}
        app.call, app.write = call, write
        self.assertEqual(await app.turn('t', 'hello', on_tool_call=handler), '排队了')
        self.assertTrue(written[0]['result']['success'])
        self.assertEqual(json.loads(written[0]['result']['contentItems'][0]['text']), {'status': 'queued'})
        self.assertFalse(app.events)


class NativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_archives_once_and_keeps_sandbox_read_only(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE threads (conversation TEXT PRIMARY KEY, thread_id TEXT);'
                         'CREATE TABLE old_threads (conversation TEXT, thread_id TEXT, time REAL);'
                         "INSERT INTO threads VALUES ('group:1','old');")
        calls = []
        class App:
            async def start(self): pass
            async def call(self, method, params):
                calls.append((method, params))
                return {'thread': {'id': 'new'}, 'model': 'actual', 'reasoningEffort': 'medium'}
        tools = ({'type': 'function', 'name': 'speak', 'description': 'test', 'inputSchema': {}},)
        agent = CodexAgent(db, '/unused', 'unused', app=App())
        req = TurnRequest('group:1', 'hello', CHAT, NONE, '', dynamic_tools=tools)
        self.assertEqual(await agent.prepare(req), 'new')
        self.assertEqual(calls[0][0], 'thread/start')
        self.assertEqual(calls[0][1]['sandbox'], 'read-only')
        self.assertEqual(calls[0][1]['dynamicTools'], list(tools))
        self.assertEqual(db.execute('SELECT thread_id FROM old_threads').fetchall(), [('old',)])
        # Recreating the adapter simulates a bridge restart.
        agent = CodexAgent(db, '/unused', 'unused', app=App())
        self.assertEqual(await agent.prepare(req), 'new')
        self.assertEqual(calls[-1][0], 'thread/resume')
        self.assertNotIn('dynamicTools', calls[-1][1])
        self.assertEqual(db.execute('SELECT count(*) FROM old_threads').fetchone()[0], 1)

    async def test_failed_registration_keeps_original_binding(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('CREATE TABLE threads (conversation TEXT PRIMARY KEY, thread_id TEXT);'
                         'CREATE TABLE old_threads (conversation TEXT, thread_id TEXT, time REAL);'
                         "INSERT INTO threads VALUES ('group:1','old');")
        class App:
            async def start(self): pass
            async def call(self, *args): raise RuntimeError('registration failed')
        agent = CodexAgent(db, '/unused', 'unused', app=App())
        req = TurnRequest('group:1', '', CHAT, NONE, '', dynamic_tools=({'name': 'speak'},))
        with self.assertRaisesRegex(RuntimeError, 'registration failed'):
            await agent.prepare(req)
        self.assertEqual(agent.session_id('group:1'), 'old')
        self.assertEqual(db.execute('SELECT count(*) FROM old_threads').fetchone()[0], 0)

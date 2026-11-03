"""Agent-chosen speech: validation, per-turn grant, real CLI transport and voice routing."""
import asyncio
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.agents.base import NONE
from qq_chat import prompts
from qq_chat.bridge import Source, Bridge
from qq_chat.memory_progress import SPEAK_SCRIPT, MemoryProgress, speak_command
from qq_chat.voice import SPEAK_MAX_CHARS, ProcessSynthesizer, VoiceReplies, speak_text
import tests.test_front_tools as front_tests
from tests.test_voice import wav_bytes

LINE = '准备好了吗？我们出发吧！'


class SpeakTextTests(unittest.TestCase):
    def test_accepts_complete_lines_and_rejects_fragments_and_unknown_options(self):
        self.assertEqual(speak_text('今天也辛苦啦~', '安慰', '中'), '今天也辛苦啦！')
        for text in ('我想听。', '嗯嗯！！', '😀😀😀😀😀😀', None):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '太短'):
                speak_text(text, '日常', '中')
        with self.assertRaisesRegex(ValueError, '最多'):
            speak_text('好' * (SPEAK_MAX_CHARS + 1), '日常', '中')
        for style, intensity in (('愤怒', '中'), ('日常', '极高'), ('/tmp/a.wav', '中')):
            with self.assertRaises(ValueError):
                speak_text(LINE, style, intensity)


class SpeakQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_style_reaches_synthesizer_only_for_spoken_lines(self):
        calls, sent = [], []

        class Synth:
            async def synthesize(self, text, **options):
                calls.append((text, options))
                return wav_bytes()

            async def close(self): pass

        async def send(source, audio): sent.append(source)
        worker = VoiceReplies(Synth(), send, send, lambda source: False)
        try:
            self.assertTrue(worker.say('owner', LINE, '开心', '高'))
            self.assertTrue(worker.submit('owner', '普通回复内容'))
            await asyncio.wait_for(worker.queue.join(), 2)
        finally:
            await worker.close()
        self.assertEqual(calls, [(LINE, {'style': '开心', 'intensity': '高'}), ('普通回复内容', {})])
        with self.assertRaisesRegex(ValueError, 'scene'):
            worker.say('owner', LINE, '开心', '中', 'x' * 61)
        self.assertEqual(sent, ['owner', 'owner'])

    async def test_process_protocol_omits_style_for_text_only_backends(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'requests.jsonl'
            script = ('import sys,json\n'
                      f'log=open({str(log)!r},"a")\n'
                      'for line in sys.stdin:\n'
                      ' log.write(line); log.flush()\n'
                      f' print(json.dumps({{"wav": {__import__("base64").b64encode(wav_bytes()).decode()!r}}}),flush=True)\n')
            backend = ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=5)
            try:
                await backend.synthesize('第一句')
                await backend.synthesize('第二句', style='安慰', intensity='低')
            finally:
                await backend.close()
            requests = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(requests, [{'text': '第一句'},
                                    {'text': '第二句', 'style': '安慰', 'intensity': '低'}])


class SpeakToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = front_tests.FrontToolsTests.asyncSetUp
    asyncTearDown = front_tests.FrontToolsTests.asyncTearDown
    setup_tools = front_tests.FrontToolsTests.setup_tools
    token = front_tests.FrontToolsTests.token

    def enable_voice(self):
        self.calls, self.audio = [], []

        class Synth:
            async def synthesize(inner, text, **options):
                self.calls.append((text, options))
                return wav_bytes()

            async def close(inner): pass

        async def send_audio(source, audio): self.audio.append(source)
        async def notice(source, text): pass
        self.cfg['voice'] = {'enabled': True, 'mode': 'tool'}
        self.bridge.voice = VoiceReplies(Synth(), send_audio, notice, self.bridge.blocked)

    def full_codex(self):
        self.cfg['agents']['back'] = []
        self.cfg['agents']['profiles']['front']['type'] = 'codex_app'

    async def test_grant_lists_speak_only_when_granted_and_voices_the_bound_source(self):
        server = await self.setup_tools()
        async with server.grant(self.owner) as grant:
            names = {t['name'] for t in await server.call(self.token(grant), 'tools/list', {})}
            self.assertNotIn('speak', names)  # Claude FRONT tool set is unchanged.
            with self.assertRaises(ValueError):
                await server.call(self.token(grant), 'speak', {'text': LINE, 'style': '开心'})
        async with server.grant(self.group, names=('speak',)) as grant:
            token = self.token(grant)
            listed = await server.call(token, 'tools/list', {})
            self.assertEqual([t['name'] for t in listed], ['speak'])
            with self.assertRaisesRegex(ValueError, '未启用'):
                await server.call(token, 'speak', {'text': LINE, 'style': '开心'})
            self.enable_voice()
            for arguments in ({'text': LINE, 'style': '开心', 'group_id': 1},
                              {'text': '我想听。', 'style': '日常'},
                              {'text': LINE, 'style': '愤怒'}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    await server.call(token, 'speak', arguments)
            self.assertEqual(await server.call(token, 'speak', {'text': LINE, 'style': '开心'}),
                             {'status': 'queued'})
            await server.call(token, 'speak', {'text': LINE, 'style': '开心', 'scene': '  缠着对方撒娇 '})
            await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(self.calls, [(LINE, {'style': '开心', 'intensity': '中'}),
                                      (LINE, {'style': '开心', 'intensity': '中', 'scene': '缠着对方撒娇'})])
        self.assertEqual(self.audio, [self.group, self.group])
        with self.assertRaises(ValueError):
            async with server.grant(self.owner, names=('speak', 'shell')): pass
        await self.bridge.voice.close()

    async def test_real_cli_reports_queue_and_correctable_errors(self):
        server = await self.setup_tools()
        self.enable_voice()
        command = [sys.executable, '-I', str(Path(__file__).parents[1] / 'tools/speak.py')]

        async def run(grant, text, style='日常', scene=()):
            proc = await asyncio.create_subprocess_exec(
                *command, '--grant', grant.config_path, '--style', style, '--intensity', '中',
                *scene, '--text', text, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 5)
            self.assertEqual(err, b'')
            return proc.returncode, json.loads(out)

        async with server.grant(self.owner, names=('speak',)) as grant:
            self.assertEqual(await run(grant, LINE, '开心'), (0, {'status': 'queued'}))
            self.assertEqual(await run(grant, LINE, '开心', ('--scene', '出发前雀跃')), (0, {'status': 'queued'}))
            code, result = await run(grant, '我想听。')
            self.assertEqual((code, result['code']), (1, 'request_rejected'))
            self.assertIn('太短', result['error'])
            await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(await run(grant, LINE), (1, {'error': '本轮授权文件不存在，请使用当前轮提供的命令，不能复用历史路径',
                                                      'code': 'grant_missing'}))
        self.assertEqual(self.audio, [self.owner, self.owner])
        self.assertEqual(self.calls[1][1].get('scene'), '出发前雀跃')
        await self.bridge.voice.close()

    async def test_full_owner_turn_gets_speak_command_only_in_tool_mode(self):
        server = await self.setup_tools()
        self.full_codex()
        await self.bridge.chat(self.owner, '你好')
        self.assertNotIn('speak.py', self.front.requests[-1].instructions)
        self.enable_voice()
        self.cfg['memory'] = {'enabled': False}
        await self.bridge.chat(self.owner, '你好')
        req = self.front.requests[-1]
        self.assertIn('speak.py', req.instructions)
        self.assertIn(prompts.SPEAK_RULES, req.instructions)
        self.assertIn('speak.py', req.runtime_instructions)
        self.assertNotIn('memory_search.py', req.instructions)
        self.assertIsNotNone(req.on_activity)  # Speaking stays out of progress narration.
        self.assertFalse(server.grants)
        await self.bridge.chat(self.guest, '你好')
        self.assertNotIn('speak.py', self.front.requests[-1].instructions)
        await self.bridge.voice.close()

    async def test_group_guest_voice_is_scoped_and_revoked_when_disabled(self):
        server = await self.setup_tools()
        self.full_codex()
        self.enable_voice()
        self.cfg['voice']['group_enabled'] = True
        guest = Source('group', 20001, 30001)
        async with server.grant(guest, names=('speak',)) as grant:
            token = self.token(grant)
            self.assertEqual([x['name'] for x in await server.call(token, 'tools/list', {})], ['speak'])
            await server.call(token, 'speak', {'text': LINE, 'style': '日常'})
            for name in ('memory_search', 'dispatch_task', 'poke'):
                with self.assertRaises(ValueError):
                    await server.call(token, name, {})
            self.cfg['voice']['group_enabled'] = False
            with self.assertRaises(ValueError):
                await server.call(token, 'speak', {'text': LINE, 'style': '日常'})
        self.cfg['voice']['group_enabled'] = True
        for names in (('memory_search',), ('speak', 'memory_search')):
            with self.assertRaises(ValueError):
                async with server.grant(guest, names=names): pass
        with self.assertRaises(ValueError):
            async with server.grant(Source('private', 30001, 30001), names=('speak',)): pass
        await self.bridge.chat(guest, '说句话')
        req = self.front.requests[-1]
        self.assertEqual(req.level, NONE)
        self.assertEqual([t['name'] for t in req.dynamic_tools], ['speak'])
        self.assertNotIn('--grant', req.runtime_instructions)
        self.assertNotIn('memory_search.py', req.runtime_instructions)
        self.assertNotIn('只陪对方文字聊天：不调用工具', req.instructions)
        await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(self.audio, [guest])
        self.assertFalse(server.grants)
        await self.bridge.voice.close()

    async def test_tool_mode_does_not_voice_every_reply(self):
        submitted = []

        class Voice:
            def submit(self, source, reply):
                submitted.append(reply)
                return True
        self.bridge.voice = Voice()
        self.cfg['voice'] = {'enabled': True, 'mode': 'reply'}
        await Bridge.enqueue_voice(self.bridge, self.owner, '第一条')
        self.cfg['voice']['mode'] = 'tool'
        await Bridge.enqueue_voice(self.bridge, self.owner, '第二条')
        self.assertEqual(submitted, ['第一条'])


class SpeakConfigTests(unittest.TestCase):
    def test_unknown_voice_mode_is_rejected_at_startup(self):
        with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
            with self.assertRaisesRegex(ValueError, 'voice.mode'):
                Bridge({'owner_qq': 1, 'codex_path': 'unused', 'voice': {'mode': 'always'}})

    def test_direct_speak_command_is_quiet_like_memory(self):
        command = shlex.join(['python3', '-I', str(SPEAK_SCRIPT), '--grant', '/private/g.json',
                              '--style', '开心', '--intensity', '中', '--text', LINE])
        self.assertTrue(speak_command(command))
        self.assertTrue(speak_command(shlex.join(['/bin/bash', '-lc', command])))
        with_scene = shlex.join(['python3', '-I', str(SPEAK_SCRIPT), '--grant', '/private/g.json', '--style', '开心',
                                 '--intensity', '中', '--scene', '出发前雀跃', '--text', LINE])
        self.assertTrue(speak_command(with_scene))
        for value in (command.replace(str(SPEAK_SCRIPT), '/tmp/speak.py'), command + ' --root /',
                      'echo speak.py', None):
            self.assertFalse(speak_command(value))
        sent = []
        gate = MemoryProgress(sent.append)
        gate.progress('我来说一句')
        gate.activity({'type': 'commandExecution', 'command': command})
        gate.progress('说完了')
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, warm_reply, timeout=5):
        encoded = __import__('base64').b64encode(wav_bytes()).decode()
        script = ('import sys,json\n'
                  'for line in sys.stdin:\n'
                  ' request=json.loads(line)\n'
                  ' if request.get("warmup"):\n'
                  f'  reply={warm_reply!r}\n'
                  '  if reply is None: continue\n'
                  '  print(reply,flush=True); continue\n'
                  f' print(json.dumps({{"wav": {encoded!r}}}),flush=True)\n')
        return ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=timeout)

    async def test_ready_unsupported_and_silent_backends(self):
        ready = self.backend('{"ready": true}')
        try:
            self.assertTrue(await ready.warm())
            process = ready.process
            self.assertEqual(await ready.synthesize(LINE), wav_bytes())
            self.assertIs(ready.process, process)  # the warmed process serves the first line
        finally:
            await ready.close()
        legacy = self.backend('{"error": "synthesis_failed"}')
        try:
            self.assertFalse(await legacy.warm())
            self.assertIsNotNone(legacy.process)  # answered, so it stays up
            self.assertEqual(await legacy.synthesize(LINE), wav_bytes())
        finally:
            await legacy.close()
        silent = self.backend(None, timeout=0.5)
        with self.assertLogs(level='WARNING'):
            self.assertFalse(await silent.warm())
        self.assertIsNone(silent.process)

    async def test_close_cancels_pending_warmup(self):
        started = asyncio.Event()

        class Synth:
            async def warm(self):
                started.set()
                await asyncio.sleep(60)

            async def close(self):
                self.closed = True

        async def send(source, value): pass
        synth = Synth()
        worker = VoiceReplies(synth, send, send, lambda source: False)
        worker.warm()
        first = worker.warming
        worker.warm()
        self.assertIs(worker.warming, first)  # no second load while one is running
        await asyncio.wait_for(started.wait(), 1)
        await worker.close()
        self.assertTrue(first.cancelled())
        self.assertTrue(synth.closed)

    async def test_each_connection_warms_unless_disabled(self):
        with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
            bridge = Bridge({'owner_qq': 1, 'codex_path': 'unused'})
            calls = []

            class Voice:
                def warm(self): calls.append('warm')
                async def close(self): calls.append('close')

            class Socket:
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration

            async def idle(): await asyncio.sleep(60)
            bridge.voice, bridge.worker = Voice(), idle
            try:
                await bridge.session(Socket())
                await bridge.session(Socket())
                self.assertEqual(calls, ['warm', 'close', 'warm', 'close'])
                bridge.cfg['voice'] = {'preload': False}
                calls.clear()
                await bridge.session(Socket())
                self.assertEqual(calls, ['close'])
            finally:
                bridge.db.close()

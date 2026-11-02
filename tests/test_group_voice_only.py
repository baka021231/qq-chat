"""Group-only automatic speech, bounded queue, and original-text fallback."""
import asyncio
import contextlib
import unittest

from qq_chat.bridge import Source, Incoming
from qq_chat.voice import VoiceReplies
from tests import test_agents as agent_fixtures
from tests.test_voice import wav_bytes


class GroupVoiceOnlyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = agent_fixtures.AgentBridgeTests.asyncSetUp

    def enable(self, synth=None, send_audio=None, capacity=8):
        self.audio = []
        self.bridge.cfg['voice'] = {'enabled': True, 'mode': 'tool',
                                    'group_enabled': True, 'group_voice_only': True}
        class Synth:
            async def synthesize(self, text, **options): return wav_bytes()
            async def close(self): pass
        async def send(source, audio): self.audio.append(source)
        self.bridge.voice = VoiceReplies(synth or Synth(), send_audio or send,
                                        self.bridge.send, self.bridge.blocked, capacity=capacity)
        self.addAsyncCleanup(self.bridge.voice.close)

    async def test_owner_and_guest_group_reply_is_audio_only_and_has_no_speak(self):
        self.enable()
        for source in [Source('group', 20001, 30001), Source('group', 20001, 10001)]:
            self.assertTrue(self.bridge.group_voice_only(source))
            self.assertFalse(self.bridge.group_speak_enabled(source))
            reply, streamed = await self.bridge.chat(source, '你好')
            req = self.fake.requests[-1]
            self.assertFalse(streamed)
            self.assertIsNone(req.on_answer)
            self.assertIsNone(req.on_progress)
            self.assertEqual(req.dynamic_tools, ())
            self.assertIsNone(req.on_tool_call)
            self.assertIn('自动语音回复', req.runtime_instructions)
            await self.bridge.deliver(source, reply, '你好', streamed=streamed)
            await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
            self.assertEqual(self.sent, [])
        self.assertEqual(len(self.audio), 2)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM history WHERE role='assistant'").fetchone()[0], 2)

    async def test_private_streaming_and_no_voice_mode_are_unchanged(self):
        self.enable()
        reply, streamed = await self.bridge.chat(self.owner, '你好')
        self.assertTrue(streamed)
        self.assertIsNotNone(self.fake.requests[-1].on_answer)
        await self.bridge.deliver(self.owner, reply, '你好', streamed=streamed)
        self.assertEqual(self.sent, [(self.owner, reply)])
        self.assertEqual(self.audio, [])
        voice = self.bridge.voice
        self.bridge.voice = None
        guest = Source('group', 20001, 30001)
        self.assertFalse(self.bridge.group_voice_only(guest))
        await self.bridge.deliver(guest, '文字还在', '你好')
        self.assertEqual(self.sent[-1], (guest, '文字还在'))
        self.bridge.voice = voice

    async def test_synthesis_or_audio_send_failure_falls_back_to_exact_original(self):
        for failure_stage in ['synthesis', 'delivery']:
            with self.subTest(stage=failure_stage):
                class Synth:
                    async def synthesize(inner, text, **options):
                        if failure_stage == 'synthesis': raise RuntimeError('backend detail')
                        return wav_bytes()
                    async def close(inner): pass
                async def send(source, audio): raise RuntimeError('QQ rejected')
                self.enable(synth=Synth(), send_audio=send)
                source = Source('group', 20001, 30001)
                text = '这是原文，不是失败提示。'
                self.sent.clear()
                await self.bridge.deliver(source, text, 'question')
                await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
                self.assertEqual(self.sent, [(source, text)])
                await self.bridge.voice.close()

    async def test_full_queue_falls_back_without_enqueuing_duplicate_audio(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Synth:
            async def synthesize(inner, text, **options):
                entered.set(); await release.wait(); return wav_bytes()
            async def close(inner): pass
        self.enable(synth=Synth(), capacity=1)
        source = Source('group', 20001, 30001)
        await self.bridge.deliver(source, '第一句', 'question')
        await entered.wait()
        await self.bridge.deliver(source, '第二句', 'question')
        await self.bridge.deliver(source, '第三句', 'question')
        self.assertEqual(self.sent, [(source, '第三句')])
        release.set()
        await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(self.audio, [source, source])

    async def test_management_command_still_uses_text(self):
        self.enable()
        worker = asyncio.create_task(self.bridge.worker())
        try:
            await self.bridge.queue.put(Incoming(Source('group', 20001, 30001), '/ping'))
            await asyncio.wait_for(self.bridge.queue.join(), 2)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.audio, [])
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError): await worker

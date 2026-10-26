"""Optional speech delivery, independent of Codex and OneBot transport.

The synthesizer is a persistent JSON-lines subprocess in its own environment.
Only this module knows its protocol; only the bridge knows QQ destinations.
"""
import asyncio
import base64
import contextlib
import io
import json
import logging
import re
import wave

MAX_AUDIO_BYTES = 8 * 1024 * 1024


def speech_text(text):
    """Keep prose, with spoken placeholders for code and URLs."""
    text = re.sub(r'```[\s\S]*?```|~~~[\s\S]*?~~~', '，代码见文字消息。', text)
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1，链接见文字消息', text)
    text = re.sub(r'https?://[^\s<>]+', '链接见文字消息', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*+]\s+)', '', text)
    text = text.replace('`', '').replace('**', '').replace('~', '。')
    return re.sub(r'\s+', ' ', text).strip()


def speech_chunks(text, limit=140):
    while text:
        end = min(len(text), limit)
        if end < len(text):
            boundary = max(text.rfind(c, 0, end) for c in '。！？；，.!?; ')
            if boundary >= end // 2:
                end = boundary + 1
        chunk, text = text[:end].strip(), text[end:].lstrip()
        if chunk:
            yield chunk


def validate_wav(data):
    if not isinstance(data, bytes) or not 44 < len(data) <= MAX_AUDIO_BYTES:
        raise ValueError('Invalid speech audio size')
    with wave.open(io.BytesIO(data), 'rb') as audio:
        if (audio.getcomptype() != 'NONE' or audio.getnchannels() != 1
                or audio.getsampwidth() != 2 or audio.getframerate() != 32000
                or not 0 < audio.getnframes() <= 32000 * 120):
            raise ValueError('Invalid speech WAV format or duration')
        if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
            raise ValueError('Truncated speech audio')


class ProcessSynthesizer:
    """One request at a time. Kill on timeout to avoid stale response reuse."""
    def __init__(self, command, timeout=120):
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise ValueError('voice.command must be an argv list')
        self.command, self.timeout = command, timeout
        self.process = None
        self.lock = asyncio.Lock()

    async def synthesize(self, text):
        async with self.lock:
            try:
                async with asyncio.timeout(self.timeout):
                    if self.process is None or self.process.returncode is not None:
                        self.process = await asyncio.create_subprocess_exec(
                            *self.command, stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                            limit=MAX_AUDIO_BYTES * 2)
                    self.process.stdin.write((json.dumps({'text': text}, ensure_ascii=False) + '\n').encode())
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                    if len(line) > MAX_AUDIO_BYTES * 2:
                        raise ValueError('Speech response too large')
                    result = json.loads(line)
                    audio = base64.b64decode(result['wav'], validate=True)
                    validate_wav(audio)
                    return audio
            except BaseException:
                await self.close()
                raise

    async def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.wait()


class VoiceReplies:
    """Bounded FIFO: speech never blocks text generation or message reception."""
    def __init__(self, synthesizer, send_audio, send_notice, blocked, capacity=8):
        self.synthesizer = synthesizer
        self.send_audio, self.send_notice, self.blocked = send_audio, send_notice, blocked
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None

    def submit(self, source, reply):
        text = speech_text(reply)
        if not text or self.blocked(source):
            return True
        try:
            self.queue.put_nowait((source, text))
        except asyncio.QueueFull:
            return False
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        return True

    async def run(self):
        while True:
            source, text = await self.queue.get()
            try:
                for chunk in speech_chunks(text):
                    if self.blocked(source):
                        break
                    audio = await self.synthesizer.synthesize(chunk)
                    if self.blocked(source):
                        break
                    await self.send_audio(source, audio)
            except Exception as error:
                # No text/audio payloads, paths or backend errors in shared logs.
                logging.warning('Voice reply failed (%s); text retained', type(error).__name__)
                if not self.blocked(source):
                    with contextlib.suppress(Exception):
                        await self.send_notice(source, '本次语音未能完成，文字回复已保留。')
            finally:
                self.queue.task_done()

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        await self.synthesizer.close()

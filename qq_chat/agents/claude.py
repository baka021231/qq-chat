"""Claude Code JSONL adapter with per-level isolation and gated text delivery."""
import asyncio
import base64
from collections import defaultdict
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
import time
import uuid

from .base import Agent, CHAT, FULL, MAX_REPLY, MissingSession, NONE

BASE_ARGS = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
             '--verbose', '--include-partial-messages', '--system-prompt-snapshot', 'off',
             '--setting-sources', 'project', '--disable-slash-commands', '--no-chrome',
             '--permission-prompts', 'none']
ISOLATION_ENV = ('CLAUDE_CODE_DISABLE_CLAUDE_MDS', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY')
CALL_MARKERS = re.compile(r'<\s*(?:invoke|system-reminder)', re.IGNORECASE)


class UnexpectedTools(RuntimeError):
    pass


def user_message(text, images):
    content = [{'type': 'text', 'text': text}]
    for image in images:
        path = Path(image)
        data = path.read_bytes()
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            mime = 'image/gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            mime = 'image/webp'
        else:
            raise ValueError('Unsupported Claude image format')
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                                                    'data': base64.b64encode(data).decode('ascii')}})
    return {'type': 'user', 'message': {'role': 'user', 'content': content}}


async def terminate(process):
    if process.returncode is not None:
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            await asyncio.wait_for(process.wait(), 5)
            return
        except asyncio.TimeoutError:
            continue
    raise RuntimeError('Claude process did not terminate')


class ClaudeCodeAgent(Agent):
    def __init__(self, db, cwd, profile, name, runtime):
        self.name, self.db, self.cwd = name, db, Path(cwd)
        self.profile = dict(profile)
        self.runtime = Path(runtime) / name
        self._fresh, self._state, self._active = set(), {}, {}
        self._locks = defaultdict(asyncio.Lock)

    async def start(self):
        if shutil.which(self.profile['path']) is None:
            raise FileNotFoundError('Claude executable is unavailable')
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runtime.is_symlink():
            raise ValueError('Claude runtime must not be a symlink')
        self.runtime.chmod(0o700)

    def session_id(self, key):
        row = self.db.execute('SELECT session_id FROM agent_sessions WHERE conversation=? AND profile=?',
                              (key, self.name)).fetchone()
        return row[0] if row else None

    def model_state(self, session_id):
        return self._state.get(session_id, (self.profile.get('model'), self.profile.get('effort')))

    async def prepare(self, req):
        if req.role != CHAT or req.level not in (NONE, FULL):
            raise ValueError('Claude FRONT/TASK modes are not implemented in PR2')
        session = self.session_id(req.key)
        if session is None:
            session = str(uuid.uuid4())
            self.db.execute('INSERT INTO agent_sessions VALUES (?, ?, ?)', (req.key, self.name, session))
            self.db.commit()
            self._fresh.add(session)
        return session

    async def reset(self, key):
        self.db.execute('INSERT INTO old_agent_sessions SELECT conversation, profile, session_id, ? '
                        'FROM agent_sessions WHERE conversation=? AND profile=?', (time.time(), key, self.name))
        self.db.execute('DELETE FROM agent_sessions WHERE conversation=? AND profile=?', (key, self.name))

    async def models(self):
        result = []
        for item in self.profile['models']:
            info = {'model': item} if isinstance(item, str) else dict(item)
            info.setdefault('defaultReasoningEffort', self.profile.get('effort'))
            info.setdefault('supportedReasoningEfforts', [])
            info.setdefault('inputModalities', ['text', 'image'])
            result.append(info)
        return result

    async def default_model(self):
        return self.profile['model']

    async def close(self):
        for process in list(self._active.values()):
            await terminate(process)

    async def run(self, req):
        async with self._locks[req.key]:
            return await self._run(req)

    async def _run(self, req):
        await self.start()
        if req.role != CHAT or req.level not in (NONE, FULL):
            raise ValueError('Claude FRONT/TASK modes are not implemented in PR2')
        if req.key in self._active:
            raise RuntimeError('Claude session already has an active turn')
        session = req.session_id or await self.prepare(req)
        fresh = session in self._fresh
        fd, prompt = tempfile.mkstemp(prefix='prompt-', suffix='.md', dir=self.runtime)
        process, stderr_reader = None, None
        try:
            with os.fdopen(fd, 'w') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(req.instructions)
            args = [self.profile['path'], *BASE_ARGS]
            env = os.environ.copy()
            if req.level == NONE:
                args += ['--safe-mode', '--tools', '', '--strict-mcp-config',
                         '--disallowedTools', 'mcp__*', '--permission-mode', 'dontAsk',
                         '--system-prompt-file', prompt]
                env.update({key: '1' for key in ISOLATION_ENV})
                cwd = self.runtime
            else:
                args += ['--tools', self.profile.get('tools', 'default'),
                         '--permission-mode', self.profile['permission_mode'],
                         '--append-system-prompt-file', prompt]
                cwd = self.cwd
            args += ['--session-id' if fresh else '--resume', session,
                     '--max-turns', str(self.profile.get('max_turns', 6))]
            model = req.model or self.profile.get('model')
            effort = req.effort if req.effort is not None else self.profile.get('effort')
            if model:
                args += ['--model', model]
            if effort:
                args += ['--effort', effort]
            payload = json.dumps(user_message(req.text, req.images), ensure_ascii=False) + '\n'
            async with asyncio.timeout(self.profile.get('timeout_seconds', 3600)):
                process = await asyncio.create_subprocess_exec(
                    *args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=16 * 1024 * 1024, start_new_session=True)
                self._active[req.key] = process

                async def drain_stderr():
                    prefix = bytearray()
                    while chunk := await process.stderr.read(8192):
                        if len(prefix) < 16384:
                            prefix.extend(chunk[:16384 - len(prefix)])
                    return prefix.decode(errors='replace')

                stderr_reader = asyncio.create_task(drain_stderr())
                process.stdin.write(payload.encode())
                await process.stdin.drain()
                process.stdin.close()
                initialized, result = False, None
                messages, streams = {}, {}
                current = None
                async for line in process.stdout:
                    event = json.loads(line)
                    kind = event.get('type')
                    if kind == 'system' and event.get('subtype') == 'init':
                        tools = event.get('tools')
                        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                            raise UnexpectedTools('Missing or invalid Claude tool list')
                        if (req.level == NONE and tools) or any(t.startswith('mcp__qq__') for t in tools):
                            raise UnexpectedTools('Claude tools violate the current permission level')
                        initialized = True
                        self._fresh.discard(session)
                        self._state[session] = event.get('model', model), effort
                    elif kind in ('assistant', 'stream_event'):
                        if not initialized:
                            raise UnexpectedTools('Claude output arrived before tool validation')
                        if event.get('parent_tool_use_id') is not None:
                            continue
                        if kind == 'assistant':
                            message = event.get('message', {})
                            parts = [b.get('text', '') for b in message.get('content', []) if b.get('type') == 'text']
                            if parts:
                                messages[message.get('id', 'assistant')] = '\n'.join(parts)
                        else:
                            inner = event.get('event', {})
                            if inner.get('type') == 'message_start':
                                current = inner.get('message', {}).get('id')
                            delta = inner.get('delta', {})
                            if inner.get('type') == 'content_block_start' and inner.get('content_block', {}).get('type') == 'tool_use':
                                name = inner['content_block'].get('name', '')
                                if req.level == NONE or name.startswith('mcp__qq__'):
                                    raise UnexpectedTools('Unexpected runtime tool call')
                                if req.on_progress:
                                    req.on_progress('正在使用工具')
                            if inner.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                                if current is None:
                                    raise RuntimeError('Claude delta has no message')
                                streams[current] = streams.get(current, '') + delta.get('text', '')
                    elif kind == 'result':
                        result = event
                    elif kind == 'rate_limit_event':
                        logging.info('Claude rate limit event profile=%s', self.name)
                code = await process.wait()
                stderr = await stderr_reader
            error_text = stderr + json.dumps(result.get('errors', []) if result else [])
            if not fresh and not initialized and 'No conversation found with session ID' in error_text:
                raise MissingSession('Claude session is missing')
            if code or not initialized or not result or result.get('is_error') or result.get('subtype') != 'success':
                raise RuntimeError('Claude turn failed; inspect login, model availability and limits')
            # Completed messages are authoritative; streams fill any text-only gaps.
            for ident, text in streams.items():
                messages.setdefault(ident, text)
            reply = '\n\n'.join(messages.values()).strip() or result.get('result', '').strip()
            if not reply or CALL_MARKERS.search(reply):
                raise RuntimeError('Claude reply is empty or contains simulated invocation markers')
            req.history_text = reply
            logging.info('Claude usage profile=%s usage=%s total_cost_usd=%s',
                         self.name, json.dumps(result.get('usage', {})), result.get('total_cost_usd'))
            # Replay only validated text. No prefix reaches QQ before the whole-turn gate.
            if req.on_answer:
                req.on_answer(reply)
            return reply[:MAX_REPLY]
        finally:
            if process is not None:
                await terminate(process)
            if stderr_reader is not None:
                stderr_reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_reader
            self._active.pop(req.key, None)
            Path(prompt).unlink(missing_ok=True)

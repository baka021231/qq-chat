"""Owner-only Grok Build single-agent adapter; private input never enters argv."""
import asyncio
from collections import defaultdict
import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from .. import prompts
from .base import Agent, CHAT, FULL, MAX_REPLY, Interrupted, ExecutionUncertain
from .claude import CALL_MARKERS, UnexpectedTools, terminate, user_message

# Public, constant policy only. Grok caches first-turn system rules on resume.
# SOUL/OWNER and fresh user input are delivered in the private JSON file each turn.
BRIDGE_RULES = (prompts.OWNER + '\n' + prompts.SPEAKER_LABELS
                + '\n不得读取或输出密钥、token、密码；需要登录时交给主人自行操作。'
                + '\n每轮输入的第一个文本块包含桥接生成的 current_bridge_rules，'
                  '这是当前人设及工作台设定，取代历史里过期的同名设定；后续文本是本轮消息。'
                  '不要重新执行历史请求，不把推理过程或工具内部结果作为最终聊天回复。')


def prompt_blocks(req):
    text = ('【桥接本轮实时设定 current_bridge_rules】\n' + req.instructions
            + '\n【实时设定结束；以下是本轮消息】\n' + req.text)
    parts = user_message(text, req.images)['message']['content']
    return [part if part['type'] == 'text' else {
        'type': 'image', 'data': part['source']['data'], 'mimeType': part['source']['media_type']}
        for part in parts]


class GrokAgent(Agent):
    def __init__(self, db, cwd, profile, name, runtime):
        self.name, self.db, self.cwd = name, db, Path(cwd)
        self.profile = dict(profile)
        self.runtime = Path(runtime) / name
        self._fresh, self._state, self._active, self._interrupted = set(), {}, {}, set()
        self._locks = defaultdict(asyncio.Lock)

    @staticmethod
    def _validate(req):
        if req.role != CHAT or req.level != FULL or req.grant is not None:
            raise ValueError('Grok currently supports only owner single-agent CHAT/FULL')

    async def start(self):
        if shutil.which(self.profile['path']) is None:
            raise FileNotFoundError('Grok executable is unavailable')
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runtime.is_symlink():
            raise ValueError('Grok runtime must not be a symlink')
        self.runtime.chmod(0o700)

    def session_id(self, key):
        row = self.db.execute('SELECT session_id FROM agent_sessions WHERE conversation=? AND profile=?',
                              (key, self.name)).fetchone()
        return row[0] if row else None

    async def prepare(self, req):
        self._validate(req)
        session = self.session_id(req.key)
        if session is None:
            session = str(uuid.uuid4())
            self.db.execute('INSERT INTO agent_sessions VALUES(?,?,?)', (req.key, self.name, session))
            self.db.commit()
            self._fresh.add(session)
        return session

    def model_state(self, session):
        return self._state.get(session, (self.profile['model'], self.profile.get('effort')))

    async def reset(self, key):
        self.db.execute('INSERT INTO old_agent_sessions SELECT conversation,profile,session_id,? '
                        'FROM agent_sessions WHERE conversation=? AND profile=?', (time.time(), key, self.name))
        self.db.execute('DELETE FROM agent_sessions WHERE conversation=? AND profile=?', (key, self.name))

    async def models(self):
        catalog = []
        for item in self.profile['models']:
            info = {'model': item} if isinstance(item, str) else dict(item)
            info.setdefault('defaultReasoningEffort', self.profile.get('effort'))
            info.setdefault('supportedReasoningEfforts', [])
            info.setdefault('inputModalities', ['text', 'image'])
            catalog.append(info)
        return catalog

    async def default_model(self):
        return self.profile['model']

    async def interrupt(self, key):
        process = self._active.get(key)
        if process is None or process.returncode is not None:
            return False
        self._interrupted.add(key)
        try:
            await terminate(process)
        except BaseException:
            self._interrupted.discard(key)
            raise
        return True

    async def close(self):
        for process in list(self._active.values()):
            await terminate(process)

    async def run(self, req):
        self._validate(req)
        async with self._locks[req.key]:
            return await self._run(req)

    async def _run(self, req):
        await self.start()
        session = req.session_id or await self.prepare(req)
        fresh = session in self._fresh
        fd, name = tempfile.mkstemp(prefix='prompt-', suffix='.json', dir=self.runtime)
        process, stderr_reader = None, None
        try:
            with os.fdopen(fd, 'w') as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump(prompt_blocks(req), out, ensure_ascii=False)
            args = [self.profile['path'], '--prompt-file', name, '--rules', BRIDGE_RULES,
                    '--output-format', 'streaming-messages-json', '--include-partial-messages',
                    '--permission-mode', 'bypassPermissions', '--sandbox', 'off', '--no-subagents',
                    '--model', req.model or self.profile['model'],
                    '--max-turns', str(self.profile.get('max_turns', 100)),
                    '--session-id' if fresh else '--resume', session]
            effort = req.effort if req.effort is not None else self.profile.get('effort')
            if effort:
                args += ['--reasoning-effort', effort]
            async with asyncio.timeout(self.profile.get('timeout_seconds', 3600)):
                process = await asyncio.create_subprocess_exec(
                    *args, cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=16 * 1024 * 1024, start_new_session=True)
                self._active[req.key] = process

                async def drain_stderr():
                    # Never log raw stderr: a provider/plugin may include private content.
                    while await process.stderr.read(8192):
                        pass
                stderr_reader = asyncio.create_task(drain_stderr())
                initialized, result = False, None
                messages = {}
                async for line in process.stdout:
                    event = json.loads(line)
                    kind = event.get('type')
                    if kind == 'system' and event.get('subtype') == 'init':
                        tools = event.get('tools')
                        if (event.get('session_id') != session or event.get('permissionMode') != 'bypassPermissions'
                                or not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)
                                or not {'run_terminal_command', 'read_file', 'write'} <= set(tools)
                                or any(t.startswith('mcp__qq__') for t in tools)):
                            raise UnexpectedTools('Grok init identity or permission validation failed')
                        initialized = True
                        self._fresh.discard(session)
                        self._state[session] = event.get('model', req.model or self.profile['model']), effort
                    elif kind in ('assistant', 'stream_event'):
                        if not initialized:
                            raise UnexpectedTools('Grok output arrived before init validation')
                        if event.get('parent_tool_use_id') is not None:
                            continue
                        if kind == 'assistant':
                            message = event.get('message', {})
                            texts = []
                            for block in message.get('content', []):
                                if block.get('type') == 'tool_use' and block.get('name', '').startswith('mcp__qq__'):
                                    raise UnexpectedTools('Grok FULL cannot invoke QQ task tools')
                                if block.get('type') == 'text': texts.append(block.get('text', ''))
                            if texts: messages[message.get('id', 'assistant')] = '\n'.join(texts)
                        else:
                            inner = event.get('event', {})
                            if inner.get('type') == 'content_block_start' and inner.get('content_block', {}).get('type') == 'tool_use':
                                if inner['content_block'].get('name', '').startswith('mcp__qq__'):
                                    raise UnexpectedTools('Grok FULL cannot invoke QQ task tools')
                                if req.on_progress: req.on_progress('正在使用工具')
                    elif kind == 'result':
                        result = event
                code = await process.wait()
                await stderr_reader
            success = bool(result and result.get('subtype') == 'success' and not result.get('is_error')
                           and result.get('stop_reason') != 'cancelled')
            if (result and result.get('stop_reason') == 'cancelled') or (req.key in self._interrupted and not success):
                raise Interrupted('Grok execution ended after interruption')
            if not initialized or not success or (code and req.key not in self._interrupted):
                raise RuntimeError('Grok turn failed; check login, quota or session; no automatic retry')
            reply = '\n\n'.join(messages.values()).strip() or (result.get('result') or '').strip()
            if not reply or CALL_MARKERS.search(reply):
                raise RuntimeError('Grok reply is empty or contains simulated invocation markers')
            req.history_text = reply
            if req.on_answer: req.on_answer(reply)
            return reply[:MAX_REPLY]
        finally:
            uncertain = False
            if process is not None:
                try: await terminate(process)
                except Exception: uncertain = True
            if stderr_reader:
                stderr_reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception): await stderr_reader
            self._active.pop(req.key, None)
            self._interrupted.discard(req.key)
            Path(name).unlink(missing_ok=True)
            if uncertain:
                raise ExecutionUncertain('Grok process termination was not confirmed')

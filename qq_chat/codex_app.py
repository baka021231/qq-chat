"""Client of the shared local Codex daemon over its private Unix socket."""
import asyncio
import contextlib
import json
import logging
import re
import shlex
import os
from pathlib import Path

from websockets.asyncio.client import unix_connect
from .agents.base import Interrupted, ExecutionUncertain

class AppServer:
    def __init__(self, executable):
        self.executable = executable
        self.ws = None
        self.reader = None
        self.closing = False
        self.pending = {}
        self.events = {}
        self.active_turns = {}
        self.serial = 0
        self.ready = asyncio.Lock()

    async def start(self):
        async with self.ready:
            if self.reader and not self.reader.done():
                return
            if self.ws:
                await self.ws.close()
            self.closing = False
            starter = await asyncio.create_subprocess_exec(
                self.executable, 'app-server', 'daemon', 'start',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(starter.wait(), 30)
            except BaseException:
                if starter.returncode is None:
                    starter.kill()
                    await starter.wait()
                raise
            if starter.returncode:
                raise RuntimeError('Unable to start shared Codex daemon')
            codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
            socket = codex_home / 'app-server-control' / 'app-server-control.sock'
            self.ws = await unix_connect(str(socket.resolve()), open_timeout=15,
                                         close_timeout=2, max_size=8*1024*1024, proxy=None)
            self.reader = asyncio.create_task(self.read())
            try:
                await self.call('initialize', {'clientInfo': {'name': 'qq_bridge', 'version': '2.1'}, 'capabilities': {'experimentalApi': False}})
                await self.write({'method': 'initialized'})
            except BaseException:
                await self.close()
                raise

    async def write(self, message):
        await self.ws.send(json.dumps(message, ensure_ascii=False))

    async def call(self, method, params):
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.write({'id': ident, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(ident, None)

    async def read(self):
        try:
            async for line in self.ws:
                msg = json.loads(line)
                if 'method' in msg:
                    if 'id' in msg:
                        # Never silently approve unexpected interactive requests.
                        await self.write({'id': msg['id'], 'error': {'code': -32601, 'message': 'QQ bridge does not support interactive approval; use the terminal'}})
                        continue
                    params = msg.get('params', {})
                    thread = params.get('threadId')
                    if thread in self.events:
                        self.events[thread].put_nowait(msg)
                    if msg['method'] in ('thread/tokenUsage/updated', 'thread/compacted'):
                        logging.info('Codex usage event %s %s', msg['method'], json.dumps(params))
                elif msg.get('id') in self.pending:
                    f = self.pending[msg['id']]
                    if not f.done():
                        if 'error' in msg:
                            f.set_exception(RuntimeError(str(msg['error'])))
                        else:
                            f.set_result(msg.get('result'))
        except Exception:
            if not self.closing:
                logging.exception('App Server reader failed')
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError('App Server disconnected'))
            for q in self.events.values():
                q.put_nowait({'method': 'disconnected'})

    async def turn(self, thread, text, on_progress=None, on_answer=None, show_tools=False, image_paths=(), model=None, effort=None):
        q = asyncio.Queue()
        self.events[thread] = q
        turn_id = None
        ended = False
        try:
            inputs = [{'type': 'text', 'text': text}]
            inputs.extend({'type': 'localImage', 'path': str(path)} for path in image_paths)
            params = {'threadId': thread, 'input': inputs}
            if model is not None:
                params['model'] = model
            if effort is not None:
                params['effort'] = effort
            result = await self.call('turn/start', params)
            turn_id = result['turn']['id']
            self.active_turns[thread] = turn_id
            messages = {}
            phases, streamed = {}, {}
            async with asyncio.timeout(3600):
                while True:
                    event = await q.get()
                    method, params = event['method'], event.get('params', {})
                    if method == 'disconnected':
                        raise ExecutionUncertain('App Server disconnected during turn')
                    if on_progress:
                        progress = progress_text(method, params, show_tools=show_tools)
                        if progress:
                            on_progress(progress)
                    if method == 'item/started':
                        item = params.get('item', {})
                        if item.get('type') == 'agentMessage':
                            phases[item['id']] = item.get('phase')
                    if method == 'item/agentMessage/delta' and on_answer:
                        ident = params.get('itemId')
                        if phases.get(ident) == 'final_answer':
                            delta = params.get('delta', '')
                            streamed[ident] = streamed.get(ident, '') + delta
                            on_answer(delta)
                    if method == 'item/completed':
                        item = params.get('item', {})
                        if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                            messages[item['id']] = item.get('text', '')
                            if on_answer:
                                prefix = streamed.get(item['id'], '')
                                if item.get('text', '').startswith(prefix):
                                    on_answer(item.get('text', '')[len(prefix):])
                                else:
                                    raise RuntimeError('Streamed answer differs from completed message')
                    if method == 'turn/completed' and params['turn']['id'] == turn_id:
                        turn = params['turn']
                        ended = True
                        if turn['status'] == 'interrupted':
                            raise Interrupted('Codex turn interrupted')
                        if turn['status'] != 'completed':
                            raise RuntimeError('Codex turn failed: '+str(turn.get('error')))
                        reply = '\n\n'.join(messages.values()).strip()
                        if not reply:
                            raise RuntimeError('Codex returned empty reply')
                        return reply
        except BaseException as error:
            if turn_id and not ended:
                with contextlib.suppress(Exception):
                    await self.call('turn/interrupt', {'threadId': thread, 'turnId': turn_id})
            if not ended and not isinstance(error, (asyncio.CancelledError, Interrupted)):
                raise ExecutionUncertain('Codex execution outcome is unconfirmed') from error
            raise
        finally:
            self.events.pop(thread, None)
            self.active_turns.pop(thread, None)

    async def interrupt(self, thread):
        turn = self.active_turns.get(thread)
        if turn is None:
            return False
        await self.call('turn/interrupt', {'threadId': thread, 'turnId': turn})
        return True

    async def close(self):
        # Disconnect this client only; the daemon also serves terminal sessions.
        self.closing = True
        if self.ws:
            await self.ws.close()
        if self.reader:
            await self.reader
        self.ws = None
        self.reader = None


def command_summary(command):
    """Show command structure, never arbitrary argument values or inline scripts."""
    try:
        words = shlex.split(command.splitlines()[0])
    except (ValueError, IndexError):
        return '执行命令'
    while words and '=' in words[0]:
        words.pop(0)
    if not words:
        return '执行命令'
    program = Path(words[0]).name
    if not re.fullmatch(r'[\w.+-]{1,40}', program):
        return '执行命令'
    if program in ('bash', 'sh', 'zsh', 'env', 'sudo'):
        return program + ' 脚本'
    if program in ('cat', 'head', 'tail', 'sed', 'rg', 'ls'):
        files = [Path(w).name for w in words[1:] if re.search(r'\.(py|java|md|json|toml|txt|log|service)$', w)
                 and re.fullmatch(r'[\w./-]+', w)]
        if files:
            return program + ' · ' + '、'.join(files[:3])[:100]
    safe_commands = {'show', 'status', 'restart', 'stop', 'start', 'is-active', 'resume', 'app-server', 'diff', 'log', 'version'}
    flags = {'--user', '--no-pager', '--help', '--version', '-n', '-C', '--stat', '--check'}
    short = [program]
    for word in words[1:]:
        if word in (';', '&&', '||', '|') or '<' in word or '>' in word:
            break
        if word in safe_commands or word in flags:
            short.append(word)
        if len(short) == 4:
            break
    return ' '.join(short)


def progress_text(method, params, show_tools=False):
    """Commentary and plans by default; tool details require explicit opt-in."""
    item = params.get('item', {})
    kind = item.get('type')
    if method == 'item/completed' and kind == 'agentMessage' and item.get('phase') == 'commentary':
        return item.get('text', '').strip()[:1200]
    if method == 'turn/plan/updated':
        lines = ['计划：']
        if params.get('explanation'):
            lines.append(str(params['explanation']))
        statuses = {'pending': '待做', 'inProgress': '进行中', 'in_progress': '进行中', 'completed': '完成'}
        for step in params.get('plan', []):
            lines.append(f"・{step.get('step', '')}（{statuses.get(step.get('status'), '待做')}）")
        return '\n'.join(lines)[:1200] if len(lines) > 1 else None
    if method == 'item/completed' and kind == 'plan':
        return ('计划：\n' + item.get('text', ''))[:1200] if item.get('text') else None
    if not show_tools:
        return None
    labels = {
        'commandExecution': command_summary(item.get('command', '')),
        'webSearch': '搜索或打开网页',
        'mcpToolCall': '调用工具',
        'dynamicToolCall': '调用工具',
        'fileChange': '修改文件',
    }
    if kind == 'mcpToolCall':
        labels[kind] = '工具：' + str(item.get('tool', '未知工具'))[:80]
    if kind == 'dynamicToolCall':
        labels[kind] = '工具：' + str(item.get('tool', '未知工具'))[:80]
    if kind == 'fileChange':
        names = [Path(c.get('path', '')).name for c in item.get('changes', [])]
        labels[kind] = '修改文件：' + '、'.join(names[:3])[:100]
    if kind == 'webSearch':
        action = item.get('action') or {}
        if action.get('type') == 'search':
            query = action.get('query') or '；'.join(action.get('queries') or [])
            labels[kind] = '搜索网页：' + str(query)[:100]
        elif action.get('type') == 'openPage':
            from urllib.parse import urlsplit
            labels[kind] = '打开网页：' + urlsplit(action.get('url', '')).netloc
    if kind in labels:
        if method == 'item/started':
            return '⏳ ' + labels[kind]
        if method == 'item/completed':
            failed = item.get('status') in ('failed', 'declined') or item.get('exitCode') not in (None, 0)
            return ('⚠️ ' if failed else '✓ ') + labels[kind] + ('（未成功）' if failed else '（完成）')
    return None

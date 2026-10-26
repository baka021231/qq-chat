"""Text QQ chat bridge: NapCat OneBot 11 -> existing Linux Codex login."""
import asyncio
import contextlib
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid
import unicodedata
from . import prompts
from .codex_app import AppServer
from .files import MAX_FILE_BYTES, save_attachment
from .images import MAX_IMAGE_BYTES, save_image
from .reply import stream_bubbles
from .moderation import BLOCK_SENDER, blocked_group_sender, bounded_guest_reply, obvious_abuse, update_blacklist

from websockets.asyncio.client import connect

BASE = Path(__file__).resolve().parents[1]
CONFIG = BASE / "config.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class Source:
    kind: str
    target: int
    sender_id: int | None = None
    sender_name: str = ''

    @property
    def conversation(self):
        return f"{self.kind}:{self.target}"


@dataclass(frozen=True)
class Incoming:
    source: Source
    text: str
    attachments: tuple = ()
    context_id: int | None = None
    images: tuple = ()


def owner_request(source, owner):
    return (str(source.sender_id) == str(owner)
            and (source.kind == 'group' or (source.kind == 'private' and str(source.target) == str(owner))))


def conversation_key(source, owner):
    # Keep tool-enabled owner turns separate from other group members' history.
    if source.kind == 'group' and owner_request(source, owner):
        return source.conversation + ':owner'
    return source.conversation


def speaker_message(source, text, owner):
    # Names are display data; authority comes only from the numeric sender ID.
    name = ''.join(c if c not in '[]|' and not unicodedata.category(c).startswith('C') else ' '
                   for c in source.sender_name).strip()[:64] or str(source.sender_id)
    role = '主人' if str(source.sender_id) == str(owner) else '群友'
    scene = '私聊' if source.kind == 'private' else '群聊'
    return f'[发言人：{name} | QQ：{source.sender_id} | 身份：{role} | 场景：{scene}]\n{text}'


def incoming_text(event, owner, allowed_groups=(), allow_all_groups=False, require_trigger=True,
                  group_user_blacklist=()):
    if event.get("post_type") != "message":
        return None
    user = str(event.get("user_id", ""))
    bot = str(event.get("self_id", ""))
    if not user.isdigit() or not bot.isdigit() or int(user) == int(bot):
        return None
    kind = event.get("message_type")
    sender = event.get('sender') or {}
    name = str(sender.get('card') or sender.get('nickname') or user)
    if kind == "private":
        if user != str(owner):
            return None
        source = Source("private", int(user), int(user), name)
    elif kind == "group":
        group = str(event.get("group_id", ""))
        if not group.isdigit() or (not allow_all_groups and group not in {str(g) for g in allowed_groups}):
            return None
        source = Source("group", int(group), int(user), name)
        if blocked_group_sender(source, {'owner_qq': owner, 'group_user_blacklist': group_user_blacklist}):
            return None
    else:
        return None
    message = event.get("message", [])
    if not isinstance(message, list):
        # Request array-format messages from NapCat, avoiding CQ code interpretation.
        return None
    text = "".join(part.get("data", {}).get("text", "") for part in message if part.get("type") == "text").strip()
    mentioned = any(
        part.get("type") == "at" and str(part.get("data", {}).get("qq", "")) == bot
        for part in message
    )
    if require_trigger and kind == "group" and not (mentioned or any(alias in text.casefold() for alias in ('aemeath', '爱弥斯', 'ams', '小爱'))):
        return None
    attachments = tuple(part.get('data', {}) for part in message
                        if part.get('type') == 'file') if kind == 'private' else ()
    images = tuple(part.get('data', {}) for part in message
                   if part.get('type') == 'image') if owner_request(source, owner) else ()
    return Incoming(source, text, attachments, images=images) if text or attachments or images else None


class Bridge:
    def blocked(self, source):
        return blocked_group_sender(source, self.cfg)

    def block_user(self, user, *, blocked=True):
        users = update_blacklist(BASE / 'config.json', user, self.cfg['owner_qq'], blocked=blocked)
        self.cfg['group_user_blacklist'] = users

    def auto_block(self, source):
        """Only the event's real sender can be auto-blocked; owners are exempt."""
        if source.kind != 'group' or owner_request(source, self.cfg['owner_qq']):
            return
        try:
            self.block_user(source.sender_id)
        except (OSError, ValueError, TypeError):
            # Stay silent even if persistence fails; still block until the next restart.
            users = {str(user) for user in self.cfg.get('group_user_blacklist', [])}
            users.add(str(source.sender_id))
            self.cfg['group_user_blacklist'] = sorted(users)
            logging.error('Group blacklist persistence failed; sender blocked in memory')

    def conversation(self, source):
        return conversation_key(source, self.cfg['owner_qq'])

    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self.app = AppServer(cfg["codex_path"])
        self.pending = {}
        self.send_lock = asyncio.Lock()
        self.last_send = 0.0
        self.queue = asyncio.Queue(maxsize=8)
        self.db = sqlite3.connect(BASE / "chat.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, time REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS history (role TEXT, content TEXT)")
        # Preserve pre-upgrade history exclusively for the owner's private chat.
        if "conversation" not in {row[1] for row in self.db.execute("PRAGMA table_info(history)")}:
            self.db.execute("ALTER TABLE history ADD COLUMN conversation TEXT")
            self.db.execute("UPDATE history SET conversation=?", (f"private:{int(cfg['owner_qq'])}",))
        self.db.execute("CREATE TABLE IF NOT EXISTS threads (conversation TEXT PRIMARY KEY, thread_id TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS bootstrap (thread_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS old_threads (conversation TEXT, thread_id TEXT, time REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS group_context (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, content TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS group_context_group ON group_context(group_id, id)")
        self.db.commit()

    def remember_group(self, source, text):
        cursor = self.db.execute("INSERT INTO group_context (group_id, content) VALUES (?, ?)",
                                 (source.target, speaker_message(source, text[:1000], self.cfg['owner_qq'])))
        context_id = cursor.lastrowid
        self.db.execute("DELETE FROM group_context WHERE group_id=? AND id NOT IN "
                        "(SELECT id FROM group_context WHERE group_id=? ORDER BY id DESC LIMIT 100)",
                        (source.target, source.target))
        self.db.commit()
        return context_id

    def group_reference(self, source, context_id):
        if source.kind != 'group' or context_id is None:
            return ''
        rows = self.db.execute("SELECT content FROM group_context WHERE group_id=? AND id<? "
                               "ORDER BY id DESC LIMIT 40", (source.target, context_id)).fetchall()
        messages, size = [], 0
        for (content,) in rows:
            if size + len(content) > 12000:
                break
            messages.append(content)
            size += len(content)
        if not messages:
            return ''
        return prompts.group_context(list(reversed(messages)))

    async def send(self, source, text):
        # One limiter for progress, answers and commands, including long chunks.
        async with self.send_lock:
            if self.blocked(source):
                return
            await self._send(source, text)

    async def _send(self, source, text):
        # Group members listed in mention_qq are @-mentioned on replies to them.
        mention = source.kind == "group" and str(source.sender_id) in {str(q) for q in self.cfg.get("mention_qq", ())}
        for start in range(0, len(text), 1600):
            message = []
            if start == 0 and mention:
                message.append({"type": "at", "data": {"qq": str(source.sender_id)}})
            message.append({"type": "text", "data": {"text": text[start:start + 1600]}})
            echo = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[echo] = future
            try:
                delay = 1.5 - (time.monotonic() - self.last_send)
                if delay > 0:
                    await asyncio.sleep(delay)
                self.last_send = time.monotonic()
                await self.ws.send(json.dumps({"action": f"send_{source.kind}_msg", "params": {
                    "group_id" if source.kind == "group" else "user_id": source.target,
                    "message": message,
                }, "echo": echo}))
                response = await asyncio.wait_for(future, timeout=20)
                if response.get("status") != "ok" or response.get("retcode") != 0:
                    raise RuntimeError("QQ rejected outgoing message")
            finally:
                self.pending.pop(echo, None)

    async def onebot_call(self, action, params):
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({'action': action, 'params': params, 'echo': echo}))
            response = await asyncio.wait_for(future, 120)
            if response.get('status') != 'ok' or response.get('retcode') != 0:
                raise ValueError('NapCat 下载附件失败，文件可能已过期')
            return response.get('data') or {}
        finally:
            self.pending.pop(echo, None)

    async def receive_attachments(self, incoming):
        if incoming.source.kind != 'private' or str(incoming.source.sender_id) != str(self.cfg['owner_qq']):
            raise ValueError('当前聊天未开放附件接收')
        if len(incoming.attachments) > 5:
            raise ValueError('单条消息最多接收 5 个附件')
        paths = []
        for attachment in incoming.attachments:
            file_id = attachment.get('file_id')
            if not isinstance(file_id, str) or not file_id:
                raise ValueError('附件缺少 file_id')
            if int(attachment.get('file_size') or 0) > MAX_FILE_BYTES:
                raise ValueError('附件超过 50 MiB 上限')
            result = await self.onebot_call('get_file', {'file_id': file_id})
            path = await asyncio.to_thread(save_attachment, result.get('file', ''),
                                          result.get('file_name') or attachment.get('file') or 'attachment')
            paths.append(str(path))
            logging.info('Private attachment saved: %s', path)
        return prompts.attachments_saved(incoming.text, paths)

    async def receive_images(self, incoming):
        if not owner_request(incoming.source, self.cfg['owner_qq']):
            raise ValueError('当前聊天未开放图片接收')
        if len(incoming.images) > 5:
            raise ValueError('单条消息最多接收 5 张图片')
        paths = []
        for image in incoming.images:
            if int(image.get('file_size') or 0) > MAX_IMAGE_BYTES:
                raise ValueError('单张图片超过 20 MiB 上限')
            result = {}
            ident = image.get('file_id') or image.get('file')
            if ident:
                try:
                    result = await self.onebot_call('get_image', {'file': ident})
                except (ValueError, asyncio.TimeoutError):
                    # Fresh event URLs can still work when the image cache lookup fails.
                    pass
            path = await asyncio.to_thread(save_image, result.get('file', ''),
                                           result.get('url') or image.get('url', ''))
            paths.append(str(path))
        return paths

    async def instructions(self, authorized):
        rules = None
        rules_path = BASE / 'workspace' / 'AGENTS.md'
        if rules_path.is_file():
            rules = await asyncio.to_thread(rules_path.read_text, encoding='utf-8')
            logging.info('QQ workspace AGENTS.md loaded for current turn')
        return prompts.instructions(authorized, rules)

    async def thread_for(self, source, authorized):
        """Resume the conversation's Codex thread, or start one, under this turn's permissions."""
        options = {"cwd": str(BASE / "workspace"), "approvalPolicy": "never",
                   "sandbox": "danger-full-access" if authorized else "read-only",
                   "developerInstructions": await self.instructions(authorized)}
        await self.app.start()
        conversation = self.conversation(source)
        row = self.db.execute("SELECT thread_id FROM threads WHERE conversation=?", (conversation,)).fetchone()
        if row:
            await self.app.call("thread/resume", {"threadId": row[0], **options})
            return row[0]
        result = await self.app.call("thread/start", {**options, "ephemeral": False})
        thread = result["thread"]["id"]
        self.db.execute("INSERT INTO threads VALUES (?, ?)", (conversation, thread))
        self.db.commit()
        return thread

    def turn_text(self, source, thread, text, context_id):
        """Prefix old-bridge history (until the thread is bootstrapped) and recent group context."""
        if not self.db.execute("SELECT 1 FROM bootstrap WHERE thread_id=?", (thread,)).fetchone():
            history = self.db.execute("SELECT role, content FROM history WHERE conversation=? ORDER BY rowid", (self.conversation(source),)).fetchall()
            if history:
                text = prompts.history_import(history, text)
        return self.group_reference(source, context_id) + text

    async def relay_progress(self, source, queue):
        while True:
            first = await queue.get()
            if first is None:
                return
            await asyncio.sleep(1)
            batch = [first]
            done = False
            while not queue.empty():
                message = queue.get_nowait()
                if message is None:
                    done = True
                    break
                if message != batch[-1]:
                    batch.append(message)
            try:
                await self.send(source, '【进度】\n' + '\n'.join(batch))
            except Exception:
                logging.warning('Progress delivery failed')
            if done:
                return

    async def relay_answer(self, source, queue):
        async for message in stream_bubbles(queue):
            await self.send(source, message)

    async def run_turn(self, source, thread, text, authorized, image_paths):
        """Run one Codex turn; authorized turns stream progress and the answer to QQ."""
        progress_queue = asyncio.Queue(maxsize=128)

        def progress(message):
            if not progress_queue.full():
                progress_queue.put_nowait(message)

        answer_queue = asyncio.Queue()

        def answer_delta(delta):
            if delta:
                answer_queue.put_nowait(delta)

        reporter = asyncio.create_task(self.relay_progress(source, progress_queue)) if authorized else None
        answer_sender = asyncio.create_task(self.relay_answer(source, answer_queue)) if authorized else None
        try:
            reply = await self.app.turn(thread, text, on_progress=progress if authorized else None,
                                        on_answer=answer_delta if authorized else None,
                                        show_tools=bool(self.cfg.get('show_tool_progress', False)),
                                        image_paths=image_paths)
            if answer_sender:
                answer_queue.put_nowait(None)
                await asyncio.wait_for(asyncio.shield(answer_sender), 120)
        finally:
            if answer_sender:
                answer_sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await answer_sender
            if reporter:
                try:
                    await asyncio.wait_for(progress_queue.put(None), 1)
                    await asyncio.wait_for(asyncio.shield(reporter), 5)
                except asyncio.TimeoutError:
                    reporter.cancel()
                finally:
                    reporter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reporter
        return reply

    async def chat(self, source, text, context_id=None, image_paths=()):
        authorized = owner_request(source, self.cfg['owner_qq'])
        thread = await self.thread_for(source, authorized)
        text = self.turn_text(source, thread, text, context_id)
        reply = await self.run_turn(source, thread, text, authorized, image_paths)
        self.db.execute("INSERT OR IGNORE INTO bootstrap VALUES (?)", (thread,))
        self.db.commit()
        return reply, authorized

    async def worker(self):
        while True:
            incoming = await self.queue.get()
            source, text = incoming.source, incoming.text
            try:
                if self.blocked(source):
                    continue
                if source.kind == 'group' and not owner_request(source, self.cfg['owner_qq']) and obvious_abuse(text):
                    self.auto_block(source)
                    continue
                # Management commands bypass the model, so authorize them here.
                # Deny unknown slash commands too, to keep future additions closed by default.
                if (text.startswith('/') and text.split(maxsplit=1)[0].casefold() not in ('/ping', '/help')
                        and not owner_request(source, self.cfg['owner_qq'])):
                    await self.send(source, '这类管理命令仅限主号使用；你可以继续聊天，或使用 /ping、/help。')
                    continue
                if incoming.attachments:
                    try:
                        text = await self.receive_attachments(incoming)
                    except (ValueError, OSError, asyncio.TimeoutError) as error:
                        logging.warning('Attachment reception failed: %s', type(error).__name__)
                        await self.send(source, '附件接收失败：' + (str(error) if isinstance(error, ValueError) else '下载或保存失败，请重新发送。'))
                        continue
                image_paths = ()
                if incoming.images:
                    try:
                        image_paths = await self.receive_images(incoming)
                    except (ValueError, OSError, asyncio.TimeoutError) as error:
                        logging.warning('Image reception failed: %s', type(error).__name__)
                        await self.send(source, '图片接收失败：' + (str(error) if isinstance(error, ValueError) else '下载或保存失败，请重新发送。'))
                        continue
                    text = prompts.images_saved(text, image_paths)
                if text in ("/ping", "连接测试"):
                    await self.send(source, "QQ 聊天连接正常。")
                elif text == "/new":
                    self.db.execute("INSERT INTO old_threads SELECT conversation, thread_id, ? FROM threads WHERE conversation=?", (time.time(), self.conversation(source)))
                    self.db.execute("DELETE FROM threads WHERE conversation=?", (self.conversation(source),))
                    self.db.execute("DELETE FROM history WHERE conversation=?", (self.conversation(source),))
                    self.db.commit()
                    await self.send(source, "已清空聊天上下文，可以开始新话题。")
                elif text == "/session":
                    row = self.db.execute("SELECT thread_id FROM threads WHERE conversation=?", (self.conversation(source),)).fetchone()
                    await self.send(source, "当前会话：" + (row[0] if row else "尚未创建；发送一条消息即可创建。"))
                elif text == '/blacklist':
                    users = self.cfg.get('group_user_blacklist', [])
                    await self.send(source, '群聊用户黑名单：' + ('、'.join(map(str, users)) if users else '空'))
                elif text.split(maxsplit=1)[:1] in (['/block'], ['/unblock']):
                    parts = text.split()
                    if len(parts) != 2:
                        await self.send(source, '用法：/block QQ号 或 /unblock QQ号')
                        continue
                    try:
                        self.block_user(parts[1], blocked=parts[0] == '/block')
                    except ValueError as error:
                        await self.send(source, str(error))
                        continue
                    await self.send(source, '已加入群聊用户黑名单。' if parts[0] == '/block' else '已解除群聊用户拉黑。')
                elif text == "/help":
                    await self.send(source, "Aemeath：主号在私聊或群聊可请求Linux操作，其他人仅文字聊天；Windows文件只读。群聊需@机器人并附文字或图片，或喊Aemeath、爱弥斯、ams、小爱（英文不分大小写）。图片识别仅主号可用，每条最多5张、每张20 MiB，支持PNG、JPEG、WebP、GIF。文件附件仅主号私聊接收。/ping、/help 所有人可用；/new 创建新会话、/session 查看会话ID、/blacklist 查看群聊用户黑名单、/block QQ号 拉黑、/unblock QQ号 解除拉黑仅限主号。黑名单用户的群消息静默忽略。")
                elif len(text) > 6000:
                    await self.send(source, "这条消息较长，请拆成每条不超过6000字。")
                else:
                    text = speaker_message(source, text, self.cfg['owner_qq'])
                    reply, streamed = await self.chat(source, text, incoming.context_id, image_paths=image_paths)
                    if source.kind == 'group' and not owner_request(source, self.cfg['owner_qq']):
                        if reply.strip() == BLOCK_SENDER:
                            self.auto_block(source)
                            continue
                        reply = bounded_guest_reply(reply)
                    if not streamed:
                        await self.send(source, reply)
                    self.db.executemany(
                        "INSERT INTO history (role, content, conversation) VALUES (?, ?, ?)",
                        [("user", text, self.conversation(source)), ("assistant", reply, self.conversation(source))])
                    self.db.commit()
                    logging.info("Chat reply delivered")
            except Exception as error:
                logging.exception("Chat delivery failed")
                with contextlib.suppress(Exception):
                    await self.send(source, "这次回复未能完成，请稍后重试；可先发送 /ping 检查 QQ 连接。")
            finally:
                self.queue.task_done()

    async def session(self, ws):
        self.ws = ws
        worker = asyncio.create_task(self.worker())
        try:
            async for payload in ws:
                event = json.loads(payload)
                if event.get("echo") in self.pending:
                    future = self.pending[event["echo"]]
                    if not future.done():
                        future.set_result(event)
                    continue
                incoming = incoming_text(event, self.cfg["owner_qq"], self.cfg.get("allowed_groups", []),
                                         allow_all_groups=self.cfg.get('allow_all_groups', True), require_trigger=False,
                                         group_user_blacklist=self.cfg.get('group_user_blacklist', []))
                if incoming is None:
                    continue
                message_id = event.get("message_id")
                if message_id is None:
                    continue
                key = str(event.get("self_id")) + ":" + str(message_id)
                if self.db.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
                    continue
                self.db.execute("INSERT INTO seen VALUES (?, ?)", (key, time.time()))
                self.db.execute("DELETE FROM seen WHERE time < ?", (time.time() - 7 * 86400,))
                self.db.commit()
                if incoming.source.kind == 'group':
                    triggered = incoming_text(event, self.cfg['owner_qq'], self.cfg.get('allowed_groups', []),
                                              allow_all_groups=self.cfg.get('allow_all_groups', True),
                                              group_user_blacklist=self.cfg.get('group_user_blacklist', []))
                    if triggered and not owner_request(incoming.source, self.cfg['owner_qq']) and obvious_abuse(incoming.text):
                        self.auto_block(incoming.source)
                        continue
                    context_id = self.remember_group(incoming.source, incoming.text)
                    if triggered is None:
                        continue
                    incoming = Incoming(incoming.source, incoming.text, incoming.attachments, context_id, incoming.images)
                try:
                    self.queue.put_nowait(incoming)
                except asyncio.QueueFull:
                    logging.warning("Queue full; message skipped")
                    continue
                logging.info("Allowed %s message queued", incoming.source.kind)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            self.pending.clear()
            self.ws = None

    async def run(self):
        await self.app.start()
        logging.info("App Server initialized")
        while True:
            try:
                async with connect(self.cfg["ws_url"], additional_headers={
                    "Authorization": "Bearer " + self.cfg["onebot_token"]},
                    max_size=2 ** 20, proxy=None) as ws:
                    logging.info("Connected to NapCat")
                    await self.session(ws)
            except Exception as error:
                logging.warning("Connection unavailable (%s); retrying", type(error).__name__)
                await asyncio.sleep(5)


def main():
    config = json.loads(CONFIG.read_text())
    if not str(config.get("owner_qq", "")).isdigit():
        raise SystemExit("Set owner_qq before starting the bridge")
    (BASE / "workspace").mkdir(exist_ok=True)
    asyncio.run(Bridge(config).run())

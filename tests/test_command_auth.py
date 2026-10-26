"""Exercise actual worker dispatch, with an in-memory DB and no QQ sends."""
import asyncio
import contextlib
import sqlite3
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from qq_chat.bridge import Bridge, Incoming, Source


class CommandAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.cfg = {'owner_qq': 123}
        self.bridge.queue = asyncio.Queue()
        self.bridge.db = sqlite3.connect(':memory:')
        self.bridge.db.executescript('''
            CREATE TABLE threads (conversation TEXT PRIMARY KEY, thread_id TEXT);
            CREATE TABLE history (role TEXT, content TEXT, conversation TEXT);
            CREATE TABLE old_threads (conversation TEXT, thread_id TEXT, time REAL);
        ''')
        for conversation in ('group:999', 'group:999:owner', 'private:123'):
            self.bridge.db.execute('INSERT INTO threads VALUES (?, ?)', (conversation, 'id-' + conversation))
            self.bridge.db.execute('INSERT INTO history VALUES (?, ?, ?)', ('user', 'keep', conversation))
        self.bridge.db.commit()
        self.sent = []

        async def send(source, text):
            self.sent.append(text)

        async def chat(*args):
            raise AssertionError('Command should not reach the model')

        self.bridge.send, self.bridge.chat = send, chat
        self.worker = asyncio.create_task(self.bridge.worker())

    async def asyncTearDown(self):
        self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.worker
        self.bridge.db.close()

    async def dispatch(self, source, command):
        await self.bridge.queue.put(Incoming(source, command))
        await asyncio.wait_for(self.bridge.queue.join(), 1)

    def snapshot(self):
        return tuple(list(self.bridge.db.execute('SELECT * FROM ' + table))
                     for table in ('threads', 'history', 'old_threads'))

    async def test_group_member_cannot_reset_or_read_session_or_use_future_commands(self):
        before = self.snapshot()
        for command in ('/new', '/session', '/block 123', '/unblock 789', '/blacklist', '/effort max', '/future', '/NEW', '/new extra'):
            await self.dispatch(Source('group', 999, 789, '心糖'), command)
            self.assertIn('仅限主号', self.sent[-1])
            self.assertNotIn('id-group', self.sent[-1])
            self.assertEqual(self.snapshot(), before)

    async def test_owner_can_reset_only_own_group_thread(self):
        await self.dispatch(Source('group', 999, 123), '/new')
        threads = dict(self.bridge.db.execute('SELECT * FROM threads'))
        self.assertNotIn('group:999:owner', threads)
        self.assertIn('group:999', threads)
        self.assertIn('private:123', threads)
        self.assertEqual(self.bridge.db.execute('SELECT conversation FROM old_threads').fetchall(), [('group:999:owner',)])

    async def test_private_whitelist_guest_has_no_management_permissions(self):
        self.bridge.cfg['private_whitelist'] = [789]
        before = self.snapshot()
        for command in ('/new', '/session', '/block 123', '/unblock 789', '/blacklist'):
            await self.dispatch(Source('private', 789, 789), command)
            self.assertIn('仅限主号', self.sent[-1])
            self.assertEqual(self.snapshot(), before)

    async def test_owner_can_read_private_and_group_session(self):
        for source, expected in ((Source('private', 123, 123), 'id-private:123'),
                                 (Source('group', 999, 123), 'id-group:999:owner')):
            await self.dispatch(source, '/session')
            self.assertIn(expected, self.sent[-1])

    async def test_only_owner_can_manage_persistent_group_blacklist(self):
        with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'owner_qq': 123, 'allowed_groups': [], 'allow_all_groups': False}))
            source = Source('private', 123, 123)
            await self.dispatch(source, '/block 789')
            self.assertEqual(self.bridge.cfg['group_user_blacklist'], ['789'])
            await self.dispatch(source, '/blacklist')
            self.assertIn('789', self.sent[-1])
            await self.dispatch(source, '/block 123')
            self.assertIn('不能拉黑主号', self.sent[-1])
            await self.dispatch(source, '/block nope')
            self.assertIn('正整数', self.sent[-1])
            await self.dispatch(source, '/unblock 789')
            self.assertEqual(self.bridge.cfg['group_user_blacklist'], [])
            self.assertEqual(json.loads(config.read_text())['group_user_blacklist'], [])

    async def test_public_ping_help_do_not_change_state(self):
        before = self.snapshot()
        for command in ('/ping', '/help', '连接测试'):
            await self.dispatch(Source('group', 999, 789), command)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()

"""Frontend control works without a model, survives service restart and fails closed."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qq_chat.bridge import Bridge, Source
from qq_chat.front_switch import (atomic_json, changed_config, command, choices,
                                   job_directory, pending_job, run_job, schedule)


def config():
    return {'owner_qq': 123, 'agents': {'front': 'grok', 'guest': 'guest', 'back': [], 'profiles': {
        'grok': {'type': 'grok_build', 'path': '/bin/true', 'model': 'grok-4.7',
                 'models': ['grok-4.7'], 'permission_mode': 'bypassPermissions', 'sandbox': 'off'},
        'codex': {'type': 'codex_app', 'path': '/bin/true'},
        'guest': {'type': 'claude_code', 'path': '/bin/true', 'model': 'haiku', 'models': ['haiku']}}}}


class FrontSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = config(); atomic_json(self.root / 'config.json', self.cfg)
        self.bridge = SimpleNamespace(cfg=self.cfg, send=AsyncMock())
        self.owner = Source('private', 123, 123)
        self.scheduler = patch('qq_chat.front_switch.schedule', new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def call(self, text, authorized=True):
        await command(self.bridge, self.owner, text, self.root, authorized=authorized)

    def read_config(self):
        return json.loads((self.root / 'config.json').read_text())

    def job(self):
        directory = job_directory(self.root)
        ident = json.loads((directory / 'pending.json').read_text())['id']
        return directory / (ident + '.json'), ident

    async def test_owner_switch_acknowledged_before_schedule_and_no_early_mutation(self):
        async def scheduling(root, ident):
            self.bridge.send.assert_awaited_once()
            self.assertEqual(self.read_config(), self.cfg)
        self.scheduler.side_effect = scheduling
        await self.call('/front codex')
        path, _ = self.job()
        self.assertEqual(json.loads(path.read_text())['target'], 'codex')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(pending_job(path.parent))
        self.assertEqual(self.bridge.cfg, self.cfg)

    async def test_listing_same_front_bad_args_and_unknown_target_do_not_restart(self):
        for text in ('/front', '/FRONT grok', '/front a b', '/front --help', '/front ../../codex'):
            await self.call(text)
            self.assertEqual(self.read_config(), self.cfg)
        self.scheduler.assert_not_awaited()
        self.assertEqual(choices(self.cfg), ['grok', 'codex'])

    async def test_guest_cannot_even_read_available_profiles(self):
        for text in ('/front', '/front codex'):
            await self.call(text, authorized=False)
            self.assertIn('仅限主人', self.bridge.send.await_args.args[1])
        self.scheduler.assert_not_awaited()
        self.assertFalse((self.root / 'run').exists())

    async def test_duplicate_switch_is_rejected_until_job_finishes(self):
        await self.call('/front codex')
        await self.call('/front codex')
        self.scheduler.assert_awaited_once()
        self.assertIn('正在进行', self.bridge.send.await_args.args[1])

    async def test_unavailable_scheduler_leaves_config_untouched_and_retry_possible(self):
        self.scheduler.side_effect = RuntimeError('secret must never be sent')
        await self.call('/front codex')
        path, _ = self.job(); self.assertEqual(json.loads(path.read_text())['status'], 'failed')
        self.assertEqual(self.read_config(), self.cfg)
        self.assertNotIn('secret', self.bridge.send.await_args.args[1])
        self.assertFalse(pending_job(path.parent))

    async def test_failed_ack_cannot_schedule_or_change_front(self):
        self.bridge.send.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError): await self.call('/front codex')
        self.scheduler.assert_not_awaited()
        self.assertEqual(self.read_config(), self.cfg)
        path, _ = self.job(); self.assertFalse(pending_job(path.parent))

    async def test_job_reads_latest_config_preserves_guests_and_backs_up_private(self):
        await self.call('/front codex'); path, ident = self.job()
        latest = self.read_config(); latest['group_user_blacklist'] = ['555']
        atomic_json(self.root / 'config.json', latest)
        # run_job owns its own notify event loop, just as the independent service does.
        with patch('qq_chat.front_switch.restart_and_check') as restart, patch('qq_chat.front_switch.notify', AsyncMock(return_value=True)):
            await asyncio.to_thread(run_job, self.root, ident)
            restart.assert_called_once_with(self.root)
            await asyncio.to_thread(run_job, self.root, ident)  # no duplicate restart
            restart.assert_called_once()
        current = self.read_config()
        self.assertEqual(current['agents']['front'], 'codex')
        self.assertEqual(current['agents']['guest'], 'guest')
        self.assertEqual(current['agents']['back'], [])
        self.assertEqual(current['group_user_blacklist'], ['555'])
        self.assertEqual(json.loads((self.root/'config.json.bak').read_text()), latest)
        self.assertEqual((self.root/'config.json.bak').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['status'], 'completed')

    async def test_restart_failure_rolls_back_and_checks_recovery(self):
        await self.call('/front codex'); path, ident = self.job()
        with patch('qq_chat.front_switch.restart_and_check', side_effect=[RuntimeError('bad startup'), None]) as restart, patch('qq_chat.front_switch.notify', AsyncMock(return_value=True)):
            await asyncio.to_thread(run_job, self.root, ident)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(self.read_config(), self.cfg)
        self.assertEqual(json.loads(path.read_text())['status'], 'rolled_back')

    async def test_restart_failure_does_not_overwrite_concurrent_config_changes(self):
        await self.call('/front codex'); path, ident = self.job()
        def restart(root):
            current = self.read_config(); current['agents']['front'] = 'grok'
            current['extra'] = 'manual change'; atomic_json(root/'config.json', current)
            raise RuntimeError('failed')
        with patch('qq_chat.front_switch.restart_and_check', side_effect=restart), patch('qq_chat.front_switch.notify', AsyncMock(return_value=True)):
            await asyncio.to_thread(run_job, self.root, ident)
        self.assertEqual(self.read_config()['extra'], 'manual change')
        self.assertEqual(json.loads(path.read_text())['status'], 'failed')

    async def test_revalidation_failure_never_mutates_config_or_restarts(self):
        await self.call('/front codex'); path, ident = self.job()
        latest = self.read_config(); del latest['agents']['profiles']['codex']
        atomic_json(self.root/'config.json', latest)
        with patch('qq_chat.front_switch.restart_and_check') as restart, patch('qq_chat.front_switch.notify', AsyncMock(return_value=True)):
            await asyncio.to_thread(run_job, self.root, ident)
        restart.assert_not_called(); self.assertEqual(self.read_config(), latest)
        self.assertEqual(json.loads(path.read_text())['status'], 'failed')

    def test_legacy_default_guest_is_pinned_and_unsafe_grok_back_is_rejected(self):
        cfg = self.cfg; cfg['agents']['front'] = 'codex'; del cfg['agents']['guest']
        updated = changed_config(cfg, 'grok')
        self.assertEqual(updated['agents']['guest'], 'codex')
        cfg['agents']['back'] = ['codex']; cfg['agents']['profiles']['codex']['writes'] = True
        with self.assertRaises(ValueError): changed_config(cfg, 'grok')

    async def test_real_session_dispatch_bypasses_blocked_worker_and_checks_identity(self):
        with patch('qq_chat.bridge.BASE', self.root):
            bridge = Bridge(self.cfg)
        self.addCleanup(bridge.db.close)
        bridge.send = AsyncMock(); bridge.voice = None
        entered = asyncio.Event(); busy = asyncio.Event(); events = asyncio.Queue()
        async def blocked_worker():
            entered.set(); await busy.wait()
        bridge.worker = blocked_worker
        class Socket:
            def __aiter__(self): return self
            async def __anext__(self): return await events.get()
        bridge.cfg['private_whitelist'] = [456]
        def event(number, user):
            return json.dumps({'post_type':'message','message_type':'private','self_id':999,
                               'user_id':user,'message_id':number,'message':[{'type':'text','data':{'text':'/front codex'}}]})
        with patch('qq_chat.bridge.BASE', self.root):
            task = asyncio.create_task(bridge.session(Socket()))
            try:
                await entered.wait()
                await events.put(event(1, 456))
                async with asyncio.timeout(2):
                    while not bridge.send.await_count: await asyncio.sleep(.001)
                self.scheduler.assert_not_awaited()
                self.assertIn('仅限主人', bridge.send.await_args.args[1])
                await events.put(event(2, 123))
                async with asyncio.timeout(2):
                    while not self.scheduler.await_count: await asyncio.sleep(.001)
                self.assertEqual(bridge.queue.qsize(), 0)
                self.assertFalse(busy.is_set())
            finally:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
                await bridge.tasks.close()

    async def test_scheduler_uses_independent_service_without_shell(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch('qq_chat.front_switch.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
            await schedule(self.root, 'valid-id')
        args = launch.await_args.args
        self.assertEqual(args[0], 'systemd-run')
        self.assertIn('--on-active=2s', args)
        self.assertIn('qq_chat.front_switch', args)
        self.assertEqual(args[-1], 'valid-id')

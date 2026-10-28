"""Direct diary lookup and one-time migration, using temporary records only."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.diary_migration import migrate
from qq_chat.memory import search


class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'memory/private').mkdir(parents=True)
        self.source = self.root/'our-story/2026-10/2026-10-28.md'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('纸飞机原始日记')
        self.dest = self.root/'memory/private/diary/our-story/2026-10/2026-10-28.md'

    def test_dry_run_then_apply_verifies_and_preserves_original(self):
        self.assertEqual(migrate(self.root)['new_files'], 1)
        self.assertFalse(self.dest.exists())
        self.assertEqual(migrate(self.root, apply=True)['new_files'], 1)
        self.assertEqual(self.dest.read_bytes(), self.source.read_bytes())
        self.assertEqual(migrate(self.root, apply=True)['new_files'], 0)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o600)

    def test_conflict_preflight_preserves_both_and_writes_no_other_records(self):
        self.dest.parent.mkdir(parents=True); self.dest.write_text('新正式日记')
        (self.root/'OUR_STORY.md').write_text('索引')
        with self.assertRaisesRegex(ValueError, '不同内容'):
            migrate(self.root, apply=True)
        self.assertEqual(self.dest.read_text(), '新正式日记')
        self.assertEqual(self.source.read_text(), '纸飞机原始日记')
        self.assertFalse((self.dest.parents[2]/'OUR_STORY.md').exists())

    def test_new_diary_is_immediately_searchable_without_original_or_sync(self):
        migrate(self.root, apply=True)
        self.source.unlink()
        self.dest.write_text('新内容雪绒球')
        hit = search(self.root/'memory', '雪绒球', exclude_private_snapshots=True)
        self.assertEqual(hit['results'][0]['file'], 'private/diary/our-story/2026-10/2026-10-28.md')
        self.assertFalse(search(self.root/'memory', '纸飞机', exclude_private_snapshots=True)['results'])
        self.assertFalse(search(self.root/'memory', '雪绒球', group_id=20001, exclude_private_snapshots=True)['results'])

    def test_retired_copies_excluded_but_manual_notes_preserved(self):
        for directory in ('diary-current', 'existing-records-20261028-123456-123456'):
            p=self.root/'memory/private'/directory/'old.md';p.parent.mkdir();p.write_text('旧结论')
        (self.root/'memory/private/manual.md').write_text('手记')
        self.assertFalse(search(self.root/'memory','旧结论',exclude_private_snapshots=True)['results'])
        self.assertTrue(search(self.root/'memory','手记',exclude_private_snapshots=True)['results'])

    def test_whitelist_and_retry_after_interrupted_copy(self):
        (self.root/'OWNER.md').write_text('不得迁移')
        (self.root/'OUR_STORY.md').write_text('日期索引')
        from qq_chat.diary_migration import replace_copy
        calls = []
        def interrupted(fd, parts, raw):
            calls.append(parts)
            if len(calls) == 2:
                raise OSError('simulated interruption')
            replace_copy(fd, parts, raw)
        with patch('qq_chat.diary_migration.replace_copy',side_effect=interrupted):
            with self.assertRaises(OSError):migrate(self.root,apply=True)
        self.assertEqual(migrate(self.root,apply=True)['new_files'],1)
        self.assertFalse((self.root/'memory/private/diary/OWNER.md').exists())
        self.assertEqual(self.dest.read_bytes(),self.source.read_bytes())

    def test_source_symlink_and_hardlink_rejected(self):
        import os
        self.source.unlink();self.source.symlink_to('/etc/hostname')
        with self.assertRaises(OSError):migrate(self.root, apply=True)
        self.source.unlink();other=self.root/'other';other.write_text('链接')
        os.link(other,self.source)
        with self.assertRaises(ValueError):migrate(self.root, apply=True)

    def test_destination_symlink_rejected(self):
        (self.root/'memory/private/diary').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):migrate(self.root, apply=True)

    def test_lock_and_bounds_prevent_writes(self):
        import fcntl
        (self.root/'.automation').mkdir()
        with (self.root/'.automation/nightly.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):migrate(self.root,apply=True)
        with patch('qq_chat.diary_migration.MAX_RECORDS',0):
            with self.assertRaises(ValueError):migrate(self.root,apply=True)
        self.assertFalse(self.dest.exists())


from tests import test_front_tools as ft


class DiaryToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ft.FrontToolsTests.asyncSetUp
    asyncTearDown = ft.FrontToolsTests.asyncTearDown
    setup_tools = ft.FrontToolsTests.setup_tools
    token = ft.FrontToolsTests.token

    async def test_service_reads_new_diary_without_copy_or_lock(self):
        server = await self.setup_tools()
        diary = self.root/'workspace/memory/private/diary/our-story/2026-10/2026-10-28.md'
        diary.parent.mkdir(parents=True); diary.write_text('私人的新日记测试内容')
        async with server.grant(self.group,memory_only=True) as grant:
            self.assertFalse((await server.call(self.token(grant),'memory_search',{'query':'私人的'}))['results'])
        async with server.grant(self.owner,memory_only=True) as grant:
            self.assertTrue((await server.call(self.token(grant),'memory_search',{'query':'新日记'}))['results'])
            diary.write_text('私人的更新日记测试内容')
            self.assertTrue((await server.call(self.token(grant),'memory_search',{'query':'更新日记'}))['results'])
        self.assertFalse((self.root/'workspace/.automation').exists())

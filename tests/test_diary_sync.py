"""Private diary copy freshness and real authenticated search integration."""
import fcntl
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.diary_sync import search_with_diaries, synchronize
from tests import test_front_tools as ft


class DiaryCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'; self.root.mkdir()
        self.relative = 'our-story/2026-10/2026-10-28.md'
        self.diary = self.root / self.relative
        self.diary.parent.mkdir(parents=True)
        self.diary.write_text('第一条日记：今天的纸飞机飞得很远。')
        self.copy = self.root / 'memory/private/diary-current' / self.relative

    def test_new_saved_record_is_searchable_without_changing_source(self):
        original = self.diary.read_bytes()
        result = search_with_diaries(self.root, '纸飞机')
        self.assertIn('纸飞机', result['results'][0]['text'])
        self.assertEqual(result['results'][0]['file'], 'private/diary-current/' + self.relative)
        self.assertEqual(result['diary_sync']['updated'], 1)
        self.assertEqual(self.diary.read_bytes(), original)
        self.assertEqual(self.copy.stat().st_mode & 0o777, 0o600)

    def test_source_change_even_with_same_size_and_mtime_replaces_stale_copy(self):
        search_with_diaries(self.root, '纸飞机')
        old = self.diary.stat()
        self.diary.write_text('第二条日记：今天的雪绒球飞得很远。')
        self.assertEqual(self.diary.stat().st_size, old.st_size)
        os.utime(self.diary, ns=(old.st_atime_ns, old.st_mtime_ns))
        result = search_with_diaries(self.root, '雪绒球')
        self.assertIn('雪绒球', result['results'][0]['text'])
        self.assertFalse(search_with_diaries(self.root, '纸飞机')['results'])

    def test_unchanged_copy_not_rewritten_and_new_date_added(self):
        search_with_diaries(self.root, '纸飞机'); old = self.copy.stat().st_mtime_ns
        self.assertEqual(search_with_diaries(self.root, '纸飞机')['diary_sync']['updated'], 0)
        self.assertEqual(self.copy.stat().st_mtime_ns, old)
        other = self.diary.with_name('2026-10-29.md'); other.write_text('新一天的海豹故事。')
        self.assertIn('海豹', search_with_diaries(self.root, '海豹')['results'][0]['text'])

    def test_removed_source_only_removes_managed_copy_not_original_or_manual_notes(self):
        search_with_diaries(self.root, '纸飞机')
        manual = self.root / 'memory/private/manual.md'; manual.write_text('手动记忆')
        self.diary.unlink()
        result = search_with_diaries(self.root, '纸飞机')
        self.assertEqual(result['results'], [])
        self.assertEqual(result['diary_sync']['removed'], 1)
        self.assertFalse(self.copy.exists()); self.assertTrue(manual.exists())

    def test_legacy_snapshot_preserved_but_not_returned_as_current_fact(self):
        old = self.root / 'memory/private/existing-records-20261028-000000-000001/old.md'
        old.parent.mkdir(parents=True); old.write_text('过期结论只在旧快照中')
        result = search_with_diaries(self.root, '过期结论')
        self.assertEqual(result['results'], []); self.assertTrue(old.exists())

    def test_only_fixed_sources_copied_index_and_log_included(self):
        (self.root/'OUR_STORY.md').write_text('日期索引')
        detail=self.root/'development-logs/2026-10/2026-10-28.md'
        detail.parent.mkdir(parents=True); detail.write_text('客观验证记录')
        (self.root/'OWNER.md').write_text('不能被同步的资料')
        (self.root/'config.json').write_text('也不能被同步')
        self.assertIn('验证',search_with_diaries(self.root,'验证')['results'][0]['text'])
        self.assertEqual(synchronize(self.root)['files'],3)
        self.assertFalse(search_with_diaries(self.root,'不能被同步')['results'])

    def test_group_search_never_reads_or_copies_private_diaries(self):
        group=self.root/'memory/groups/20001'; group.mkdir(parents=True)
        (group/'note.md').write_text('本群公开内容')
        with patch('qq_chat.diary_sync.synchronize',side_effect=AssertionError('must not sync')):
            result=search_with_diaries(self.root,'公开',group_id=20001)
        self.assertEqual(result['results'][0]['file'],'groups/20001/note.md')
        self.assertFalse(self.copy.exists())

    def test_invalid_query_and_busy_organizer_do_not_write_copies(self):
        with self.assertRaises(ValueError):search_with_diaries(self.root,' ')
        self.assertFalse((self.root/'.automation').exists())
        area=self.root/'.automation'; area.mkdir()
        with (area/'nightly.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'正在整理'):search_with_diaries(self.root,'纸飞机')
        self.assertFalse(self.copy.exists())

    def test_unsafe_or_invalid_source_fails_before_changing_any_copy(self):
        search_with_diaries(self.root,'纸飞机'); before=self.copy.read_bytes()
        outside=Path(self.temp.name)/'outside.md'; outside.write_text('外面的文件')
        self.diary.unlink(); self.diary.symlink_to(outside)
        with self.assertRaises(ValueError):search_with_diaries(self.root,'纸飞机')
        self.assertEqual(self.copy.read_bytes(),before)
        self.diary.unlink(); self.diary.write_bytes(b'\xff')
        with self.assertRaises(ValueError):search_with_diaries(self.root,'纸飞机')
        self.assertEqual(self.copy.read_bytes(),before)

    def test_destination_symlink_and_source_hardlink_do_not_escape(self):
        outside=Path(self.temp.name)/'outside'; outside.mkdir()
        (self.root/'memory').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):synchronize(self.root)
        self.assertEqual(list(outside.iterdir()),[])
        (self.root/'memory').unlink()
        linked=Path(self.temp.name)/'linked.md'; os.link(self.diary,linked)
        with self.assertRaises(ValueError):synchronize(self.root)

    def test_incomplete_scan_or_copy_failure_never_searches_stale_data(self):
        search_with_diaries(self.root,'纸飞机'); before=self.copy.read_bytes()
        with patch('qq_chat.diary_sync.MAX_RECORDS',0), patch('qq_chat.diary_sync.search') as lookup:
            with self.assertRaises(ValueError):search_with_diaries(self.root,'纸飞机')
            lookup.assert_not_called()
        self.assertEqual(self.copy.read_bytes(),before)
        self.diary.write_text('修改后的文件')
        with patch('qq_chat.diary_sync.replace_copy',side_effect=OSError()), patch('qq_chat.diary_sync.search') as lookup:
            with self.assertRaises(ValueError):search_with_diaries(self.root,'修改')
            lookup.assert_not_called()


class DiaryToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ft.FrontToolsTests.asyncSetUp
    asyncTearDown = ft.FrontToolsTests.asyncTearDown
    setup_tools = ft.FrontToolsTests.setup_tools
    token = ft.FrontToolsTests.token

    async def test_real_cli_reports_busy_diary_instead_of_expired_authorization(self):
        server = await self.setup_tools()
        area = self.root / 'workspace/.automation'; area.mkdir()
        with (area / 'nightly.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            async with server.grant(self.owner, memory_only=True) as grant:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I',
                    str(Path(__file__).parents[1] / 'tools/memory_search.py'),
                    '--grant', grant.config_path, '--query', '日记',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                output, error = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(json.loads(output), {'error': '日记正在整理，请稍后再查'})
                self.assertEqual(error, b'')
        self.assertFalse((self.root / 'workspace/memory').exists())

    async def test_authenticated_private_search_updates_group_call_does_not(self):
        server=await self.setup_tools()
        diary=self.root/'workspace/our-story/2026-10/2026-10-28.md'
        diary.parent.mkdir(parents=True); diary.write_text('私人的新日记测试内容')
        async with server.grant(self.group,memory_only=True) as grant:
            result=await server.call(self.token(grant),'memory_search',{'query':'私人的'})
            self.assertFalse(result['results'])
        self.assertFalse((self.root/'workspace/memory/private/diary-current').exists())
        async with server.grant(self.owner,memory_only=True) as grant:
            result=await server.call(self.token(grant),'memory_search',{'query':'私人的'})
            self.assertIn('新日记',result['results'][0]['text'])
            diary.write_text('私人的更新日记测试内容')
            result=await server.call(self.token(grant),'memory_search',{'query':'更新日记'})
            self.assertIn('更新日记',result['results'][0]['text'])

"""Memory retrieval boundaries and the real per-turn Unix/CLI tool path."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.memory import search
from tests import test_front_tools as front_tests


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'memory'
        for folder in ('shared', 'private', 'groups/20001', 'groups/20002'):
            (self.root / folder).mkdir(parents=True)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def test_scope_and_literal_search_include_source(self):
        self.write('shared/public.md', '标题\n共同关键词\n')
        self.write('private/private.md', '共同关键词 主人私事')
        self.write('groups/20001/local.md', '共同关键词 本群')
        self.write('groups/20002/other.md', '共同关键词 别群')
        self.write('unscoped.md', '共同关键词 未分类不能读')
        private = search(self.root, '共同关键词')['results']
        group = search(self.root, '共同关键词', group_id=20001)['results']
        self.assertEqual({r['file'] for r in private}, {'shared/public.md', 'private/private.md'})
        self.assertEqual({r['file'] for r in group}, {'shared/public.md', 'groups/20001/local.md'})
        self.assertEqual(next(r['line'] for r in group if r['file'].startswith('shared')), 2)
        self.assertEqual(search(self.root, '[.*]')['results'], [])

    def test_symlinks_hardlinks_special_files_and_external_scope_are_not_read(self):
        outside = Path(self.tmp.name) / 'outside.md'
        outside.write_text('外部秘密')
        (self.root / 'shared/link.md').symlink_to(outside)
        (self.root / 'shared/folder').symlink_to(outside.parent, target_is_directory=True)
        os.link(outside, self.root / 'shared/hard.md')
        os.mkfifo(self.root / 'shared/pipe.md')
        self.assertFalse(search(self.root, '秘密')['results'])
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): search(alias, '秘密')

    def test_scope_symlink_cannot_expose_private_to_group(self):
        self.write('private/a.md', '秘密')
        (self.root / 'groups/20001').rmdir()
        (self.root / 'groups/20001').symlink_to(self.root / 'private', target_is_directory=True)
        result = search(self.root, '秘密', group_id=20001)
        self.assertEqual(result['results'], [])
        self.assertTrue(result['partial'])

    def test_limits_ranking_normalization_and_match_excerpt(self):
        for number in range(9):
            self.write(f'shared/{number}.md', 'x' * 2000 + 'SEED ７ preference' + '尾' * 1000)
        self.write('shared/best.md', 'seed 7 偏好 回答 简短')
        result = search(self.root, 'seed 7 偏好')
        self.assertEqual(result['results'][0]['file'], 'shared/best.md')
        self.assertLessEqual(len(result['results']), 5)
        self.assertLessEqual(sum(len(r['text']) for r in result['results']), 2000)
        self.assertTrue(all('seed' in r['text'].casefold() for r in result['results']))

    def test_invalid_and_empty_results_are_distinct_from_unavailable(self):
        for query in ('', ' ', 'x' * 201, None, ' '.join(str(i) for i in range(9))):
            with self.subTest(query=query), self.assertRaises(ValueError): search(self.root, query)
        with self.assertRaises(ValueError): search(self.root, 'x', group_id='../private')
        self.assertEqual(search(self.root, '不存在')['status'], 'no_results')
        self.assertEqual(search(self.root / 'missing', 'x')['status'], 'not_configured')

    def test_budget_and_invalid_files_report_partial(self):
        self.write('shared/large.md', 'x' * 300000)
        (self.root / 'shared/bad.md').write_bytes(b'\xff')
        self.assertTrue(search(self.root, 'x')['partial'])
        self.write('shared/a.md', '命中')
        self.write('shared/b.md', '命中')
        with patch('qq_chat.memory.MAX_FILES', 1):
            result = search(self.root, '命中')
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['results']), 1)

    def test_heading_cannot_hide_answer_and_keyword_context_is_returned(self):
        self.write('private/answer.md', '## 私聊记忆验收暗号\n\n说明：用于测试。\n私聊记忆验收暗号：SYNTHETIC-ANSWER\n')
        result = search(self.root, '私聊记忆验收暗号')
        self.assertIn('SYNTHETIC-ANSWER', result['results'][0]['text'])
        self.assertEqual(result['results'][0]['line'], 4)
        self.write('private/context.md', '## 测试关键词\n\n答案另起一行\nVALUE-IN-CONTEXT\n')
        self.assertIn('VALUE-IN-CONTEXT', search(self.root, '测试关键词')['results'][0]['text'])

    def test_separated_answers_in_one_file_and_result_budget(self):
        self.write('private/multi.md', ('关键词 answer-FIRST\n' + '无关内容\n' * 20
                   + '关键词 answer-SECOND\n' + '无关内容\n' * 20
                   + '关键词 answer-THIRD\n' + '无关内容\n' * 20 + '关键词 answer-FOURTH'))
        result = search(self.root, '关键词')
        combined = '\n'.join(r['text'] for r in result['results'])
        self.assertIn('answer-FIRST', combined)
        self.assertIn('answer-SECOND', combined)
        self.assertEqual(len(result['results']), 3)
        self.assertLessEqual(sum(len(r['text']) for r in result['results']), 2000)
        self.assertTrue(all(len(r['text']) <= 500 for r in result['results']))



# Reuse setup helpers rather than inheriting and silently re-running all tests.
class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = front_tests.FrontToolsTests.asyncSetUp
    asyncTearDown = front_tests.FrontToolsTests.asyncTearDown
    setup_tools = front_tests.FrontToolsTests.setup_tools
    token = front_tests.FrontToolsTests.token

    async def test_authenticated_scope_query_validation_and_memory_only_grant(self):
        server = await self.setup_tools()
        root = self.root / 'workspace/memory'
        for folder in ('private', 'shared', 'groups/20001'):
            (root / folder).mkdir(parents=True)
            (root / folder / 'note.md').write_text('关键词 ' + folder)
        async with server.grant(self.group, memory_only=True) as grant:
            token = self.token(grant)
            listed = await server.call(token, 'tools/list', {})
            self.assertEqual([r['name'] for r in listed], ['memory_search'])
            result = await server.call(token, 'memory_search', {'query': '关键词'})
            self.assertEqual({r['file'] for r in result['results']}, {'shared/note.md', 'groups/20001/note.md'})
            for arguments in ({'query': '关键词', 'group_id': 2}, {'query': True}, {'query': 'x', 'root': '/'}):
                with self.assertRaises(ValueError): await server.call(token, 'memory_search', arguments)
            with self.assertRaises(ValueError): await server.call(token, 'dispatch_task', {'goal': 'not allowed'})
        with self.assertRaises(ValueError): await server.call(token, 'memory_search', {'query': '关键词'})
        async with server.grant(self.owner, memory_only=True) as grant:
            token = self.token(grant)
            self.cfg['memory'] = {'enabled': False}
            with self.assertRaisesRegex(ValueError, '已关闭'):
                await server.call(token, 'memory_search', {'query': '关键词'})
            self.cfg['memory'] = {'enabled': True}
            source, conversation, _ = server.grants[token]
            server.grants[token] = (source, conversation, 0)
            with self.assertRaisesRegex(ValueError, '已失效'):
                await server.call(token, 'memory_search', {'query': '关键词'})
        with self.assertRaises(ValueError):
            async with server.grant(self.guest, memory_only=True): pass

    async def test_real_cli_works_only_during_grant_without_identity_or_path_arguments(self):
        server = await self.setup_tools()
        folder = self.root / 'workspace/memory/private'
        folder.mkdir(parents=True)
        (folder / 'preference.md').write_text('解释时先说简短重点，按需展开。')
        command = [sys.executable, '-I', str(Path(__file__).parents[1] / 'tools/memory_search.py')]
        async with server.grant(self.owner, memory_only=True) as grant:
            async def run():
                proc = await asyncio.create_subprocess_exec(*command, '--grant', grant.config_path,
                         '--query', '简短', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), 5)
                return proc.returncode, json.loads(out), err
            code, result, err = await run()
            self.assertEqual(code, 0)
            self.assertEqual(result['results'][0]['file'], 'private/preference.md')
            self.assertIn('按需展开', result['results'][0]['text'])
            self.assertEqual(err, b'')
            source, conversation, _ = server.grants[self.token(grant)]
            server.grants[self.token(grant)] = (source, conversation, 0)
            code, result, err = await run()
            self.assertEqual(code, 1)
            self.assertEqual(result['code'], 'grant_expired')
            self.assertEqual(err, b'')
        code, result, err = await run()
        self.assertEqual(code, 1)
        self.assertEqual(set(result), {'error', 'code'})
        self.assertEqual(result['code'], 'grant_missing')
        self.assertEqual(err, b'')

    async def test_full_turn_has_only_memory_command_and_revokes_after_turn(self):
        server = await self.setup_tools()
        self.cfg['agents']['back'] = []
        self.cfg['agents']['profiles']['front']['type'] = 'codex_app'
        await self.bridge.chat(self.owner, '回忆一下')
        req = self.front.requests[-1]
        self.assertIn('memory_search.py', req.instructions)
        self.assertIn('查询记忆默认静默', req.instructions)
        self.assertIsNotNone(req.on_activity)
        self.assertEqual(req.level, 'full')
        self.assertIsNone(req.grant)  # Does not change the FULL provider tool policy.
        self.assertFalse(server.grants)
        await self.bridge.chat(self.guest, '回忆一下')
        self.assertNotIn('memory_search.py', self.front.requests[-1].instructions)
        self.cfg['memory'] = {'enabled': False}
        await self.bridge.chat(self.owner, '回忆一下')
        self.assertNotIn('memory_search.py', self.front.requests[-1].instructions)


    async def test_consecutive_turns_use_fresh_grant_and_do_not_authorize_guest(self):
        server = await self.setup_tools()
        self.cfg['agents']['back'] = []
        self.cfg['agents']['profiles']['front']['type'] = 'codex_app'
        paths = []
        for _ in range(2):
            await self.bridge.chat(self.owner, '回忆一下')
            req = self.front.requests[-1]
            self.assertIn('替代历史中的命令和路径', req.runtime_instructions)
            import shlex
            command_line = next(line for line in req.runtime_instructions.splitlines() if ' --grant ' in line)
            words = shlex.split(command_line)
            path = words[words.index('--grant') + 1]
            paths.append(path)
            self.assertFalse(Path(path).exists())
            self.assertFalse(server.grants)
        self.assertNotEqual(paths[0], paths[1])
        await self.bridge.chat(self.guest, '回忆一下')
        self.assertNotIn(' --grant ', self.front.requests[-1].runtime_instructions)


    async def test_cli_errors_distinguish_bad_config_from_unreachable_service(self):
        command = [sys.executable, '-I', str(Path(__file__).parents[1] / 'tools/memory_search.py')]
        path = self.root / 'bad-grant.json'
        for content, expected in [('invalid-json', 'grant_invalid'),
                (json.dumps({'mcpServers': {'qq': {'env': {'QQ_TOOL_SOCKET': str(self.root/'absent.sock'),
                  'QQ_TOOL_TOKEN': 'synthetic-test-only'}}}}), 'service_unavailable')]:
            path.write_text(content)
            proc = await asyncio.create_subprocess_exec(*command, '--grant', str(path), '--query', 'test',
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 5)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(out)['code'], expected)
            self.assertNotIn(b'synthetic-test-only', out)
            self.assertEqual(err, b'')

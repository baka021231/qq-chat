"""Per-turn capability boundary using real Unix/MCP transport and fake agents."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from qq_chat.agents.base import FRONT, NONE
from qq_chat.bridge import Source
from qq_chat.tool_server import FRONT_TOOLS, ToolServer, tool_definitions
import tests.test_tasks as task_tests


class FrontToolsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_tests.TaskTests.asyncSetUp
    until = task_tests.TaskTests.until
    create = task_tests.TaskTests.create
    row = task_tests.TaskTests.row

    async def asyncTearDown(self):
        await self.bridge.tool_server.close()
        await task_tests.TaskTests.asyncTearDown(self)

    async def setup_tools(self):
        await self.bridge.tool_server.start()
        self.cfg['agents']['profiles']['front']['type'] = 'claude_code'
        return self.bridge.tool_server

    def token(self, grant):
        return json.loads(Path(grant.config_path).read_text())['mcpServers']['qq']['env']['QQ_TOOL_TOKEN']

    async def test_grant_permissions_revoke_expiry_and_foreign_source(self):
        server = await self.setup_tools()
        self.assertEqual(server.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(server.socket.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            async with server.grant(self.guest): pass
        async with server.grant(self.owner) as grant:
            token = self.token(grant)
            self.assertEqual(Path(grant.config_path).stat().st_mode & 0o777, 0o600)
            self.assertEqual({'mcp__qq__'+t['name'] for t in await server.call(token, 'tools/list', {})}, FRONT_TOOLS)
            with self.assertRaises(ValueError): await server.call('forged', 'list_tasks', {})
            source, key, _ = server.grants[token]; server.grants[token] = (source, key, 0)
            with self.assertRaises(ValueError): await server.call(token, 'list_tasks', {})
        self.assertFalse(Path(grant.config_path).exists())
        with self.assertRaises(ValueError): await server.call(token, 'list_tasks', {})

    async def test_calls_cannot_supply_another_chat_or_target(self):
        server = await self.setup_tools()
        async with server.grant(self.owner) as grant:
            for name, args in [('dispatch_task', {'goal':'goal','conversation':'other'}),
                               ('poke', {'user_id':30001}), ('poke', {'group_id':20001}),
                               ('task_status', {'id':True}), ('dispatch_task', {'goal':'goal','agent':'other'})]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    await server.call(self.token(grant), name, args)
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    async def test_real_task_creation_produces_bridge_receipt_and_bound_source(self):
        server = await self.setup_tools()
        async with server.grant(self.group) as grant:
            result = await server.call(self.token(grant), 'dispatch_task', {'goal':'repair README'})
            self.assertEqual(result['receipt'], 'sent')
            self.assertEqual(self.row(result['id'])['conversation'], self.bridge.conversation(self.group))
            self.assertEqual(self.sent, [(self.group, '【已下发任务 #1 · work】repair README')])
            with self.assertRaises(ValueError):
                async with server.grant(self.owner) as other:
                    await server.call(self.token(other), 'task_status', {'id':result['id']})
            self.assertEqual(len(await server.call(self.token(grant), 'list_tasks', {})), 1)

    async def test_failed_receipt_does_not_lose_task_or_instruct_duplicate_dispatch(self):
        server = await self.setup_tools(); self.fail_send = True
        async with server.grant(self.owner) as grant:
            result = await server.call(self.token(grant), 'dispatch_task', {'goal':'goal'})
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['receipt'], 'pending')
        self.assertEqual(self.row(1)['receipt_sent'], 0)
        self.fail_send = False
        self.assertTrue(await self.bridge.task_receipt(1))
        self.assertTrue(await self.bridge.task_receipt(1))
        self.assertEqual(len(self.sent), 1)

    async def test_poke_can_only_target_owner_in_this_chat_and_api_failures_propagate(self):
        server = await self.setup_tools(); calls = []
        async def onebot(name, params): calls.append((name, params)); return {}
        self.bridge.onebot_call = onebot
        for source in (self.owner, self.group):
            async with server.grant(source) as grant:
                await server.call(self.token(grant), 'poke', {})
        self.assertEqual(calls, [('send_poke', {'user_id':'10001'}),
                                ('send_poke', {'user_id':'10001', 'group_id':'20001'})])
        async def fail(*args): raise RuntimeError('QQ rejected')
        self.bridge.onebot_call = fail
        async with server.grant(self.owner) as grant:
            with self.assertRaises(RuntimeError): await server.call(self.token(grant), 'poke', {})

    async def test_actual_stdio_shim_works_under_python_isolation(self):
        server = await self.setup_tools()
        async def onebot(*args): return {}
        self.bridge.onebot_call = onebot
        async with server.grant(self.owner) as grant:
            env = os.environ.copy();env.update(json.loads(Path(grant.config_path).read_text())['mcpServers']['qq']['env'])
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', str(Path(__file__).parents[1]/'qq_chat/front_tools.py'),
                          env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            requests = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'poke','arguments':{}}},
                        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'poke','arguments':{'target':3}}}]
            out, err = await asyncio.wait_for(process.communicate(('\n'.join(json.dumps(r) for r in requests)+'\n').encode()), 5)
            replies = [json.loads(line) for line in out.splitlines()]
            self.assertEqual(len(replies[1]['result']['tools']), 6)
            self.assertFalse(replies[2]['result'].get('isError', False))
            self.assertTrue(replies[3]['result']['isError'])
            self.assertEqual(err, b'')

    async def test_owner_front_has_grant_guests_and_reports_do_not(self):
        await self.setup_tools()
        await self.bridge.chat(self.owner, 'hello')
        req = self.front.requests[-1]
        self.assertEqual(req.level, FRONT)
        self.assertIsNotNone(req.grant)
        self.assertFalse(Path(req.grant.config_path).exists())
        await self.bridge.chat(self.guest, 'hello')
        self.assertEqual(self.front.requests[-1].level, NONE)
        self.assertIsNone(self.front.requests[-1].grant)
        self.back.release.set(); ident = await self.create()
        await self.until(lambda:self.row(ident)['reported'])
        report = self.front.requests[-1]
        self.assertEqual(report.level, NONE)
        self.assertIsNone(report.grant)
        self.assertFalse(self.bridge.tool_server.grants)

    async def test_cancelling_foreground_revokes_token_and_removes_config(self):
        server = await self.setup_tools();self.front.release.clear()
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        config = self.front.requests[-1].grant.config_path
        self.assertTrue(server.grants)
        turn.cancel();await asyncio.gather(turn,return_exceptions=True)
        self.assertFalse(server.grants)
        self.assertFalse(Path(config).exists())

    async def test_model_text_cannot_create_task_or_bridge_receipt(self):
        await self.setup_tools();self.front.reply = '已经交给后台，任务 #999'
        await self.bridge.chat(self.owner, 'hello')
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertFalse(any('【已下发任务' in text for _,text in self.sent))

    async def test_send_to_task_is_next_turn_same_session_and_holds_writer(self):
        server = await self.setup_tools(); ident=await self.create()
        await self.until(lambda:self.back.requests)
        async with server.grant(self.owner) as grant:
            result=await server.call(self.token(grant),'send_to_task',{'id':ident,'message':'also verify tests'})
            self.assertEqual(result['status'],'queued_for_next_turn')
            self.assertEqual(len(self.back.requests),1)
            with self.assertRaises(ValueError):
                async with server.grant(self.group) as other:
                    await server.call(self.token(other),'send_to_task',{'id':ident,'message':'other'})
        self.back.release.set(); await self.until(lambda:self.row(ident)['reported'])
        self.assertEqual(len(self.back.requests),2)
        self.assertIn('also verify tests',self.back.requests[1].text)
        self.assertEqual(self.back.requests[0].session_id,self.back.requests[1].session_id)

    async def test_dispatch_respects_limits_and_failed_calls_leave_no_extra_task(self):
        server=await self.setup_tools()
        async with server.grant(self.owner) as grant:
            token=self.token(grant)
            for _ in range(3):await server.call(token,'dispatch_task',{'goal':'goal'})
            with self.assertRaises(ValueError):await server.call(token,'dispatch_task',{'goal':'overflow'})
        self.assertEqual(self.bridge.db.execute('SELECT count(*) FROM tasks').fetchone()[0],3)

    async def test_audit_does_not_log_token_or_argument_text(self):
        server=await self.setup_tools()
        async def onebot(*args):return {}
        self.bridge.onebot_call=onebot
        async with server.grant(self.owner) as grant:
            token=self.token(grant)
            with self.assertLogs(level='INFO') as logs:
                await server.call(token,'poke',{})
            self.assertIn('Front tool completed', '\n'.join(logs.output))
            self.assertNotIn(token, '\n'.join(logs.output))

    async def test_stop_does_not_execute_queued_additional_instructions(self):
        await self.setup_tools();ident=await self.create()
        await self.until(lambda:self.back.requests)
        self.bridge.tasks.send_message(self.bridge.conversation(self.owner),ident,'additional')
        await self.bridge.tasks.stop(self.bridge.conversation(self.owner),ident)
        await self.until(lambda:self.row(ident)['reported'])
        self.assertEqual(len(self.back.requests),1)
        self.assertIn('尚未执行的追加要求',self.bridge.report_input(self.row(ident)))

    async def test_socket_rejects_replacing_regular_file(self):
        server=self.bridge.tool_server;server.folder.mkdir()
        server.socket.write_text('preserve')
        with self.assertRaises(ValueError):await server.start()
        self.assertEqual(server.socket.read_text(),'preserve')

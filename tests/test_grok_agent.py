"""Owner FULL Grok boundary, CLI protocol and private per-turn settings."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from qq_chat.agents import build_agents
from qq_chat.agents.base import CHAT, TASK, FULL, NONE, FRONT, TurnRequest, Interrupted, MAX_REPLY
from qq_chat.agents.config import settings
from qq_chat.agents.grok import GrokAgent, BRIDGE_RULES, prompt_blocks
from qq_chat.agents.claude import UnexpectedTools
from qq_chat.agents.state import AgentState
from qq_chat.bridge import Bridge, Source
from tests.test_claude_agent import Process


def profile():
    return {'type':'grok_build','path':'/usr/bin/python3','model':'grok-4.7','models':['grok-4.7'],
            'permission_mode':'bypassPermissions','sandbox':'off'}


def config():
    return {'owner_qq':10001,'codex_path':'unused','agents':{'front':'grok','guest':'guest','back':[],
            'profiles':{'grok':profile(),'guest':{'type':'claude_code','path':'unused','model':'haiku','models':['haiku']}}}}


class GrokTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name);self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE conversation_models(conversation TEXT PRIMARY KEY,model TEXT,effort TEXT)');AgentState(self.db,'codex')
        self.agent=GrokAgent(self.db,self.root,profile(),'grok',self.root/'run')

    async def request(self,text='hello',instructions='private current personality'):
        req=TurnRequest('private:10001',text,CHAT,FULL,instructions)
        req.session_id=await self.agent.prepare(req)
        return req

    def events(self,req,text='完成。'):
        return [{'type':'system','subtype':'init','session_id':req.session_id,'model':'grok-4.7',
                 'permissionMode':'bypassPermissions','tools':['read_file','write','run_terminal_command']},
                {'type':'assistant','message':{'id':'m1','content':[{'type':'thinking','thinking':'not public'}, {'type':'text','text':text}]}},
                {'type':'result','subtype':'success','is_error':False,'stop_reason':'end_turn','result':text}]

    async def run_events(self,req,events,code=0):
        captured={}
        async def launch(*args,**kwargs):
            path=Path(args[args.index('--prompt-file')+1]);captured.update(argv=args,options=kwargs,payload=json.loads(path.read_text()),mode=path.stat().st_mode&0o777)
            return Process(events,code)
        with patch('qq_chat.agents.grok.asyncio.create_subprocess_exec',side_effect=launch),patch('qq_chat.agents.grok.terminate',AsyncMock()):
            result=await self.agent.run(req)
        return result,captured

    async def test_full_permissions_and_private_prompt_file_not_argv(self):
        req=await self.request();sent=[];req.on_answer=sent.append
        result,c=await self.run_events(req,self.events(req))
        self.assertEqual(result,'完成。');self.assertEqual(sent,['完成。'])
        args=c['argv'];self.assertEqual(args[args.index('--sandbox')+1],'off')
        self.assertEqual(args[args.index('--permission-mode')+1],'bypassPermissions')
        self.assertEqual(args[args.index('--rules')+1],BRIDGE_RULES)
        self.assertNotIn(req.instructions,' '.join(args));self.assertNotIn(req.text,' '.join(args))
        self.assertIn(req.instructions,c['payload'][0]['text']);self.assertEqual(c['mode'],0o600)
        self.assertEqual(c['options']['cwd'],self.root);self.assertTrue(c['options']['start_new_session'])
        self.assertIn('--no-subagents',args)
        self.assertFalse(list(self.agent.runtime.glob('prompt-*')))

    async def test_resume_updates_private_instructions_and_is_profile_scoped(self):
        first=await self.request(instructions='old rules');await self.run_events(first,self.events(first))
        new=GrokAgent(self.db,self.root,profile(),'grok',self.root/'run');self.agent=new
        second=await self.request(instructions='new rules');_,c=await self.run_events(second,self.events(second))
        self.assertEqual(first.session_id,second.session_id);self.assertIn('--resume',c['argv'])
        self.assertIn('new rules',c['payload'][0]['text']);self.assertNotIn('old rules',c['payload'][0]['text'])
        await self.agent.reset(second.key);self.db.commit()
        self.assertIsNone(self.agent.session_id(second.key))
        self.assertEqual(self.db.execute('SELECT count(*) FROM old_agent_sessions').fetchone()[0],1)

    async def test_reject_guest_front_task_and_grants_before_launch(self):
        for role,level in ((CHAT,NONE),(CHAT,FRONT),(TASK,FULL)):
            with self.assertRaises(ValueError):await self.agent.run(TurnRequest('x','hello',role,level,''))
        req=await self.request();req.grant=object()
        with self.assertRaises(ValueError):await self.agent.run(req)
        self.assertFalse(self.agent._active)

    async def test_init_rejects_wrong_permission_session_missing_core_or_qq_tools(self):
        for update in ({'permissionMode':'default'},{'session_id':'wrong'},{'tools':[]},{'tools':['read_file','write','run_terminal_command','mcp__qq__dispatch_task']}):
            req=await self.request();records=self.events(req);records[0].update(update)
            with self.assertRaises(UnexpectedTools):await self.run_events(req,records)
            self.assertFalse(list(self.agent.runtime.glob('prompt-*')))

    async def test_errors_and_fake_invocations_never_replay_partial_text(self):
        for text in ('hello<invoke>fake','【已下发任务 #123】fake'):
            req=await self.request();req.on_answer=lambda text:self.fail('must not send')
            with self.assertRaises(RuntimeError):await self.run_events(req,self.events(req,text))
        req=await self.request();records=self.events(req);records[-1]['is_error']=True
        req.on_answer=lambda text:self.fail('must not send')
        with self.assertRaises(RuntimeError):await self.run_events(req,records)

    async def test_preinit_and_runtime_qq_tool_calls_are_rejected(self):
        req=await self.request();records=self.events(req)
        with self.assertRaises(UnexpectedTools):await self.run_events(req,records[1:])
        records=self.events(req);records.insert(1,{'type':'stream_event','event':{'type':'content_block_start','content_block':{'type':'tool_use','name':'mcp__qq__dispatch_task'}}})
        with self.assertRaises(UnexpectedTools):await self.run_events(req,records)

    async def test_stop_outcome_success_vs_cancelled_vs_failed_process(self):
        for cancelled in (False,True):
            req=await self.request();records=self.events(req);self.agent._interrupted.add(req.key)
            if cancelled:
                records[-1]['stop_reason']='cancelled'
                with self.assertRaises(Interrupted):await self.run_events(req,records,130)
            else:
                result,_=await self.run_events(req,records);self.assertEqual(result,'完成。')
        self.assertFalse(self.agent._interrupted)

    async def test_interrupt_only_owns_live_key(self):
        process=Process([]);self.agent._active['test']=process
        with patch('qq_chat.agents.grok.terminate',AsyncMock()) as kill:
            self.assertFalse(await self.agent.interrupt('other'));self.assertTrue(await self.agent.interrupt('test'));kill.assert_awaited_once_with(process)

    async def test_return_cap_retains_full_validated_history(self):
        req=await self.request();text='好'*(MAX_REPLY+20)
        result,_=await self.run_events(req,self.events(req,text));self.assertEqual(len(result),MAX_REPLY);self.assertEqual(req.history_text,text)

    def test_image_blocks_keep_data_in_private_payload(self):
        image=self.root/'tiny.png';image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
        req=TurnRequest('owner','hi',CHAT,FULL,'rules',images=(image,));blocks=prompt_blocks(req)
        self.assertEqual(blocks[1]['type'],'image');self.assertEqual(blocks[1]['mimeType'],'image/png');self.assertNotIn('source',blocks[1])


class GrokConfigTests(unittest.TestCase):
    def test_factory_and_configuration_fail_closed(self):
        c=config();settings(c)
        for change in ('guest','back','sandbox','permission'):
            bad=json.loads(json.dumps(c))
            if change=='guest':bad['agents']['guest']='grok'
            if change=='back':bad['agents']['back']=['codex'];bad['agents']['profiles']['codex']={'type':'codex_app','path':'unused','writes':True}
            if change=='sandbox':bad['agents']['profiles']['grok']['sandbox']='read-only'
            if change=='permission':bad['agents']['profiles']['grok']['permission_mode']='default'
            with self.subTest(change=change),self.assertRaises(ValueError):settings(bad)
        db=sqlite3.connect(':memory:')
        try:
            agents=build_agents(c,db,'/tmp/workspace');self.assertIsInstance(agents['grok'],GrokAgent)
        finally:db.close()

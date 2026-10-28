import shlex
import unittest

from qq_chat.memory_progress import MemoryProgress, SCRIPT, memory_command
from qq_chat.codex_app import AppServer, progress_text


def command(query='暗号'):
    return shlex.join(['python3', '-I', str(SCRIPT), '--grant', '/private/runtime.json', '--query', query])


class MemoryProgressTests(unittest.TestCase):
    def test_only_direct_search_is_silent_not_other_commands(self):
        self.assertTrue(memory_command(command('a; echo unsafe')))
        self.assertTrue(memory_command(shlex.join(['/bin/bash', '-lc', command()])))
        for value in (command() + '; touch /tmp/not-memory', 'echo memory_search.py',
                      command().replace(str(SCRIPT), '/tmp/memory_search.py'),
                      command() + ' --root /', None):
            self.assertFalse(memory_command(value))

    def test_memory_only_discards_preface_and_progress(self):
        sent = []; gate = MemoryProgress(sent.append)
        gate.progress('我查一下记忆')
        gate.activity({'type': 'agentMessage'})
        gate.activity({'type': 'commandExecution', 'command': command()})
        gate.progress('查到了')
        self.assertEqual(sent, [])

    def test_real_work_flushes_preface_and_stays_visible(self):
        sent = []; gate = MemoryProgress(sent.append)
        gate.progress('我来修改代码')
        gate.activity({'type': 'commandExecution', 'command': 'python3 -m unittest'})
        gate.progress('测试通过')
        self.assertEqual(sent, ['我来修改代码', '测试通过'])

    def test_memory_then_work_and_unknown_tools_remain_visible(self):
        sent = []; gate = MemoryProgress(sent.append)
        gate.progress('先查记忆')
        gate.activity({'type': 'mcpToolCall', 'server': 'qq', 'tool': 'memory_search'})
        gate.progress('查完了')
        gate.activity({'type': 'dynamicToolCall', 'tool': 'some-new-tool'})
        gate.progress('开始实际工作')
        self.assertEqual(sent, ['开始实际工作'])

    def test_tool_details_hidden_even_when_opted_in_but_other_work_kept(self):
        for method in ('item/started', 'item/completed'):
            self.assertIsNone(progress_text(method, {'item': {'type': 'commandExecution',
                'command': command(), 'status': 'failed', 'exitCode': 1}}, show_tools=True))
        self.assertIsNotNone(progress_text('item/started', {'item': {
            'type': 'commandExecution', 'command': 'python3 -m unittest'}}, show_tools=True))


class ProviderProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_codex_event_flow_keeps_answer_and_execution_error(self):
        for work, failed in ((False, False), (True, False), (False, True)):
            app = AppServer('unused');sent=[];answers=[];gate=MemoryProgress(sent.append)
            async def call(method, params):
                if method != 'turn/start': return {}
                q=app.events['thread']
                events=[('item/completed', {'item': {'type': 'agentMessage', 'phase': 'commentary',
                                                    'text': '准备中'}}),
                        ('item/started', {'item': {'type': 'commandExecution',
                                                   'command': 'python3 -m unittest' if work else command()}}),
                        ('item/started', {'item': {'type': 'agentMessage', 'id': 'a', 'phase': 'final_answer'}}),
                        ('item/agentMessage/delta', {'itemId': 'a', 'delta': '答案'}),
                        ('item/completed', {'item': {'type': 'agentMessage','id':'a',
                                                     'phase': 'final_answer','text':'答案'}}),
                        ('turn/completed', {'turn': {'id': 't', 'status':'failed' if failed else 'completed'}})]
                for method, params in events:q.put_nowait({'method':method,'params':params})
                return {'turn': {'id': 't'}}
            app.call=call
            if failed:
                with self.assertRaises(RuntimeError):
                    await app.turn('thread','question',on_progress=gate.progress,on_activity=gate.activity,on_answer=answers.append)
            else:
                result=await app.turn('thread','question',on_progress=gate.progress,on_activity=gate.activity,on_answer=answers.append)
                self.assertEqual(result,'答案')
            self.assertEqual(sent, ['准备中'] if work else [])
            self.assertEqual(''.join(answers), '答案')

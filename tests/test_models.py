"""Model commands keep thread history and cannot grant management to guests."""
import asyncio
import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.bridge import Bridge, Incoming, Source
from qq_chat.models import available_models


class FakeApp:
    def __init__(self):
        self.calls, self.threads = [], {}
        self.reject = None

    async def start(self):
        pass

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == 'model/list':
            return {'data': [{'model': 'model-a', 'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': [{'reasoningEffort': x} for x in ('low', 'medium', 'high')]},
                             {'model': 'model-b', 'defaultReasoningEffort': 'low', 'supportedReasoningEfforts': [{'reasoningEffort': x} for x in ('low', 'high')]}], 'nextCursor': None}
        if method in ('thread/unsubscribe', 'thread/inject_items'):
            return {}
        if params.get('model') == self.reject and self.reject:
            raise RuntimeError('Selected model unavailable')
        if method == 'thread/start':
            thread = 'thread-' + str(len(self.threads))
            self.threads[thread] = params.get('model', 'model-a')
        elif method == 'thread/resume':
            thread = params['threadId']
            self.threads[thread] = params.get('model', self.threads.get(thread, 'model-a'))
        else:
            raise AssertionError(method)
        return {'thread': {'id': thread}, 'model': self.threads[thread]}


class ModelCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base_patch = patch('qq_chat.bridge.BASE', Path(self.folder.name))
        self.base_patch.start()
        self.cfg = {'owner_qq': 123, 'codex_path': 'unused'}
        self.bridge = Bridge(self.cfg)
        self.bridge.agents['codex'].app = self.app = FakeApp()
        self.sent = []
        async def send(source, text):
            self.sent.append(text)
        self.bridge.send = send
        self.worker = asyncio.create_task(self.bridge.worker())
        self.owner = Source('private', 123, 123)

    async def asyncTearDown(self):
        self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.worker
        self.bridge.db.close()
        self.base_patch.stop()
        self.folder.cleanup()

    async def dispatch(self, text, source=None):
        self.bridge.queue.put_nowait(Incoming(source or self.owner, text))
        await asyncio.wait_for(self.bridge.queue.join(), 2)

    def selection(self):
        return self.bridge.db.execute('SELECT model, effort FROM conversation_models WHERE conversation="private:123"').fetchone()

    async def test_list_status_and_switch_preserve_thread_and_history(self):
        await self.dispatch('/models')
        self.assertIn('model-b', self.sent[-1])
        await self.dispatch('/model')
        self.assertIn('model-a', self.sent[-1])
        original = dict(self.bridge.db.execute('SELECT conversation, thread_id FROM threads'))
        self.bridge.db.execute('INSERT INTO history VALUES (?, ?, ?)', ('user', 'keep history', 'private:123'))
        self.bridge.db.commit()
        await self.dispatch('/model model-b')
        self.assertEqual(self.selection(), ('model-b', 'low'))
        self.assertEqual(dict(self.bridge.db.execute('SELECT conversation, thread_id FROM threads')), original)
        self.assertEqual(self.bridge.db.execute('SELECT content FROM history').fetchone()[0], 'keep history')
        await self.bridge.thread_for(self.owner, True)
        resume = [params for method, params in self.app.calls if method == 'thread/resume'][-1]
        self.assertEqual(resume['model'], 'model-b')
        self.assertEqual(resume['config']['model_reasoning_effort'], 'low')
        self.assertEqual(resume['sandbox'], 'danger-full-access')
        await self.dispatch('/model')
        self.assertIn('model-b', self.sent[-1])

    async def test_default_restores_effective_backend_default(self):
        await self.dispatch('/model model-b')
        await self.dispatch('/model default')
        self.assertEqual(self.selection()[0], 'model-a')
        self.assertTrue(any(method == 'thread/unsubscribe' for method, _ in self.app.calls))

    async def test_preference_survives_restart_new_and_other_conversations(self):
        await self.dispatch('/model model-b')
        restarted = Bridge(dict(self.cfg))
        restarted.agents['codex'].app = self.app
        try:
            thread = await restarted.thread_for(self.owner, True)
            self.assertEqual(restarted.agent.model_state(thread)[0], 'model-b')
        finally:
            restarted.db.close()
        await self.dispatch('/new')
        self.assertEqual(self.selection()[0], 'model-b')
        thread = await self.bridge.thread_for(self.owner, True)
        self.assertEqual(self.app.threads[thread], 'model-b')
        await self.bridge.thread_for(Source('group', 999, 123), True)
        self.assertNotIn('model', self.app.calls[-1][1])
        await self.bridge.thread_for(Source('private', 789, 789), False)
        self.assertNotIn('model', self.app.calls[-1][1])
        self.assertEqual(self.app.calls[-1][1]['sandbox'], 'read-only')

    async def test_invalid_selection_and_backend_failure_do_not_persist(self):
        await self.dispatch('/model model-a')
        previous = self.selection()
        await self.dispatch('/model unknown')
        self.assertIn('不在当前可选列表', self.sent[-1])
        self.assertEqual(self.selection(), previous)
        call = self.app.call
        async def unavailable_catalog(method, params):
            if method == 'model/list':
                raise RuntimeError('Catalog unavailable')
            return await call(method, params)
        self.app.call = unavailable_catalog
        await self.dispatch('/model model-b')
        self.assertIn('未完成', self.sent[-1])
        self.assertEqual(self.selection(), previous)
        await self.dispatch('/model model-a extra')
        self.assertIn('用法', self.sent[-1])

    async def test_can_recover_from_unavailable_previous_model(self):
        self.bridge.db.execute('INSERT INTO conversation_models VALUES (?, ?, ?)', ('private:123', 'retired', 'high'))
        self.bridge.db.commit()
        self.app.reject = 'retired'
        await self.dispatch('/model model-b')
        self.assertEqual(self.selection()[0], 'model-b')

    async def test_fresh_thread_without_rollout_is_recreated_without_losing_history(self):
        self.bridge.db.execute('INSERT INTO threads VALUES (?, ?)', ('private:123', 'empty-thread'))
        self.bridge.db.execute('INSERT INTO history VALUES (?, ?, ?)', ('user', 'saved history', 'private:123'))
        self.bridge.db.commit()
        call = self.app.call
        async def empty_thread(method, params):
            if method == 'thread/resume' and params.get('threadId') == 'empty-thread':
                raise RuntimeError('no rollout found for thread id empty-thread')
            return await call(method, params)
        self.app.call = empty_thread
        await self.dispatch('/model model-b')
        await self.bridge.thread_for(self.owner, True)
        self.assertEqual(self.selection()[0], 'model-b')
        self.assertEqual(self.bridge.db.execute('SELECT content FROM history').fetchone()[0], 'saved history')

    async def test_each_turn_explicitly_passes_selected_model_and_effort(self):
        await self.dispatch('/model model-b')
        captured = []
        async def turn(thread, text, **options):
            captured.append(options)
            return 'OK'
        self.app.turn = turn
        await self.bridge.run_turn(self.owner, 'thread', 'hello', False, ())
        self.assertEqual(captured[-1]['model'], 'model-b')
        self.assertEqual(captured[-1]['effort'], 'low')
        await self.bridge.run_turn(Source('private', 789, 789), 'other', 'hello', False, ())
        self.assertNotIn('model', captured[-1])

    async def test_effort_switch_validation_default_and_persistence(self):
        await self.dispatch('/model model-b')
        await self.dispatch('/effort high')
        self.assertEqual(self.selection(), ('model-b', 'high'))
        await self.dispatch('/effort')
        self.assertIn('high', self.sent[-1])
        await self.dispatch('/effort medium')
        self.assertEqual(self.selection(), ('model-b', 'high'))
        await self.dispatch('/effort high extra')
        self.assertIn('用法', self.sent[-1])
        await self.dispatch('/new')
        restarted = Bridge(dict(self.cfg))
        try:
            self.assertEqual(restarted.db.execute('SELECT model, effort FROM conversation_models').fetchone(), ('model-b', 'high'))
        finally:
            restarted.db.close()
        captured = []
        async def turn(thread, text, **options):
            captured.append(options)
            return 'OK'
        self.app.turn = turn
        await self.bridge.run_turn(self.owner, 'thread', 'hello', False, ())
        self.assertEqual(captured[-1]['effort'], 'high')
        self.assertEqual(captured[-1]['model'], 'model-b')
        await self.dispatch('/effort default')
        self.assertEqual(self.selection(), ('model-b', 'low'))
        await self.dispatch('/effort high')
        await self.dispatch('/model model-a')
        self.assertEqual(self.selection(), ('model-a', 'medium'))

    async def test_effort_without_selection_and_failure(self):
        await self.dispatch('/effort')
        self.assertIsNone(self.selection())
        await self.dispatch('/effort high')
        self.assertEqual(self.selection(), ('model-a', 'high'))
        call = self.app.call
        async def failed_catalog(method, params):
            if method == 'model/list':
                raise RuntimeError('Unavailable')
            return await call(method, params)
        self.app.call = failed_catalog
        await self.dispatch('/effort low')
        self.assertIn('未完成', self.sent[-1])
        self.assertEqual(self.selection(), ('model-a', 'high'))
        self.app.call = call
        group = Source('group', 999, 123)
        await self.dispatch('/effort low', group)
        self.assertEqual(self.selection(), ('model-a', 'high'))
        self.assertEqual(self.bridge.db.execute('SELECT effort FROM conversation_models WHERE conversation=?', (self.bridge.conversation(group),)).fetchone(), ('low',))

    async def test_guests_cannot_query_models_or_change_preferences(self):
        for guest in (Source('group', 999, 789, '心糖'), Source('private', 789, 789, '心糖')):
            for command in ('/models', '/model', '/model model-b', '/effort', '/effort high', '/effort default'):
                await self.dispatch(command, guest)
                self.assertIn('仅限主人', self.sent[-1])
            await self.bridge.effort_command(guest, '/effort high')
            self.assertIn('仅限主人', self.sent[-1])
            await self.bridge.model_command(guest, '/model model-b')
            self.assertIn('仅限主人', self.sent[-1])
        self.assertEqual(self.app.calls, [])
        self.assertIsNone(self.selection())


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_and_hidden_models(self):
        class Catalog(FakeApp):
            async def call(self, method, params):
                if params.get('cursor') == 'next':
                    return {'data': [{'model': 'second'}], 'nextCursor': None}
                return {'data': [{'model': 'first'}, {'model': 'hidden', 'hidden': True}], 'nextCursor': 'next'}
        self.assertEqual(list(await available_models(Catalog())), ['first', 'second'])

    async def test_repeated_cursor_fails_instead_of_looping(self):
        class Catalog(FakeApp):
            async def call(self, method, params):
                return {'data': [], 'nextCursor': 'same'}
        with self.assertRaises(RuntimeError):
            await available_models(Catalog())


if __name__ == '__main__':
    unittest.main()

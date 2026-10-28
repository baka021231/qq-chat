"""Codex adapter preserving the bridge's existing thread and turn semantics."""
import contextlib
from pathlib import Path
import time

from ..models import available_models, configured_default
from .base import Agent, CHAT, TASK, FULL, MAX_REPLY, NONE, TurnRequest


class CodexAgent(Agent):
    def __init__(self, db, cwd, executable, *, app=None, name='codex', profile=None, legacy=True):
        self.name = name
        self.db = db
        self.cwd = Path(cwd)
        from ..codex_app import AppServer
        self.app = app if app is not None else AppServer(executable)
        self.profile, self.legacy = dict(profile or {}), legacy
        self._models = {}
        self._efforts = {}
        self._task_sessions = {}

    async def start(self):
        await self.app.start()

    def session_id(self, key):
        if key.startswith('task:'):
            return self._task_sessions.get(key)
        if self.legacy:
            row = self.db.execute('SELECT thread_id FROM threads WHERE conversation=?', (key,)).fetchone()
        else:
            row = self.db.execute('SELECT session_id FROM agent_sessions WHERE conversation=? AND profile=?',
                                  (key, self.name)).fetchone()
        return row[0] if row else None

    def model_state(self, session_id):
        return self._models.get(session_id), self._efforts.get(session_id)

    async def prepare(self, req: TurnRequest):
        if req.role not in (CHAT, TASK) or req.level not in (FULL, NONE) or (req.role == TASK and req.level != FULL):
            raise ValueError('Unsupported Codex role/level')
        options = {'cwd': str(self.cwd), 'approvalPolicy': 'never',
                   'sandbox': self.profile.get('sandbox', 'danger-full-access') if req.level == FULL else 'read-only',
                   'developerInstructions': req.instructions}
        await self.start()
        if req.model is not None:
            options['model'] = req.model
            if req.effort is not None:
                options['config'] = {'model_reasoning_effort': req.effort}
        thread = self.session_id(req.key)
        if thread:
            try:
                result = await self.app.call('thread/resume', {'threadId': thread, **options})
            except RuntimeError as error:
                if 'no rollout found for thread id' not in str(error):
                    raise
                with contextlib.suppress(Exception):
                    await self.app.call('thread/unsubscribe', {'threadId': thread})
            else:
                self._models[thread] = result.get('model')
                self._efforts[thread] = result.get('reasoningEffort')
                return thread
        result = await self.app.call('thread/start', {**options, 'ephemeral': False})
        thread = result['thread']['id']
        if req.role == TASK:
            self._task_sessions[req.key] = thread
        elif self.legacy:
            self.db.execute('INSERT INTO threads VALUES (?, ?) ON CONFLICT(conversation) '
                            'DO UPDATE SET thread_id=excluded.thread_id', (req.key, thread))
        else:
            self.db.execute('INSERT INTO agent_sessions VALUES (?, ?, ?) ON CONFLICT(conversation, profile) '
                            'DO UPDATE SET session_id=excluded.session_id', (req.key, self.name, thread))
        self.db.commit()
        self._models[thread] = result.get('model')
        self._efforts[thread] = result.get('reasoningEffort')
        return thread

    async def run(self, req: TurnRequest):
        thread = req.session_id if req.session_id is not None else await self.prepare(req)
        if req.runtime_instructions:
            # Resuming an already-loaded thread does not refresh its developer
            # instructions. Append the current capability before starting work.
            # Failure must stop the turn rather than reuse a revoked old path.
            await self.app.call('thread/inject_items', {
                'threadId': thread, 'items': [{'type': 'message', 'role': 'developer',
                'content': [{'type': 'input_text', 'text': req.runtime_instructions}]}]})
        options = {'model': req.model, 'effort': req.effort} if req.model is not None else {}
        if req.on_runtime is not None:
            options['on_started'] = req.on_runtime
        if req.on_activity is not None:
            options['on_activity'] = req.on_activity
        reply = await self.app.turn(thread, req.text, on_progress=req.on_progress,
                                    on_answer=req.on_answer, show_tools=req.show_tools,
                                    image_paths=req.images, **options)
        # Only the returned value is capped; callbacks still receive the full stream.
        return reply[:MAX_REPLY]

    async def reset(self, key):
        if self.legacy:
            self.db.execute('INSERT INTO old_threads SELECT conversation, thread_id, ? '
                            'FROM threads WHERE conversation=?', (time.time(), key))
            self.db.execute('DELETE FROM threads WHERE conversation=?', (key,))
        else:
            self.db.execute('INSERT INTO old_agent_sessions SELECT conversation, profile, session_id, ? '
                            'FROM agent_sessions WHERE conversation=? AND profile=?', (time.time(), key, self.name))
            self.db.execute('DELETE FROM agent_sessions WHERE conversation=? AND profile=?', (key, self.name))

    async def models(self):
        return list((await available_models(self.app)).values())

    async def default_model(self):
        return await configured_default(self.app, self.cwd)

    async def check_execution(self, session, runtime_ref=None):
        """One read-only RPC, never resume, interrupt, retry or release a task."""
        await self.start()
        result = await self.app.call('thread/read', {'threadId': session, 'includeTurns': True})
        turns = result.get('thread', {}).get('turns', [])
        turn_id = (runtime_ref or {}).get('turn_id')
        matches = [turn for turn in turns if turn.get('id') == turn_id] if turn_id else turns
        # PR3 creates exactly one turn per task thread. Never infer from another turn.
        if len(matches) != 1:
            return 'unknown'
        status = matches[0].get('status')
        return status if status in ('inProgress', 'completed', 'failed', 'interrupted') else 'unknown'

    async def interrupt(self, key):
        thread = self.session_id(key)
        return await self.app.interrupt(thread) if thread else False

    async def close(self):
        await self.app.close()

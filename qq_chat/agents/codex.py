"""Codex adapter preserving the bridge's existing thread and turn semantics."""
import contextlib
from pathlib import Path
import time

from ..codex_app import AppServer
from ..models import available_models, configured_default
from .base import Agent, CHAT, FULL, MAX_REPLY, NONE, TurnRequest


class CodexAgent(Agent):
    def __init__(self, db, cwd, executable, *, app=None, name='codex'):
        self.name = name
        self.db = db
        self.cwd = Path(cwd)
        self.app = app if app is not None else AppServer(executable)
        self._models = {}
        self._efforts = {}

    async def start(self):
        await self.app.start()

    def session_id(self, key):
        row = self.db.execute('SELECT thread_id FROM threads WHERE conversation=?', (key,)).fetchone()
        return row[0] if row else None

    def model_state(self, session_id):
        return self._models.get(session_id), self._efforts.get(session_id)

    async def prepare(self, req: TurnRequest):
        # PR1 exposes only the legacy CHAT/FULL and CHAT/NONE modes.
        if req.role != CHAT or req.level not in (FULL, NONE):
            raise ValueError('Only legacy Codex chat modes are implemented in PR1')
        options = {'cwd': str(self.cwd), 'approvalPolicy': 'never',
                   'sandbox': 'danger-full-access' if req.level == FULL else 'read-only',
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
        self.db.execute('INSERT INTO threads VALUES (?, ?) ON CONFLICT(conversation) '
                        'DO UPDATE SET thread_id=excluded.thread_id', (req.key, thread))
        self.db.commit()
        self._models[thread] = result.get('model')
        self._efforts[thread] = result.get('reasoningEffort')
        return thread

    async def run(self, req: TurnRequest):
        thread = req.session_id if req.session_id is not None else await self.prepare(req)
        options = {'model': req.model, 'effort': req.effort} if req.model is not None else {}
        reply = await self.app.turn(thread, req.text, on_progress=req.on_progress,
                                    on_answer=req.on_answer, show_tools=req.show_tools,
                                    image_paths=req.images, **options)
        # Only the returned value is capped; callbacks still receive the full stream.
        return reply[:MAX_REPLY]

    async def reset(self, key):
        self.db.execute('INSERT INTO old_threads SELECT conversation, thread_id, ? '
                        'FROM threads WHERE conversation=?', (time.time(), key))
        self.db.execute('DELETE FROM threads WHERE conversation=?', (key,))

    async def models(self):
        return list((await available_models(self.app)).values())

    async def default_model(self):
        return await configured_default(self.app, self.cwd)

    async def close(self):
        await self.app.close()

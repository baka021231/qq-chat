"""Read the daemon's model catalog and resolve its configured default."""
import contextlib


async def available_models(app):
    await app.start()
    models, seen = {}, set()
    cursor = None
    while True:
        params = {'limit': 100, 'includeHidden': False}
        if cursor:
            params['cursor'] = cursor
        result = await app.call('model/list', params)
        for item in result.get('data', []):
            name = item.get('model')
            if name and not item.get('hidden', False):
                models[name] = item
        cursor = result.get('nextCursor')
        if not cursor:
            return models
        if cursor in seen:
            raise RuntimeError('Model catalog repeated its cursor')
        seen.add(cursor)


async def configured_default(app, cwd):
    """Resolve config without a model turn or modifying the user's live thread."""
    await app.start()
    result = await app.call('thread/start', {'cwd': str(cwd), 'ephemeral': True,
                                            'sandbox': 'read-only', 'approvalPolicy': 'never'})
    try:
        model = result.get('model')
        if not model:
            raise RuntimeError('Default model was not returned')
        return model
    finally:
        with contextlib.suppress(Exception):
            await app.call('thread/unsubscribe', {'threadId': result['thread']['id']})

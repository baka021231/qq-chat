"""Validate supported agent profiles before launching any child process."""
import re
import math


def settings(cfg):
    if 'agents' not in cfg:
        return 'codex', 'codex', {'codex': {'type': 'codex_app', 'path': cfg['codex_path']}}
    agents = cfg['agents']
    if not isinstance(agents, dict) or not isinstance(agents.get('profiles'), dict):
        raise ValueError('agents.profiles must be an object')
    if not isinstance(agents.get('back', []), list):
        raise ValueError('agents.back must be a list')
    back = agents.get('back', [])
    if len(back) > 1:
        raise ValueError('Multiple background profiles are not implemented until PR5')
    if not all(isinstance(name, str) for name in back):
        raise ValueError('Invalid background profile')
    front, guest = agents.get('front'), agents.get('guest', agents.get('front'))
    group_guest = agents.get('group_guest', guest)
    profiles = dict(agents['profiles'])
    if not isinstance(front, str) or not isinstance(guest, str) or front not in profiles or guest not in profiles:
        raise ValueError('front and guest must reference defined profiles')
    if not isinstance(group_guest, str) or group_guest not in profiles:
        raise ValueError('group_guest must reference a defined profile')
    if any(name not in profiles for name in back):
        raise ValueError('Unknown background profile')
    for name, profile in profiles.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
            raise ValueError('Invalid agent profile name')
        if not isinstance(profile, dict) or profile.get('type') not in ('codex_app', 'claude_code', 'grok_build'):
            raise ValueError('Unknown agent profile type')
        profile = dict(profile)
        profiles[name] = profile
        if profile['type'] == 'codex_app' and 'path' not in profile:
            profile['path'] = cfg.get('codex_path')
        if not isinstance(profile.get('path'), str) or not profile['path']:
            raise ValueError('Agent executable path is required')
        if profile['type'] == 'codex_app':
            if profile.get('sandbox', 'danger-full-access') not in ('read-only', 'workspace-write', 'danger-full-access'):
                raise ValueError('Invalid Codex sandbox')
        else:
            models = profile.get('models', [])
            if not isinstance(models, list) or not models:
                raise ValueError('CLI profiles require a model catalog')
            names = [m if isinstance(m, str) else m.get('model') if isinstance(m, dict) else None for m in models]
            if not all(isinstance(m, str) and m and not m.startswith('-') for m in names):
                raise ValueError('Invalid Claude model catalog')
            if len(set(names)) != len(names):
                raise ValueError('Duplicate Claude model names')
            if profile.get('model') not in names:
                raise ValueError('CLI default model must appear in its catalog')
            if profile['type'] == 'claude_code' and (name == front or name in back) and profile.get('permission_mode') not in (
                    'dontAsk', 'acceptEdits', 'auto', 'manual', 'plan', 'bypassPermissions'):
                raise ValueError('FULL Claude profile requires an explicit permission_mode')
            if not isinstance(profile.get('tools', 'default'), str):
                raise ValueError('Claude tools must be an explicit CLI tool string')
            limit = profile.get('max_turns', 6)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError('max_turns must be a positive integer')
            timeout = profile.get('timeout_seconds', 3600)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('timeout_seconds must be positive and finite')
            for item in models:
                if isinstance(item, dict):
                    levels = item.get('supportedReasoningEfforts', [])
                    if not isinstance(levels, list) or not all(isinstance(level, dict) and isinstance(level.get('reasoningEffort'), str) for level in levels):
                        raise ValueError('Invalid configured reasoning efforts')
    groks = [name for name, p in profiles.items() if p['type'] == 'grok_build']
    if groks and (any(name in back for name in groks) or guest in groks or group_guest in groks or (front in groks and back)):
        raise ValueError('Grok supports only owner single-agent front, separate non-Grok guest and empty back')
    for name in groks:
        if profiles[name].get('permission_mode') != 'bypassPermissions' or profiles[name].get('sandbox') != 'off':
            raise ValueError('Grok owner mode requires explicit bypassPermissions and sandbox=off')
    for name in back:
        p = profiles[name]
        if not isinstance(p.get('writes'), bool):
            raise ValueError('Background profiles require explicit writes')
        if p.get('max_parallel', 1) != 1 or isinstance(p.get('max_parallel', 1), bool):
            raise ValueError('Background max_parallel must be 1 until PR5')
        if p['type'] == 'codex_app':
            can_write = p.get('sandbox', 'danger-full-access') != 'read-only'
        else:
            # Claude FULL can have user MCP tools: no reliable readonly sandbox.
            can_write = True
        if p['writes'] != can_write:
            raise ValueError('Background writes does not match sandbox/tool capabilities')
    limits = cfg.get('tasks', {})
    if not isinstance(limits, dict):
        raise ValueError('tasks must be an object')
    for key, default in (('max_running', 4), ('max_per_conversation', 3), ('report_chars', 4000)):
        value = limits.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError('Invalid tasks.' + key)
    if limits.get('max_writers', 1) != 1 or isinstance(limits.get('max_writers', 1), bool):
        raise ValueError('Only one global writer is supported')
    minutes = limits.get('timeout_minutes', 60)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError('Invalid task timeout')
    if limits.get('progress', 'direct') not in ('direct', 'silent'):
        raise ValueError('Invalid tasks.progress')
    codex_paths = {p['path'] for p in profiles.values() if p['type'] == 'codex_app'}
    if len(codex_paths) > 1:
        raise ValueError('Codex profiles must share the daemon executable')
    return front, guest, profiles


def legacy_codex_profile(cfg, db=None):
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_metadata'").fetchone():
        row = db.execute("SELECT value FROM agent_metadata WHERE key='legacy_codex_profile'").fetchone()
        if row:
            return row[0]
    if 'agents' not in cfg:
        return 'codex'
    front, _, profiles = settings(cfg)
    if profiles[front]['type'] == 'codex_app':
        return front
    return next((name for name, p in profiles.items() if p['type'] == 'codex_app'),
                'codex' if 'codex' not in profiles else '__legacy_codex__')

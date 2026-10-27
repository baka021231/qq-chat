"""Build the existing single Codex agent; additional profiles arrive in PR2."""
from .base import Agent, CHAT, FULL, NONE, TurnRequest
from .codex import CodexAgent


def build_agents(cfg, db, cwd):
    # Do not silently accept a future configuration that would grant the wrong mode.
    if 'agents' in cfg:
        raise ValueError('Agent profile configuration is not implemented in PR1')
    return {'codex': CodexAgent(db, cwd, cfg['codex_path'])}

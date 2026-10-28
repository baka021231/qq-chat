"""Build validated front, guest and background profiles with shared Codex transport."""
from pathlib import Path

from .base import Agent, CHAT, FULL, NONE, TurnRequest
from .codex import CodexAgent
from .claude import ClaudeCodeAgent
from .grok import GrokAgent
from .config import legacy_codex_profile, settings


def build_agents(cfg, db, cwd):
    _, _, profiles = settings(cfg)
    legacy = legacy_codex_profile(cfg, db)
    agents, transport = {}, None
    for name, profile in profiles.items():
        if profile['type'] == 'codex_app':
            agent = CodexAgent(db, cwd, profile['path'], app=transport, name=name,
                               profile=profile, legacy=name == legacy)
            transport = agent.app
        elif profile['type'] == 'grok_build':
            agent = GrokAgent(db, cwd, profile, name, Path(cwd).parent / 'run/grok')
        else:
            agent = ClaudeCodeAgent(db, cwd, profile, name, Path(cwd).parent / 'run/claude')
        agents[name] = agent
    return agents

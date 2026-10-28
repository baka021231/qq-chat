"""Agent boundary; QQ policy and delivery belong to Bridge."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

NONE, FRONT, FULL = 'none', 'front', 'full'
CHAT, TASK = 'chat', 'task'
MAX_REPLY = 12000


class Interrupted(Exception):
    """Execution has confirmed an interrupted outcome."""


class ExecutionUncertain(RuntimeError):
    """The transport cannot confirm that execution has ended."""


class MissingSession(RuntimeError):
    """Resume failed before execution; Bridge may re-import bounded history once."""


@dataclass
class TurnRequest:
    key: str
    text: str
    role: str
    level: str
    instructions: str
    images: tuple = ()
    model: str | None = None
    effort: str | None = None
    on_progress: Callable[[str], None] | None = None
    on_answer: Callable[[str], None] | None = None
    grant: object | None = None
    # Bridge prepares a session before composing its legacy history input.
    session_id: str | None = None
    show_tools: bool = False
    history_text: str | None = None
    on_runtime: Callable[[dict], None] | None = None
    on_activity: Callable[[dict], None] | None = None
    # Trusted per-turn context, separate from thread configuration/history.
    runtime_instructions: str = ''


class Agent(ABC):
    name: str

    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def prepare(self, req: TurnRequest) -> str: ...

    @abstractmethod
    async def run(self, req: TurnRequest) -> str: ...

    @abstractmethod
    def session_id(self, key: str) -> str | None: ...

    @abstractmethod
    def model_state(self, session_id: str) -> tuple[str | None, str | None]: ...

    @abstractmethod
    async def reset(self, key: str):
        """Stage session archival in the caller's database transaction."""

    @abstractmethod
    async def models(self) -> list[dict]: ...

    @abstractmethod
    async def default_model(self) -> str: ...

    async def interrupt(self, key: str) -> bool:
        raise NotImplementedError('This backend does not implement interruption')

    @abstractmethod
    async def close(self): ...

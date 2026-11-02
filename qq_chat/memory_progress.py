"""Quiet memory lookups without hiding execution or changing answer streams."""
from pathlib import Path
import shlex

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/memory_search.py'
SPEAK_SCRIPT = SCRIPT.with_name('speak.py')


def memory_command(command):
    """Recognize only the direct, read-only command issued in this turn's rules."""
    try:
        args = shlex.split(command)
        if len(args) == 3 and Path(args[0]).name in ('bash', 'sh') and args[1] in ('-lc', '-c'):
            args = shlex.split(args[2])
        if (len(args) != 7 or Path(args[0]).name not in ('python', 'python3')
                or args[1] != '-I' or args[2] != str(SCRIPT)
                or args[3] != '--grant' or args[5] != '--query'):
            return False
        return bool(args[4] and args[6])
    except (TypeError, ValueError):
        return False


def speak_command(command):
    """Same shape as the speak command issued in this turn's rules."""
    try:
        args = shlex.split(command)
        if len(args) == 3 and Path(args[0]).name in ('bash', 'sh') and args[1] in ('-lc', '-c'):
            args = shlex.split(args[2])
        flags = (['--grant', '--style', '--intensity', '--text'],
                 ['--grant', '--style', '--intensity', '--scene', '--text'])
        return (len(args) in (11, 13) and Path(args[0]).name in ('python', 'python3')
                and args[1] == '-I' and args[2] == str(SPEAK_SCRIPT)
                and args[3::2] in flags and all(args[4::2]))
    except (TypeError, ValueError):
        return False


def memory_item(item):
    """Memory lookups and speaking are chat, not work worth narrating."""
    if item.get('type') == 'commandExecution':
        command = item.get('command', '')
        return memory_command(command) or speak_command(command)
    if item.get('type') == 'mcpToolCall':
        return item.get('server') == 'qq' and item.get('tool') in ('memory_search', 'speak')
    return False


class MemoryProgress:
    """Buffer narration until actual non-memory work starts.

    No prompt/content heuristics: activity comes from the execution provider.
    Unknown tools count as work. This does not filter final answers or errors.
    """
    def __init__(self, send):
        self.send = send
        self.pending = []
        self.working = False
        self.memory = False

    def progress(self, text):
        if self.working:
            self.send(text)
        elif not self.memory and len(self.pending) < 128:
            self.pending.append(text)

    def activity(self, item):
        if item.get('type') in ('agentMessage', 'reasoning', 'plan'):
            return
        if memory_item(item):
            self.memory = True
            self.pending.clear()
            return
        if not self.working:
            self.working = True
            for text in self.pending:
                self.send(text)
            self.pending.clear()

"""Run the repository's local owner admin operations from any working directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.admin import main

if __name__ == '__main__':
    raise SystemExit(main())

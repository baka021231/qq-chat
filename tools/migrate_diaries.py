#!/usr/bin/env python3
"""Offline owner deployment helper; defaults to validation without copying."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.diary_migration import migrate

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(migrate(Path(__file__).resolve().parents[1] / 'workspace', apply=args.apply))

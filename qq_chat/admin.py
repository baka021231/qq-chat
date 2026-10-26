"""Small local operations for owner-authorized sessions; never print secrets."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile

BASE = Path(__file__).resolve().parents[1]
FIELDS = {'group': 'allowed_groups', 'private': 'private_whitelist'}


def whitelist(path, scope, operation, ident=None):
    if scope not in FIELDS or operation not in ('list', 'add', 'remove'):
        raise ValueError('不支持的白名单操作')
    if operation != 'list':
        ident = str(ident)
        if not ident.isascii() or not ident.isdecimal() or int(ident) <= 0:
            raise ValueError('号码必须为正整数')
        ident = str(int(ident))
    path = Path(path)
    # Share the blacklist writer's lock, preserving concurrent changes.
    fd = os.open(path.parent / '.group-blacklist.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = json.loads(path.read_text())
        key = FIELDS[scope]
        original = cfg.get(key, [])
        if not isinstance(original, list):
            raise ValueError('白名单配置必须为列表')
        entries = {str(int(value)) for value in original}
        changed = False
        if operation == 'add' and ident not in entries:
            entries.add(ident)
            changed = True
        elif operation == 'remove' and ident in entries:
            entries.remove(ident)
            changed = True
        if changed:
            cfg[key] = sorted(entries, key=int)
            fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(cfg, out, ensure_ascii=False, indent=2)
                    out.write('\n')
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {'ok': True, 'scope': scope, 'operation': operation,
                'entries': sorted(entries, key=int), 'changed': changed,
                'restart_required': changed,
                'allow_all_groups': cfg.get('allow_all_groups', True)}


def main():
    parser = argparse.ArgumentParser(description='仅供主人授权会话使用的本地白名单工具')
    parser.add_argument('scope', choices=FIELDS)
    parser.add_argument('operation', choices=('list', 'add', 'remove'))
    parser.add_argument('ident', nargs='?')
    args = parser.parse_args()
    try:
        result = whitelist(BASE / 'config.json', args.scope, args.operation, args.ident)
    except Exception:
        # Config/connection exception text may contain private data.
        print(json.dumps({'ok': False, 'error': '操作失败，请检查号码、配置格式和文件权限'}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0

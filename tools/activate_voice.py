"""Restart after the current reply is committed; validate or roll back.

Run as a separate systemd user unit: restarting the bridge kills its cgroup.
This tool sends an activation result only to the explicitly supplied target.
"""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def command(*args):
    return subprocess.run(args, cwd=BASE, capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def reply_finished(db, conversation, after_row, marker):
    rows = db.execute('SELECT role, content FROM history WHERE conversation=? AND rowid>? ORDER BY rowid',
                      (conversation, after_row)).fetchall()
    for index, (role, content) in enumerate(rows[:-1]):
        if role == 'user' and marker in content and rows[index + 1][0] == 'assistant':
            return True
    return False


def disable_voice():
    path = BASE / 'config.json'
    with (BASE / '.group-blacklist.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = json.loads(path.read_text())
        cfg.setdefault('voice', {})['enabled'] = False
        fd, temporary = tempfile.mkstemp(prefix='.config-', dir=BASE)
        try:
            with os.fdopen(fd, 'w') as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(cfg, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def wait_connected(old_pid):
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        pid = command('systemctl', '--user', 'show', 'qq-bridge.service', '-p', 'MainPID', '--value')
        if pid not in ('0', old_pid):
            # The journal invocation is scoped to the new process, not old successes.
            logs = command('journalctl', '--user', '-u', 'qq-bridge.service', '_PID=' + pid,
                           '-n', '30', '--no-pager', '-o', 'cat')
            if 'Connected to NapCat' in logs:
                time.sleep(3)
                if pid == command('systemctl', '--user', 'show', 'qq-bridge.service', '-p', 'MainPID', '--value'):
                    command('systemctl', '--user', 'is-active', 'qq-bridge.service')
                    command('/usr/bin/python3', str(BASE / 'tools/verify_onebot.py'))
                    return
        time.sleep(1)
    raise RuntimeError('Bridge did not reconnect')


async def notify(group, text, speech=False):
    from websockets.asyncio.client import connect
    cfg = json.loads((BASE / 'config.json').read_text())
    if speech:
        from qq_chat.bridge import Source
        from tools.test_voice_delivery import deliver
        await deliver(cfg, Source('group', group, int(cfg['owner_qq'])), text)
        return
    # Remains usable even after git revert removes the new voice modules.
    async with connect(cfg['ws_url'], additional_headers={
        'Authorization': 'Bearer ' + cfg['onebot_token']}, proxy=None) as ws:
        echo = 'voice-activation-result'
        await ws.send(json.dumps({'action': 'send_group_msg', 'params': {
            'group_id': group, 'message': [{'type': 'text', 'data': {'text': text}}]}, 'echo': echo}))
        async with asyncio.timeout(30):
            async for payload in ws:
                event = json.loads(payload)
                if event.get('echo') == echo:
                    if event.get('status') != 'ok' or event.get('retcode') != 0:
                        raise RuntimeError('QQ rejected activation notice')
                    return
        raise RuntimeError('QQ disconnected before activation notice')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', type=int, required=True)
    parser.add_argument('--after-row', type=int, required=True)
    parser.add_argument('--marker', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    conversation = 'group:' + str(args.group) + ':owner'
    deadline = time.monotonic() + 600
    with sqlite3.connect(BASE / 'chat.sqlite3') as db:
        while not reply_finished(db, conversation, args.after_row, args.marker):
            if time.monotonic() > deadline:
                asyncio.run(notify(args.group, '语音发送测试已通过，但未等到当前回复结束，自动重启已取消。'))
                return
            time.sleep(1)
    old_pid = command('systemctl', '--user', 'show', 'qq-bridge.service', '-p', 'MainPID', '--value')
    if command('git', 'rev-parse', 'HEAD') != args.commit or command('git', 'status', '--porcelain'):
        asyncio.run(notify(args.group, '源码在等待期间发生变化，自动重启已取消；语音发送测试已通过。'))
        return
    try:
        command('systemctl', '--user', 'restart', 'qq-bridge.service')
        wait_connected(old_pid)
        asyncio.run(notify(args.group, '语音模块已启用，桥接重启及连接检查通过。以后正常回复会同时发送文字和对应语音。', speech=True))
    except Exception:
        disable_voice()
        rolled_back = False
        if command('git', 'rev-parse', 'HEAD') == args.commit and not command('git', 'status', '--porcelain'):
            try:
                command('git', 'revert', '--no-edit', args.commit)
                rolled_back = True
            except subprocess.SubprocessError:
                pass
        command('systemctl', '--user', 'restart', 'qq-bridge.service')
        wait_connected(old_pid)
        text = ('语音启用检查失败，已关闭语音并回滚代码，文字桥接已恢复。' if rolled_back
                else '语音启用检查失败，已关闭语音，文字桥接已恢复；源码有变化，未自动回滚。')
        asyncio.run(notify(args.group, text))
        raise SystemExit(1)
    print('Voice activation, reconnect and live delivery checks passed')


if __name__ == '__main__':
    main()

"""Restart only this QQ bridge from the user's WSL terminal; never send test messages."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
PACKAGE = 'qq_chat'
# Instances started before the qq_chat package existed ran this script directly.
LEGACY_SCRIPT = BASE / 'qq_bridge.py'
LOG = BASE / 'qq_bridge.log'


def bridges():
    result = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            argv = [os.fsdecode(arg) for arg in args if arg]
            if not argv or not Path(argv[0]).name.startswith('python'):
                continue
            cwd = (proc / 'cwd').resolve(strict=True)
            module = any(a == '-m' and b == PACKAGE for a, b in zip(argv[1:], argv[2:]))
            if module and cwd == BASE:
                result.append(int(proc.name))
                continue
            for arg in argv[1:]:
                if Path(arg).name != LEGACY_SCRIPT.name:
                    continue
                path = Path(arg)
                if (path if path.is_absolute() else cwd / path).resolve() == LEGACY_SCRIPT:
                    result.append(int(proc.name))
                    break
        except (OSError, ValueError):
            continue
    return sorted(result)


def main():
    if Path('/proc/1/comm').read_text().strip() == 'codex':
        raise SystemExit('请在你自己的 WSL 终端运行，当前是无法查看宿主机进程的隔离环境。')
    unit = subprocess.run(
        ['systemctl', '--user', 'show', 'qq-bridge.service', '-p', 'LoadState', '--value'],
        capture_output=True, text=True)
    if unit.returncode == 0 and unit.stdout.strip() == 'loaded':
        subprocess.run(['systemctl', '--user', 'restart', 'qq-bridge.service'], check=True)
        print('已通过 systemd 重启桥接服务。')
        print('查看状态：systemctl --user status qq-bridge.service')
        print('查看日志：journalctl --user -u qq-bridge.service -n 50 --no-pager')
        return
    lock = open(BASE / '.bridge-restart.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('已有重启操作正在进行。')
    old = bridges()
    print('已确认旧桥接进程 PID：', old or '无', flush=True)
    if len(old) > 1:
        raise SystemExit('发现多个旧实例，请先确认其启动管理方式；本次未停止任何进程。')
    if old:
        os.kill(old[0], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while old[0] in bridges():
            if time.monotonic() > deadline:
                raise SystemExit('旧进程未正常退出，本次不启动新实例。')
            time.sleep(0.25)
    time.sleep(2)
    if bridges():
        raise SystemExit('检测到桥接实例自动启动，请使用原服务管理方式重启，避免重复实例。')
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'ab', buffering=0) as log:
        offset = os.fstat(log.fileno()).st_size
        child = subprocess.Popen([sys.executable, '-u', '-m', PACKAGE], cwd=BASE,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    print('新桥接进程 PID：', child.pid, flush=True)
    deadline = time.monotonic() + 30
    connected = False
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SystemExit(f'新进程已退出，请查看日志：{LOG}')
        with LOG.open('rb') as log:
            log.seek(offset)
            connected = b'Connected to NapCat' in log.read()
        if connected:
            break
        time.sleep(0.5)
    if not connected:
        raise SystemExit(f'进程已启动，但尚未确认连接 NapCat，请查看日志：{LOG}')
    time.sleep(10)
    if child.poll() is not None or bridges() != [child.pid]:
        raise SystemExit('进程持续运行或唯一实例检查未通过，请检查日志和进程。')
    with LOG.open('rb') as log:
        log.seek(offset)
        lifecycle = [line for line in log.read().splitlines()
                     if b'Connected to NapCat' in line or b'Connection unavailable' in line]
    if not lifecycle or b'Connected to NapCat' not in lifecycle[-1]:
        raise SystemExit(f'连接状态发生变化，请查看日志：{LOG}')
    print('检查通过：新进程持续运行 10 秒，只有一个桥接实例。')
    print('日志已确认：Connected to NapCat')
    print('NapCat 未停止；未发送测试 QQ 消息。')
    print('日志路径：', LOG)


if __name__ == '__main__':
    main()

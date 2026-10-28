"""Owner control plane for persistent frontend changes, independent of model turns."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from .agents.config import settings


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.switch-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def config_lock(root):
    # Share the moderation writer's lock so blacklist changes are not lost.
    fd = os.open(root / '.group-blacklist.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def changed_config(cfg, target):
    front, guest, profiles = settings(cfg)
    if target not in profiles:
        raise ValueError('未知前台配置，请用 /front 查看可选项。')
    new = json.loads(json.dumps(cfg))
    if 'agents' not in new:
        new['agents'] = {'front': front, 'guest': guest, 'profiles': profiles, 'back': []}
    # Pin the previous guest when an older config used the front as its default.
    new['agents'].setdefault('guest', guest)
    new['agents']['front'] = target
    settings(new)
    return new


def choices(cfg):
    _, _, profiles = settings(cfg)
    result = []
    for name in profiles:
        try:
            changed_config(cfg, name)
        except ValueError:
            continue
        result.append(name)
    return result


def job_directory(root):
    directory = root / 'run/front-switch'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def pending_job(directory):
    pointer = directory / 'pending.json'
    if not pointer.exists():
        return False
    current = json.loads(pointer.read_text())
    job = json.loads((directory / (str(uuid.UUID(current['id'])) + '.json')).read_text())
    return job['status'] in ('scheduled', 'running', 'restoring') and time.time() - job['created'] < 300


async def schedule(root, ident):
    # A transient user service survives qq-bridge.service's whole-cgroup stop.
    process = await asyncio.create_subprocess_exec(
        'systemd-run', '--user', '--quiet', '--collect',
        '--unit=qq-front-switch-' + ident, '--on-active=2s',
        '--property=Type=oneshot', '--property=TimeoutStartSec=240',
        '--working-directory=' + str(root),
        sys.executable, '-m', 'qq_chat.front_switch', '--job', ident,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(process.wait(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if code:
        raise RuntimeError('Cannot schedule frontend restart')


async def command(bridge, source, text, root, *, authorized):
    if not authorized:
        await bridge.send(source, '前台切换命令仅限主人使用。')
        return
    if not hasattr(bridge, '_front_switch_lock'):
        bridge._front_switch_lock = asyncio.Lock()
    async with bridge._front_switch_lock:
        job = None
        path = None
        try:
            cfg = json.loads((root / 'config.json').read_text())
            active = settings(bridge.cfg)[0]
            parts = text.split()
            if len(parts) == 1:
                await bridge.send(source, f'当前主人前台：{active}。可选：' + '、'.join(choices(cfg))
                                  + '。用 /front 配置名 切换并自动重启；会影响所有主人会话，访客不变。')
                return
            if len(parts) != 2:
                await bridge.send(source, '用法：/front 或 /front 配置名（例如 /front codex）。')
                return
            target = parts[1]
            changed_config(cfg, target)
            if target == active and settings(cfg)[0] == active:
                await bridge.send(source, f'当前已经是 {active}，无需重启。')
                return
            directory = job_directory(root)
            with config_lock(root):
                if pending_job(directory):
                    raise ValueError('已有前台切换正在进行，请等重启完成后再试。')
                ident = str(uuid.uuid4())
                job = {'id': ident, 'target': target, 'created': time.time(), 'status': 'scheduled'}
                path = directory / (ident + '.json')
                atomic_json(path, job)
                atomic_json(directory / 'pending.json', {'id': ident})
            # No config mutation/restart if QQ acknowledgement cannot be sent.
            await bridge.send(source, f'收到，正在切换主人前台为 {target}；校验、备份后自动重启，会中断当前前台轮次。完成后私聊回报。')
            await schedule(root, ident)
        except asyncio.CancelledError:
            if job is not None:
                job['status'] = 'failed'
                atomic_json(path, job)
            raise
        except Exception as error:
            if job is not None:
                job['status'] = 'failed'
                atomic_json(path, job)
            logging.warning('Frontend control failed (%s)', type(error).__name__)
            # Never include CLI output, paths from config or arbitrary exception text.
            message = str(error) if isinstance(error, ValueError) and str(error).startswith(('未知前台', '已有前台')) else '配置校验或重启调度失败；当前运行的前台未改变。'
            await bridge.send(source, message)


def show(prop):
    return subprocess.check_output(['systemctl', '--user', 'show', 'qq-bridge.service',
                                    '-p', prop, '--value'], text=True, timeout=10).strip()


def restart_and_check(root):
    previous = show('MainPID')
    since = datetime.now().astimezone().isoformat()
    subprocess.run([sys.executable, str(root / 'tools/restart_bridge.py')], cwd=root,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        log = subprocess.run(['journalctl', '--user', '-u', 'qq-bridge.service', '--since', since,
                              '--no-pager', '-o', 'cat'], capture_output=True, text=True, timeout=10)
        if (show('ActiveState') == 'active' and show('MainPID') not in ('0', previous)
                and log.returncode == 0 and 'Connected to NapCat' in log.stdout):
            time.sleep(3)
            if show('ActiveState') == 'active':
                return
        time.sleep(1)
    raise RuntimeError('Bridge connection not confirmed')


async def notify(root, message):
    from websockets.asyncio.client import connect
    cfg = json.loads((root / 'config.json').read_text())
    echo = uuid.uuid4().hex
    async with connect(cfg['ws_url'], additional_headers={
            'Authorization': 'Bearer ' + cfg['onebot_token']}, proxy=None) as ws:
        await ws.send(json.dumps({'action': 'send_private_msg', 'params': {
            'user_id': int(cfg['owner_qq']), 'message': message}, 'echo': echo}))
        async with asyncio.timeout(20):
            async for raw in ws:
                reply = json.loads(raw)
                if reply.get('echo') == echo:
                    return reply.get('status') == 'ok' and reply.get('retcode') == 0
    return False


def run_job(root, ident):
    ident = str(uuid.UUID(ident))
    directory = job_directory(root)
    path = directory / (ident + '.json')
    job = json.loads(path.read_text())
    if job['status'] != 'scheduled' or time.time() - job['created'] > 300:
        return
    before = after = None
    try:
        with config_lock(root):
            job = json.loads(path.read_text())
            if job['status'] != 'scheduled':
                return
            if json.loads((directory / 'pending.json').read_text())['id'] != ident:
                raise RuntimeError('Superseded switch')
            job['status'] = 'running'
            atomic_json(path, job)
            cfg_path = root / 'config.json'
            before = json.loads(cfg_path.read_text())
            after = changed_config(before, job['target'])
            # Permanent job-specific snapshot; the usual .bak is also refreshed.
            atomic_json(directory / (ident + '.backup.json'), before)
            atomic_json(root / 'config.json.bak', before)
            atomic_json(cfg_path, after)
        restart_and_check(root)
        job['status'] = 'completed'
        message = f'主人前台已切换为 {job["target"]}，重启及 QQ 连接检查通过；访客配置保持不变。'
    except Exception:
        job['status'] = 'failed'
        message = '前台切换失败，尚未确认恢复，请检查桥接服务。'
        if before is not None and after is not None:
            with config_lock(root):
                current = json.loads((root / 'config.json').read_text())
                if current == after:
                    atomic_json(root / 'config.json', before)
                    job['status'] = 'restoring'
            if job['status'] == 'restoring':
                try:
                    restart_and_check(root)
                    job['status'] = 'rolled_back'
                    message = '前台切换失败，已恢复原配置并重启，QQ 连接正常。'
                except Exception:
                    job['status'] = 'failed'
    job['finished'] = time.time()
    atomic_json(path, job)
    try:
        job['notification_delivered'] = asyncio.run(notify(root, message))
    except Exception:
        job['notification_delivered'] = False
    atomic_json(path, job)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    run_job(Path(__file__).resolve().parents[1], args.job)

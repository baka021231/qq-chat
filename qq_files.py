"""Save NapCat's downloaded cache files under the Linux QQ downloads directory."""
import os
from pathlib import Path
import re
import stat
import uuid

DOWNLOADS = Path('/home/baka/Downloads/QQ')
MAX_FILE_BYTES = 50 * 1024 * 1024


def save_attachment(source, name, directory=DOWNLOADS):
    source = Path(source)
    if not source.is_absolute():
        raise ValueError('NapCat 未返回有效的本地文件路径')
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '_', name).strip()
    name = name.encode('utf-8')[:180].decode('utf-8', errors='ignore')
    if name in ('', '.', '..'):
        name = 'attachment'
    # Each upload gets its own folder; preserve the name without overwriting files.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = directory / uuid.uuid4().hex[:16]
    folder.mkdir(mode=0o700)
    target = folder / name
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as src:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('附件不是普通文件')
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError('附件超过 50 MiB 上限')
            out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(out_fd, 'wb') as out:
                total = 0
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise ValueError('附件超过 50 MiB 上限')
                    out.write(chunk)
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        folder.rmdir()
        raise

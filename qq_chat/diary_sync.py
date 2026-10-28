"""Copy a fixed set of private diary sources before an owner private search.

No source/destination paths or visibility settings come from model arguments.
The original records and old snapshots are preserved. This is not a writer of
new facts, a topic organizer, or a general-purpose directory mirroring tool.
"""
from contextlib import contextmanager
import fcntl
from itertools import islice
import os
from pathlib import Path
import re
import stat
import uuid

from .memory import MAX_FILE_BYTES, MAX_BYTES, search, validate_query

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
MAX_RECORDS = 128
MAX_ENTRIES = 2048
MONTH = re.compile(r'\d{4}-\d{2}\Z')
DAY = re.compile(r'\d{4}-\d{2}-\d{2}\.md\Z')
FAMILIES = ('our-story', 'development-logs')


@contextmanager
def directory(parent, name, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def entries(fd, budget):
    with os.scandir(fd) as iterator:
        names = [entry.name for entry in islice(iterator, max(0, MAX_ENTRIES - budget[0]) + 1)]
    budget[0] += len(names)
    if budget[0] > MAX_ENTRIES:
        raise ValueError('日记目录超过同步扫描上限')
    return sorted(names)


def paths(fd):
    """Yield only OUR_STORY and family/YYYY-MM/YYYY-MM-DD.md, never recurse freely."""
    budget = [0]
    try:
        os.stat('OUR_STORY.md', dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        yield ('OUR_STORY.md',)
    for family in FAMILIES:
        try:
            family_fd = os.open(family, DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            continue
        try:
            for month in entries(family_fd, budget):
                if not MONTH.fullmatch(month):
                    continue
                with directory(family_fd, month) as month_fd:
                    for day in entries(month_fd, budget):
                        if DAY.fullmatch(day) and day[:7] == month:
                            yield (family, month, day)
        finally:
            os.close(family_fd)


@contextmanager
def parent_for(fd, parts, create=False):
    from contextlib import ExitStack
    with ExitStack() as stack:
        parent = fd
        for component in parts[:-1]:
            parent = stack.enter_context(directory(parent, component, create))
        yield parent


def read_record(fd, parts, *, limit=MAX_FILE_BYTES):
    with parent_for(fd, parts) as parent:
        opened = os.open(parts[-1], FILE_FLAGS, dir_fd=parent)
        with os.fdopen(opened, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('日记必须是无链接的普通文件')
            if info.st_size > limit:
                raise ValueError('日记超过单文件同步上限')
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) > limit or info.st_size != after.st_size
                    or info.st_mtime_ns != after.st_mtime_ns):
                raise ValueError('日记在读取中发生变化，请稍后重试')
    raw.decode('utf-8')
    if b'\0' in raw:
        raise ValueError('日记不是有效文本')
    return raw


def replace_copy(fd, parts, raw):
    with parent_for(fd, parts, create=True) as parent:
        name = '.sync-' + uuid.uuid4().hex
        opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
        try:
            with os.fdopen(opened, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def synchronize(workspace):
    """Refresh private/diary-current under the same lock as the local organizer.

    On failure the caller must not search stale copies as if sync succeeded.
    Individual copies are atomic, not a cross-file transaction; retry converges.
    Only managed copies of removed source records are deleted.
    """
    root = os.open(workspace, DIR_FLAGS)
    try:
        with directory(root, '.automation', create=True) as area:
            opened = os.open('nightly.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=area)
            with os.fdopen(opened, 'r+b') as lock:
                info = os.fstat(lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('日记同步锁不可用')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('日记正在整理，请稍后再查') from None
                records = {}
                size = 0
                for parts in paths(root):
                    if len(records) >= MAX_RECORDS:
                        raise ValueError('日记数量超过本阶段同步上限')
                    raw = read_record(root, parts)
                    size += len(raw)
                    if size > MAX_BYTES:
                        raise ValueError('日记总大小超过同步上限')
                    records[parts] = raw
                # All source reads finish before changing any managed copy.
                with directory(root, 'memory', create=True) as memory:
                    with directory(memory, 'private', create=True) as private:
                        with directory(private, 'diary-current', create=True) as current:
                            old = set(paths(current))
                            changed = 0
                            for parts, raw in records.items():
                                try:
                                    unchanged = read_record(current, parts) == raw
                                except FileNotFoundError:
                                    unchanged = False
                                if not unchanged:
                                    replace_copy(current, parts, raw)
                                    changed += 1
                            removed = 0
                            for parts in old - records.keys():
                                with parent_for(current, parts) as parent:
                                    os.unlink(parts[-1], dir_fd=parent)
                                    os.fsync(parent)
                                removed += 1
                return {'status': 'ok', 'files': len(records), 'updated': changed, 'removed': removed}
    except (OSError, UnicodeError) as error:
        raise ValueError('日记同步失败，本次未使用旧副本回答') from error
    finally:
        os.close(root)


def search_with_diaries(workspace, query, *, group_id=None):
    validate_query(query)
    workspace = Path(workspace)
    # Group calls never inspect or copy private diary material, even with no hits.
    if group_id is not None:
        return search(workspace / 'memory', query, group_id=group_id)
    synced = synchronize(workspace)
    result = search(workspace / 'memory', query, exclude_private_snapshots=True)
    result['diary_sync'] = synced
    return result

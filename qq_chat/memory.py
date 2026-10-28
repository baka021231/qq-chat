"""Bounded literal memory search. Scope is supplied by the authenticated bridge."""
import os
from itertools import islice
import stat
import unicodedata

MAX_FILES = 256
MAX_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_ENTRIES = 2048
MAX_DEPTH = 8
MAX_RESULTS = 5
MAX_TEXT = 2000


def normalized(text):
    return unicodedata.normalize('NFKC', text).casefold()


def search(root, query, *, group_id=None):
    """Return excerpts, never executable instructions or caller-selected paths.

    None means an already authenticated owner private chat; a group ID permits
    shared/ and groups/<id>/ only. Root-level files are intentionally invisible.
    No symlinks (including the root), hardlinks, devices or pipes are read.
    """
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError('检索词应为 1～200 字')
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise ValueError('无效的群记忆范围')
    terms = list(dict.fromkeys(normalized(query).split()))
    if len(terms) > 8:
        raise ValueError('最多使用 8 个检索词，以空格分隔')
    scopes = [('shared',), ('private',)] if group_id is None else [('shared',), ('groups', str(group_id))]
    budget = {'entries': 0, 'files': 0, 'bytes': 0, 'partial': False}
    hits = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(fd, prefix, depth=0):
        # listdir does not follow links; all subsequent opens are relative to
        # retained directory descriptors, so directory replacement cannot escape.
        with os.scandir(fd) as entries:
            names = [entry.name for entry in islice(entries, MAX_ENTRIES - min(budget['entries'], MAX_ENTRIES) + 1)]
        for name in sorted(names):
            budget['entries'] += 1
            if budget['entries'] > MAX_ENTRIES or budget['files'] >= MAX_FILES or budget['bytes'] >= MAX_BYTES:
                budget['partial'] = True
                return
            if name.startswith('.'):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_DEPTH:
                        budget['partial'] = True
                        continue
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        walk(child, prefix + '/' + name, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and name.endswith(('.md', '.txt')):
                    budget['files'] += 1
                    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
                    with os.fdopen(file_fd, 'rb') as stream:
                        actual = os.fstat(stream.fileno())
                        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                            continue
                        if actual.st_size > MAX_FILE_BYTES or actual.st_size > MAX_BYTES - budget['bytes']:
                            budget['partial'] = True
                            continue
                        raw = stream.read(min(MAX_FILE_BYTES, MAX_BYTES - budget['bytes']) + 1)
                    budget['bytes'] += len(raw)
                    if len(raw) > MAX_FILE_BYTES or budget['bytes'] > MAX_BYTES:
                        budget['partial'] = True
                        continue
                    text = raw.decode('utf-8')
                    if '\x00' in text:
                        budget['partial'] = True
                        continue
                    # One excerpt per file, centred on its best matching line;
                    # never return entire long documents.
                    lines = text.splitlines()
                    candidates = []
                    for number, line in enumerate(lines):
                        value = normalized(line)
                        score = sum(term in value for term in terms)
                        if score:
                            candidates.append((score, number))
                    if candidates:
                        score, number = max(candidates, key=lambda item: (item[0], -item[1]))
                        line = lines[number]
                        # Keep the matching portion even on a very long line.
                        folded = normalized(line)
                        position = min(folded.find(t) for t in terms if t in folded)
                        start = max(0, position - 100)
                        # NFKC may change offsets; for such lines return the
                        # normalized excerpt explicitly instead of wrong offsets.
                        shown = folded if len(folded) != len(line) else line
                        excerpt = shown[start:start + 500]
                        hits.append({'file': prefix + '/' + name, 'line': number + 1,
                                     'text': excerpt, '_score': score})
                        hits.sort(key=lambda h: (-h['_score'], h['file'], h['line']))
                        del hits[MAX_RESULTS:]
            except (OSError, UnicodeError):
                budget['partial'] = True

    try:
        root_fd = os.open(root, flags)
    except FileNotFoundError:
        return {'results': [], 'status': 'not_configured', 'partial': False}
    except OSError:
        raise ValueError('记忆目录不可用，禁止符号链接') from None
    try:
        for scope in scopes:
            fd = os.dup(root_fd)
            try:
                for component in scope:
                    child = os.open(component, flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                walk(fd, '/'.join(scope))
            except FileNotFoundError:
                pass
            except OSError:
                budget['partial'] = True
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)
    remaining = MAX_TEXT
    for hit in hits:
        hit.pop('_score')
        hit['text'] = hit['text'][:remaining]
        remaining -= len(hit['text'])
    hits = [hit for hit in hits if hit['text']]
    return {'results': hits, 'status': 'ok' if hits else 'no_results',
            'partial': budget['partial'], 'reference_only': True}

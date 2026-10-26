"""Structure-aware streaming QQ bubbles. Queue items are raw text deltas."""
import asyncio
import re

LIST_START = re.compile(r'^\s*(?:\d+\.(?!\d)|\d+、|[①-⑳・•-])')
NUMBER_ONLY = re.compile(r'^\s*(?:\d+[.、]?|[①-⑳・•-])\s*$')
FENCE = re.compile(r'^\s*(`{3,}|~{3,})(.*)$')


def sentence_ends(text):
    """Only Chinese stops; protect inline code and trailing quote/format marks."""
    ends, inline, i = [], 0, 0
    while i < len(text):
        if text[i] == '`':
            end = i + 1
            while end < len(text) and text[end] == '`':
                end += 1
            size = end - i
            inline = 0 if inline == size else size if not inline else inline
            i = end
            continue
        if not inline and text[i] in '。！？':
            end = i + 1
            while end < len(text) and text[end] in '。！？”’」』）】*~':
                end += 1
            ends.append(end)
            i = end
        else:
            i += 1
    return ends


def forward_line(text):
    """Short-heading heuristic, plus explicit lead-ins and numbering."""
    plain = text.strip().strip('*').strip()
    if NUMBER_ONLY.fullmatch(plain) or plain.endswith(('：', ':')):
        return True
    if re.match(r'^#{1,6}\s+\S', plain):
        return True
    return (not LIST_START.match(plain) and 0 < len(plain) <= 20
            and not re.search(r'[。！？!?.,，;；/\\`：:]', plain))


class BubbleStream:
    def __init__(self, target=80):
        self.target = target
        self.line = ''
        self.unit = ''
        self.prefix = ''
        self.continuing = False
        self.fence = ''
        self.code = []
        self.out = []

    def _flush(self):
        if self.unit.strip():
            self.out.append(self.unit.strip())
        self.unit = ''

    def _append(self, text, separator='\n'):
        if self.prefix:
            text, self.prefix = self.prefix + '\n' + text, ''
        if self.unit and separator:
            self.unit += separator
        self.unit += text

    def _content(self, text, separator='\n'):
        # Keep the last fragment until the next line, idle timeout, or completion.
        ends = sentence_ends(text)
        start = 0
        for end in ends[:-1]:
            self._append(text[start:end], separator if start == 0 else '')
            if len(self.unit) >= self.target:
                self._flush()
            start = end
        self._append(text[start:], separator if start == 0 else '')

    def _line(self, text):
        marker = FENCE.match(text)
        if self.fence:
            self.code.append(text)
            if marker and marker[1][0] == self.fence[0] and len(marker[1]) >= len(self.fence) and not marker[2].strip():
                self._append('\n'.join(self.code))
                self._flush()
                self.fence, self.code = '', []
            return
        if marker and not self.continuing:
            self._flush()
            self.fence, self.code = marker[1], [text]
            return
        if not text.strip():
            self._flush()
            self.continuing = False
            return
        if not self.continuing and forward_line(text):
            self._flush()
            self.prefix += ('\n' if self.prefix else '') + text.strip()
            return
        if (not self.continuing and LIST_START.match(text)) or len(self.unit) >= self.target:
            self._flush()
        self._content(text, '' if self.continuing else '\n')
        self.continuing = False

    def _take(self):
        result, self.out = self.out, []
        return result

    def feed(self, delta):
        self.line += delta
        while '\n' in self.line:
            line, self.line = self.line.split('\n', 1)
            self._line(line.rstrip('\r'))
        # Stream long lines too, while reserving the final sentence/fragment.
        if not self.fence and not FENCE.match(self.line):
            ends = sentence_ends(self.line)
            safe = ends[-2] if len(ends) >= 2 else 0
            if safe and len(self.line[:safe]) + len(self.unit) >= self.target:
                prefix, self.line = self.line[:safe], self.line[safe:]
                self._line(prefix)
                self._flush()
                self.continuing = True
        return self._take()

    def idle(self):
        if not self.fence and not FENCE.match(self.line):
            # Do not cut an incomplete inline code span on an idle timeout.
            inline = 0
            for run in re.findall(r'`+', self.line):
                inline = 0 if len(run) == inline else len(run) if not inline else inline
            if not inline and self.line:
                line, self.line = self.line, ''
                self._line(line)
                self.continuing = True
            self._flush()
        return self._take()

    def finish(self):
        if self.line:
            line, self.line = self.line, ''
            self._line(line)
        if self.code:
            self._append('\n'.join(self.code))
            self.code = []
        if self.prefix:
            # No following content exists: preserve the tail without gluing backward.
            self._flush()
            self.unit, self.prefix = self.prefix, ''
        self._flush()
        return self._take()


async def stream_bubbles(queue, target=80, idle=1.5):
    parser = BubbleStream(target)
    while True:
        try:
            delta = await asyncio.wait_for(queue.get(), idle)
        except asyncio.TimeoutError:
            for bubble in parser.idle():
                yield bubble
            continue
        if delta is None:
            for bubble in parser.finish():
                yield bubble
            return
        for bubble in parser.feed(delta):
            yield bubble

"""Conservative group abuse checks and persistent sender-only blocking."""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

BLOCK_SENDER = '[[BLOCK_CURRENT_GROUP_SENDER]]'
GUEST_REPLY_LIMIT = 500


def blocked_group_sender(source, cfg):
    return (source.kind == 'group'
            and str(source.sender_id) != str(cfg['owner_qq'])
            and str(source.sender_id) in {str(user) for user in cfg.get('group_user_blacklist', [])})


def obvious_abuse(text):
    """Catch explicit output floods/imperative injection, not difficult questions."""
    if len(text) > 6000:
        return False  # The worker rejects oversized inputs without a model call.
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf').casefold().strip()
    if any(quote in text for quote in ('"', "'", '“', '”', '「', '」', '『', '』')):
        return False  # Quoted examples require the model's contextual judgment.
    # Discussing a technique or asking for an algorithm is not abuse.
    if re.search(r'如何防|怎么防|防止|检测|不要输出|别输出|不必输出|无需输出|不用输出|科学计数法|近似|算法|原理', text):
        return False
    direct = re.sub(r'^(?:(?:aemeath|爱弥斯|ams|小爱)[\s,，:：]*)', '', text)
    if re.match(r'^(?:请|你|现在|立刻|马上|必须|先|给我|都|把|\s|,|，)*(?:忽略|无视|忘掉|忘记|抛弃|绕过)(?:你|所有|全部|之前|以前|先前|上述|上面|系统|开发者|的|\s)*(?:指令|规则|限制|设定|提示词)', direct):
        return True
    if not re.search(r'输出|打印|列出|枚举|重复|复读|生成|写出|写下|计算|算出', text):
        return False
    if not re.search(r'数字|整数|素数|质数|小数|圆周率|π|字符|重复|复读|阶乘|斐波那契|从.{0,20}到|(?:个|行|次|遍)\s*\d', text):
        return False
    if re.search(r'\d{512,}', text):
        return True
    quantities = re.findall(r'([一二两三四五六七八九十百千万亿\d]{1,32})\s*(?:个|次|遍|行|位|项|条)', text)
    if any(any(unit in n for unit in '千万亿') for n in quantities):
        return True
    if any(len(n) > 8 or int(n) >= 500 for n in quantities if n.isdecimal()):
        return True
    if not re.search(r'随机|任选|任取|挑一个|选一个|抽一个|取一个', text):
        for start, end in re.findall(r'(\d{1,16})\s*(?:到|至|~|～|-)\s*(\d{1,16})', text):
            if len(start) > 8 or len(end) > 8 or abs(int(end) - int(start)) >= 500:
                return True
    return False


def update_blacklist(path, user, owner, *, blocked=True):
    """Merge just this setting under a lock; never take a target from model text."""
    user = str(user)
    if not user.isascii() or not user.isdigit() or int(user) <= 0:
        raise ValueError('QQ 号必须是正整数')
    user = str(int(user))
    path = Path(path)
    lock_fd = os.open(path.parent / '.group-blacklist.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = json.loads(path.read_text())
        if blocked and user in (str(owner), str(cfg['owner_qq'])):
            raise ValueError('不能拉黑主人')
        users = {str(int(item)) for item in cfg.get('group_user_blacklist', [])}
        if blocked:
            users.add(user)
        else:
            users.discard(user)
        result = sorted(users, key=int)
        cfg['group_user_blacklist'] = result
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
    return result


def bounded_guest_reply(reply):
    """Keep a misbehaving model from sending long numeric payloads to QQ."""
    if sum(c.isdigit() for c in reply) > 200:
        return '刷屏的长串数字就免了。'
    if len(reply) > GUEST_REPLY_LIMIT:
        return reply[:GUEST_REPLY_LIMIT] + '…'
    return reply

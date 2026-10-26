"""Deterministic Chinese speech text cleanup, with no model dependencies."""
import re

DIGITS = '零一二三四五六七八九'


def digit_words(text):
    return ''.join(DIGITS[int(c)] for c in text)


def four_digits(value, omit_one=False):
    text = str(value)
    result, zero = '', False
    for index, char in enumerate(text):
        place = len(text) - index - 1
        if char == '0':
            zero = bool(result)
            continue
        if zero:
            result += '零'
            zero = False
        if not (omit_one and not result and char == '1' and place == 1):
            result += DIGITS[int(char)]
        result += ('', '十', '百', '千')[place]
    return result


def integer_words(text):
    # Preserve identifiers/phone numbers and leading zeros digit by digit.
    if (len(text) > 16 or (len(text) > 1 and text.startswith('0'))
            or re.fullmatch(r'1[3-9][0-9]{9}', text)):
        return digit_words(text)
    value = int(text)
    if value == 0:
        return '零'
    groups = []
    while value:
        value, group = divmod(value, 10000)
        groups.append(group)
    result, zero = '', False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            zero = bool(result)
            continue
        if result and (zero or group < 1000):
            result += '零'
        result += four_digits(group, omit_one=not result) + ('', '万', '亿', '兆')[index]
        zero = False
    return result


def number_words(text):
    percent = text.endswith('%')
    text = text.rstrip('%')
    sign = ''
    if text.startswith(('-', '+')):
        sign = '负' if text[0] == '-' else '正'
        text = text[1:]
    integer, dot, decimal = text.partition('.')
    result = sign + integer_words(integer)
    if dot:
        result += '点' + digit_words(decimal)
    return ('百分之' if percent else '') + result


def normalize_for_speech(text):
    # Speech pauses, not a sentence boundary for every trailing wave mark.
    text = re.sub(r'[~～]+', '！', text)
    text = re.sub(r'…+|⋯+|\.{3,}', '，', text)
    # Common ASCII faces, including Ow<; do not remove ordinary English words.
    text = re.sub(r'(?<![A-Za-z])(?:[oO0QqTt][wWvV_ω][oO0QqTt<>]|[QqTt][Aa_][QqTt]|[><][_.﹏][><]|\^[_ω.]\^)(?![A-Za-z])', '', text)
    text = re.sub(r'[（(][^\w\u3400-\u9fff\n]{0,16}[╥﹏ಠಥ][^\u3400-\u9fff\n]{0,16}[）)]', '', text)
    text = re.sub('[0-9#*]\uFE0F?\u20E3', '', text)
    text = re.sub('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\uFE0E\uFE0F\u200D\u20E3\u00A9\u00AE\u2122]', '', text)
    text = re.sub(r'[（(]\s*[）)]', '', text)
    text = re.sub(r'(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])', lambda m: m[0].replace(',', ''), text)
    text = re.sub(r'(?<![0-9])([12][0-9]{3})年', lambda m: digit_words(m[1]) + '年', text)
    text = re.sub(r'(?<=[0-9])[-–—](?=[0-9])', '到', text)
    text = re.sub(r'[-+]?[0-9]+(?:\.[0-9]+)?%?', lambda m: number_words(m[0]), text)
    text = re.sub(r'([，。！？])\1+', r'\1', text)
    text = re.sub(r'([。！？])，+', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip()

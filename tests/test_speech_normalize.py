import unittest
from qq_chat.speech_normalize import integer_words, normalize_for_speech
from qq_chat.voice import speech_text


class NormalizeTests(unittest.TestCase):
    def test_waves_faces_emoji_and_ellipsis(self):
        self.assertEqual(normalize_for_speech('你好~~……Ow< 😊👩‍💻🇨🇳'), '你好！')
        self.assertEqual(normalize_for_speech('完成3️⃣项'), '完成项')
        self.assertEqual(normalize_for_speech('先等……然后继续。'), '先等，然后继续。')
        self.assertEqual(normalize_for_speech('hello world，CPU 正常。'), 'hello world，CPU 正常。')
        self.assertEqual(normalize_for_speech('准备好了吗？Ow< 😊'), '准备好了吗？')

    def test_integer_boundaries(self):
        cases = {'0': '零', '10': '十', '11': '十一', '20': '二十', '101': '一百零一',
                 '1001': '一千零一', '10000': '一万', '10001': '一万零一',
                 '10010': '一万零一十', '100000001': '一亿零一',
                 '100010000': '一亿零一万', '001': '零零一',
                 '13800138000': '一三八零零一三八零零零'}
        for value, expected in cases.items():
            with self.subTest(value=value): self.assertEqual(integer_words(value), expected)

    def test_decimals_percent_signs_year_and_range(self):
        self.assertEqual(normalize_for_speech('3项，-12.5度，99.5%，2026年，1-3次，1,234元'),
                         '三项，负十二点五度，百分之九十九点五，二零二六年，一到三次，一千二百三十四元')

    def test_cleanup_runs_after_markdown_and_keeps_numbers_outside_code(self):
        result = speech_text('## 第3步\n代码：```python\nx=999\n```\n[文档](https://example.com/123) 好啦~Ow<')
        self.assertIn('第三步', result)
        self.assertIn('代码见文字消息', result)
        self.assertIn('链接见文字消息', result)
        self.assertIn('好啦！', result)
        self.assertNotIn('九百', result)
        self.assertNotIn('Ow<', result)
        self.assertNotIn('http', result)

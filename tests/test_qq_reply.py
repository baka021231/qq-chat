import asyncio
import unittest

from qq_chat.reply import BubbleStream, sentence_ends, stream_bubbles


def bubbles(text, width=1, target=80):
    parser, result = BubbleStream(target), []
    for i in range(0, len(text), width):
        result.extend(parser.feed(text[i:i + width]))
    return result + parser.finish()


class BubbleTests(unittest.TestCase):
    def test_only_chinese_sentence_stops(self):
        self.assertEqual(sentence_ends('1. v1.2 qq_bridge.py! hello?'), [])
        self.assertEqual(sentence_ends('一。二！三？'), [2, 4, 6])
        self.assertEqual(sentence_ends('`a。b`后面。'), [len('`a。b`后面。')])

    def test_structure_for_every_chunk_width(self):
        cases = [
            ('第一句。第二句。', ['第一句。第二句。']),
            ('前段。\n\n后段。', ['前段。', '后段。']),
            ('说明。\n1. 第一点。\n2. 第二点。', ['说明。', '1. 第一点。', '2. 第二点。']),
            ('1、第一点。\n2、第二点。', ['1、第一点。', '2、第二点。']),
            ('①第一点。\n②第二点。', ['①第一点。', '②第二点。']),
            ('・第一点。\n・第二点。', ['・第一点。', '・第二点。']),
            ('- 第一点。\n- 第二点。', ['- 第一点。', '- 第二点。']),
            ('• 第一点。\n• 第二点。', ['• 第一点。', '• 第二点。']),
            ('前文。\n1.\n具体内容。', ['前文。', '1.\n具体内容。']),
            ('前文。\n小标题\n具体内容。', ['前文。', '小标题\n具体内容。']),
            ('前文。\n下面这样做：\n第一步。', ['前文。', '下面这样做：\n第一步。']),
            ('前文。\nNext:\n第一步。', ['前文。', 'Next:\n第一步。']),
            ('小标题\n\n1. 第一项。\n续写这一项。\n2. 第二项。', ['小标题\n1. 第一项。\n续写这一项。', '2. 第二项。']),
            ('版本 v1.2 在 qq_bridge.py。\n1. 第一步。', ['版本 v1.2 在 qq_bridge.py。', '1. 第一步。']),
            ('代码：\n```python\nx="你好。"\n\ny=2\n```\n后文。', ['代码：\n```python\nx="你好。"\n\ny=2\n```', '后文。']),
        ]
        for text, expected in cases:
            for width in (1, 2, 3, 7, 1000):
                with self.subTest(text=text, width=width):
                    self.assertEqual(bubbles(text, width), expected)

    def test_threshold_streams_before_end_without_losing_text(self):
        text = ('这是一句比较长的正文。' * 40)
        for width in (1, 17, 1000):
            parser, result = BubbleStream(), []
            for i in range(0, len(text), width):
                result.extend(parser.feed(text[i:i + width]))
            self.assertTrue(result)
            result.extend(parser.finish())
            self.assertEqual(''.join(result), text)

    def test_idle_does_not_send_heading_or_incomplete_code(self):
        parser = BubbleStream()
        parser.feed('引导句：')
        self.assertEqual(parser.idle(), [])
        parser.feed('\n正文。')
        self.assertEqual(parser.idle(), ['引导句：\n正文。'])
        parser = BubbleStream()
        parser.feed('```python\nx="你好。"')
        self.assertEqual(parser.idle(), [])
        parser.feed('\n```\n')
        self.assertEqual(parser.finish(), [])

    def test_last_fragment_waits_for_next_line(self):
        parser = BubbleStream()
        self.assertEqual(parser.feed('上一句。'), [])
        self.assertEqual(parser.feed('\n'), [])
        self.assertEqual(parser.feed('1. 新列点。\n'), ['上一句。'])
        self.assertEqual(parser.finish(), ['1. 新列点。'])

    def test_isolated_prefix_at_completion_is_preserved_separately(self):
        for tail in ('1.', '标题', '说明：'):
            self.assertEqual(bubbles('前句。\n' + tail), ['前句。', tail])

    def test_forward_prefix_does_not_go_back_on_timeout(self):
        parser = BubbleStream()
        self.assertEqual(parser.feed('前句。\n1.\n'), ['前句。'])
        self.assertEqual(parser.idle(), [])
        parser.feed('具体说明。')
        self.assertEqual(parser.idle(), ['1.\n具体说明。'])


class TimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_timer_and_tail_flush(self):
        queue = asyncio.Queue()
        stream = stream_bubbles(queue, idle=.04)
        queue.put_nowait('一句。')
        self.assertEqual(await asyncio.wait_for(anext(stream), .3), '一句。')
        queue.put_nowait('第二句。')
        queue.put_nowait(None)
        self.assertEqual([part async for part in stream], ['第二句。'])

    async def test_heading_waits_past_timeout_for_next_content(self):
        queue = asyncio.Queue()
        stream = stream_bubbles(queue, idle=.02)
        queue.put_nowait('小标题\n')
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(.06)
        self.assertFalse(task.done())
        queue.put_nowait('正文。')
        queue.put_nowait(None)
        self.assertEqual(await asyncio.wait_for(task, .3), '小标题\n正文。')
        self.assertEqual([part async for part in stream], [])

    async def test_new_deltas_reset_idle_timer(self):
        queue = asyncio.Queue()
        stream = stream_bubbles(queue, idle=.06)
        task = asyncio.create_task(anext(stream))
        for part in ('第一', '句。', '第二句。'):
            queue.put_nowait(part)
            await asyncio.sleep(.025)
            self.assertFalse(task.done())
        self.assertEqual(await asyncio.wait_for(task, .3), '第一句。第二句。')
        queue.put_nowait(None)
        self.assertEqual([part async for part in stream], [])


if __name__ == '__main__':
    unittest.main()

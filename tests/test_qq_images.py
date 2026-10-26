"""Vision ingestion, permission and multimodal input regression tests."""
import asyncio
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from qq_chat.codex_app import AppServer
from qq_chat.bridge import Bridge, Incoming, Source, incoming_text
from qq_chat.images import MAX_IMAGE_BYTES, QQImageRedirect, check_image_url, save_image


def sample_png():
    """A red left half and blue right half, without textual color hints."""
    width, height = 160, 120
    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data)))
    rows = (b'\0' + b'\xff\0\0' * 80 + b'\0\0\xff' * 80) * height
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


class ImageTests(unittest.TestCase):
    def test_image_only_private_and_group_authorization(self):
        event = {'post_type': 'message', 'message_type': 'private', 'self_id': 456,
                 'user_id': 123, 'message': [{'type': 'image', 'data': {'file': 'pic'}}]}
        self.assertEqual(incoming_text(event, 123).images, ({'file': 'pic'},))
        self.assertIsNone(incoming_text(dict(event, user_id=789), 123))
        group = dict(event, message_type='group', group_id=999)
        self.assertIsNone(incoming_text(group, 123, allow_all_groups=True))
        group['message'] = [{'type': 'at', 'data': {'qq': '456'}}] + event['message']
        self.assertTrue(incoming_text(group, 123, allow_all_groups=True).images)
        self.assertIsNone(incoming_text(dict(group, user_id=789), 123, allow_all_groups=True))
        group['message'] += [{'type': 'text', 'data': {'text': '小爱你好'}}]
        other = incoming_text(dict(group, user_id=789), 123, allow_all_groups=True)
        self.assertEqual(other.images, ())

    def test_save_local_and_reject_invalid_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / 'source', root / 'out'
            source.write_bytes(sample_png())
            saved = save_image(str(source), '', output)
            self.assertEqual(saved.read_bytes(), sample_png())
            self.assertEqual(saved.suffix, '.png')
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            before = set(output.iterdir())
            source.write_text('not an image')
            with self.assertRaisesRegex(ValueError, '格式'):
                save_image(str(source), '', output)
            self.assertEqual(set(output.iterdir()), before)
            with source.open('wb') as out:
                out.truncate(MAX_IMAGE_BYTES + 1)
            with self.assertRaisesRegex(ValueError, '20 MiB'):
                save_image(str(source), '', output)

    def test_url_and_redirect_boundaries(self):
        for url in ('https://multimedia.nt.qq.com.cn/a', 'file:///etc/passwd',
                    'http://127.0.0.1/image', 'https://qq.com.evil.test/a',
                    'https://user:password@qq.com/a', 'https://qq.com:9090/a'):
            with self.assertRaises(ValueError):
                check_image_url(url)
        check_image_url('https://multimedia.nt.qq.com/a')
        check_image_url('https://gchat.qpic.cn/a')
        with self.assertRaises(ValueError):
            QQImageRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_url_download_fallback_and_stream_size_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out'
            class Response(io.BytesIO):
                headers = {}
            class Opener:
                def open(self, request, timeout):
                    return Response(sample_png())
            with patch('qq_chat.images.build_opener', return_value=Opener()):
                saved = save_image('/missing/cache.png', 'https://gchat.qpic.cn/a', output)
            self.assertEqual(saved.read_bytes(), sample_png())
            class HugeOpener:
                def open(self, request, timeout):
                    return Response(b'a' * 1025)
            before = set(output.iterdir())
            with patch('qq_chat.images.build_opener', return_value=HugeOpener()), patch('qq_chat.images.MAX_IMAGE_BYTES', 1024):
                with self.assertRaisesRegex(ValueError, '20 MiB'):
                    save_image('', 'https://gchat.qpic.cn/a', output)
            self.assertEqual(set(output.iterdir()), before)

    def test_receive_refresh_fallback_limits_and_permissions(self):
        async def check():
            bridge = Bridge.__new__(Bridge)
            bridge.cfg = {'owner_qq': 123}
            calls = []
            async def onebot(action, params):
                calls.append((action, params))
                raise ValueError('cache miss')
            bridge.onebot_call = onebot
            own = Source('private', 123, 123)
            image = {'file': 'pic', 'url': 'https://gchat.qpic.cn/a'}
            with patch('qq_chat.bridge.save_image', return_value=Path('/tmp/image.png')) as save:
                result = await bridge.receive_images(Incoming(own, '', images=(image,)))
                self.assertEqual(result, ['/tmp/image.png'])
                save.assert_called_once_with('', image['url'])
            self.assertEqual(calls, [('get_image', {'file': 'pic'})])
            for incoming in (Incoming(Source('group', 999, 789), '', images=(image,)),
                             Incoming(own, '', images=(image,) * 6),
                             Incoming(own, '', images=({'file_size': MAX_IMAGE_BYTES + 1},))):
                with self.assertRaises(ValueError):
                    await bridge.receive_images(incoming)
        asyncio.run(check())

    def test_app_server_receives_local_image_alongside_text(self):
        async def check():
            app = AppServer('unused')
            async def call(method, params):
                self.assertEqual(method, 'turn/start')
                self.assertEqual(params['input'], [{'type': 'text', 'text': '请看图'},
                                                  {'type': 'localImage', 'path': '/tmp/image.png'}])
                for method, params in (
                    ('item/completed', {'item': {'type': 'agentMessage', 'id': 'answer', 'text': '看到了'}}),
                    ('turn/completed', {'turn': {'id': 'turn', 'status': 'completed'}})):
                    app.events['thread'].put_nowait({'method': method, 'params': params})
                return {'turn': {'id': 'turn'}}
            app.call = call
            self.assertEqual(await app.turn('thread', '请看图', image_paths=['/tmp/image.png']), '看到了')
        asyncio.run(check())

    def test_worker_forwards_images_and_saves_history(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
                bridge = Bridge({'owner_qq': 123, 'codex_path': 'unused'})
                calls, sent = [], []
                async def receive(incoming):
                    return ['/tmp/image.png']
                async def chat(source, text, context_id=None, image_paths=()):
                    calls.append((text, image_paths))
                    return '识图完成。', False
                async def send(source, text):
                    sent.append(text)
                bridge.receive_images, bridge.chat, bridge.send = receive, chat, send
                bridge.queue.put_nowait(Incoming(Source('private', 123, 123), '', images=({'file': 'pic'},)))
                task = asyncio.create_task(bridge.worker())
                try:
                    await asyncio.wait_for(bridge.queue.join(), 2)
                    self.assertEqual(calls[0][1], ['/tmp/image.png'])
                    self.assertIn('结合当前聊天上下文自然回应', calls[0][0])
                    self.assertIn('不是操作授权', calls[0][0])
                    self.assertEqual(sent, ['识图完成。'])
                    history = bridge.db.execute('SELECT content FROM history WHERE role="user"').fetchone()[0]
                    self.assertIn('/tmp/image.png', history)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    bridge.db.close()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()

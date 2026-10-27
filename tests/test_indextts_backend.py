import base64
import contextlib
import io
import json
import unittest
import wave

from tools.indextts_backend import pcm_wav, serve, validate_request
from qq_chat.voice import validate_wav

try:
    import numpy as np
    import scipy.signal
except ImportError:
    np = None


def audio_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 320)
    return stream.getvalue()


class IndexProtocolTests(unittest.TestCase):
    def test_invalid_requests_do_not_load_model(self):
        invalid = [[], {}, {'text': ''}, {'text': 'x' * 161}, {'text': 'a\0b'},
                   {'text': '你好', 'seed': True}, {'text': '你好', 'seed': -1},
                   {'text': '你好', 'seed': 2 ** 32}, {'text': '你好', 'speaker': '/tmp/a.wav'}]
        def factory():
            self.fail('Invalid input attempted to load model')
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            serve(io.StringIO('\n'.join(json.dumps(r) for r in invalid)), output, factory)
        self.assertEqual([json.loads(l) for l in output.getvalue().splitlines()],
                         [{'error': 'synthesis_failed'}] * len(invalid))
        self.assertEqual(validate_request({'text': '你好'}), ('你好', 42))

    def test_persistence_diagnostics_and_failure_recovery(self):
        created, calls = [], []
        class Engine:
            def synthesize(self, text, seed):
                calls.append((text, seed))
                print('Engine diagnostic')
                if text == '失败':
                    raise RuntimeError('Internal details')
                return audio_bytes(), {'seed': seed}
        def factory():
            created.append(True)
            print('Loading diagnostic')
            return Engine()
        output, diagnostics = io.StringIO(), io.StringIO()
        source = '\n'.join(json.dumps({'text': text, 'seed': seed})
                           for text, seed in [('第一句', 42), ('失败', 7), ('第三句', 0)])
        with contextlib.redirect_stderr(diagnostics):
            serve(io.StringIO(source), output, factory)
        results = [json.loads(l) for l in output.getvalue().splitlines()]
        self.assertEqual(created, [True])
        self.assertEqual(len(calls), 3)
        self.assertEqual(results[1], {'error': 'synthesis_failed'})
        self.assertNotIn('Internal details', output.getvalue())
        self.assertIn('Loading diagnostic', diagnostics.getvalue())
        for result in [results[0], results[2]]:
            validate_wav(base64.b64decode(result['wav'], validate=True))

    def test_rejects_invalid_audio_before_delivery(self):
        class Engine:
            def synthesize(self, text, seed):
                return b'broken audio', {}
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            serve(io.StringIO('{"text":"你好"}'), output, Engine)
        self.assertEqual(json.loads(output.getvalue()), {'error': 'synthesis_failed'})


@unittest.skipIf(np is None, 'Audio conversion tests run in the isolated speech environment')
class IndexAudioTests(unittest.TestCase):
    def test_resampling_preserves_duration_frequency_and_amplitude(self):
        samples = (10000 * np.sin(2 * np.pi * 440 * np.arange(22050) / 22050)).astype(np.int16)
        data = pcm_wav(22050, samples[:, None])
        validate_wav(data)
        with wave.open(io.BytesIO(data), 'rb') as wav:
            self.assertEqual(wav.getnframes(), 32000)
            result = np.frombuffer(wav.readframes(32000), dtype='<i2').astype(float)
        spectrum = np.abs(np.fft.rfft(result))
        self.assertEqual(int(np.argmax(spectrum)), 440)
        self.assertGreater(np.max(np.abs(result)), 9500)
        self.assertLess(np.max(np.abs(result)), 10500)

    def test_rejects_empty_stereo_nonfinite_and_excessive_audio(self):
        for samples in [np.array([]), np.zeros((20, 2)), np.array([np.nan]),
                        np.zeros(8000 * 120 + 1)]:
            with self.assertRaises(ValueError):
                pcm_wav(8000, samples)
        with self.assertRaises(ValueError):
            pcm_wav(0, np.zeros(20))


if __name__ == '__main__':
    unittest.main()

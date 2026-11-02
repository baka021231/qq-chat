import base64
import contextlib
import io
import json
from pathlib import Path
import unittest
import wave

from tools.indextts_backend import (PRESETS, SceneLibrary, cap_pauses, emotion_vector, pcm_wav, serve,
                                    validate_request)
import json as _json
import tempfile
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
                   {'text': '你好', 'seed': 2 ** 32}, {'text': '你好', 'speaker': '/tmp/a.wav'},
                   {'text': '你好', 'style': '愤怒'}, {'text': '你好', 'intensity': '极高'},
                   {'text': '你好', 'style': '/tmp/a.wav'}, {'text': '你好', 'scene': ''},
                   {'text': '你好', 'scene': 'x' * 61}, {'text': '你好', 'scene': ['哄睡']}]
        def factory():
            self.fail('Invalid input attempted to load model')
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            serve(io.StringIO('\n'.join(json.dumps(r) for r in invalid)), output, factory)
        self.assertEqual([json.loads(l) for l in output.getvalue().splitlines()],
                         [{'error': 'synthesis_failed'}] * len(invalid))
        self.assertEqual(validate_request({'text': '你好'}), ('你好', 42, '日常', '中', None))
        self.assertEqual(validate_request({'text': '你好', 'style': '开心', 'intensity': '高', 'scene': '哄睡'}),
                         ('你好', 42, '开心', '高', '哄睡'))

    def test_styles_map_to_fixed_references_and_scaled_vectors(self):
        for preset in PRESETS.values():
            reference = Path(preset['reference'])
            self.assertFalse(reference.is_absolute() or '..' in reference.parts)
        for style in ('激动', '压着怒气', '哽咽'):
            self.assertIsNone(emotion_vector(style, '高'))
        self.assertIsNone(emotion_vector('日常', '高'))
        self.assertEqual(emotion_vector('开心', '中'), PRESETS['开心']['vector'])
        low, high = emotion_vector('开心', '低'), emotion_vector('开心', '高')
        self.assertAlmostEqual(low[0], PRESETS['开心']['vector'][0] * 0.6)
        self.assertGreater(high[0], PRESETS['开心']['vector'][0])

    def test_persistence_diagnostics_and_failure_recovery(self):
        created, calls = [], []
        class Engine:
            def synthesize(self, text, seed, style, intensity, scene):
                calls.append((text, seed, style))
                print('Engine diagnostic')
                if text == '失败':
                    raise RuntimeError('Internal details')
                return audio_bytes(), {'seed': seed}
        def factory():
            created.append(True)
            print('Loading diagnostic')
            return Engine()
        output, diagnostics = io.StringIO(), io.StringIO()
        source = '\n'.join(json.dumps({'text': text, 'seed': seed, 'style': style})
                           for text, seed, style in [('第一句', 42, '开心'), ('失败', 7, '日常'), ('第三句', 0, '安慰')])
        with contextlib.redirect_stderr(diagnostics):
            serve(io.StringIO(source), output, factory)
        results = [json.loads(l) for l in output.getvalue().splitlines()]
        self.assertEqual(created, [True])
        self.assertEqual([c[2] for c in calls], ['开心', '日常', '安慰'])
        self.assertEqual(results[1], {'error': 'synthesis_failed'})
        self.assertNotIn('Internal details', output.getvalue())
        self.assertIn('Loading diagnostic', diagnostics.getvalue())
        for result in [results[0], results[2]]:
            validate_wav(base64.b64decode(result['wav'], validate=True))

    def test_rejects_invalid_audio_before_delivery(self):
        class Engine:
            def synthesize(self, text, seed, style, intensity, scene):
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

    def test_long_internal_silences_are_capped_but_edges_and_short_gaps_kept(self):
        rate = 22050
        tone = (8000 * np.sin(2 * np.pi * 300 * np.arange(rate // 2) / rate)).astype(np.int16)
        def gap(seconds): return np.zeros(int(rate * seconds), dtype=np.int16)
        samples = np.concatenate([gap(.3), tone, gap(3.0), tone, gap(.4), tone, gap(2.0)])
        result = cap_pauses(samples[:, None], rate)
        self.assertAlmostEqual(len(result) / rate, len(samples) / rate - 2.0, delta=.03)
        self.assertTrue((result[:int(rate * .3)] == 0).all())  # leading silence untouched
        self.assertTrue((result[-int(rate * 2.0):] == 0).all())  # trailing silence untouched
        short = np.concatenate([tone, gap(.8), tone])
        self.assertEqual(len(cap_pauses(short, rate)), len(short))

    def test_scene_library_picks_usable_matches_and_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'wav').mkdir()
            ids = ['zh_vo_A_1', 'zh_vo_A_2', 'zh_vo_A_3', 'zh_vo_A_4', '../escape']
            for ident in ids[:4]:
                (root / 'wav' / (ident + '.wav')).write_bytes(b'')
            entries = [dict(id=i, restricted=(i == 'zh_vo_A_3')) for i in ids]
            (root / 'index.json').write_text(_json.dumps(entries))
            axes = np.eye(5, dtype=np.float32)
            np.savez(root / 'index.npz', note=axes, line=axes)
            (root / 'qc.json').write_text(_json.dumps({'passed': ['zh_vo_A_1', 'zh_vo_A_3', '../escape']}))
            vectors = {'哄睡': axes[0], '冷笑': axes[2], '无关': axes[1] * 0, '台词': axes[0]}
            library = SceneLibrary([root], lambda texts: np.stack([vectors[t] for t in texts]))
            self.assertEqual(library.ids, ['zh_vo_A_1'])  # failed QC, restricted and unsafe ids excluded
            path, ident, score = library.pick('哄睡', '台词')
            self.assertEqual((path.name, ident, score), ('zh_vo_A_1.wav', 'zh_vo_A_1', 1.0))
            self.assertEqual(library.pick('冷笑', '无关')[:2], (None, None))
            log = [_json.loads(l) for l in (root / 'usage.jsonl').read_text().splitlines()]
            self.assertEqual([r['reference'] for r in log], ['zh_vo_A_1', None])
            self.assertTrue(all(set(r) == {'time', 'reference', 'score'} for r in log))
            second = root / 'second'
            (second / 'wav').mkdir(parents=True)
            (second / 'wav' / 'role_9.wav').write_bytes(b'')
            (second / 'index.json').write_text(_json.dumps([dict(id='role_9', restricted=False)]))
            np.savez(second / 'index.npz', note=axes[2:3], line=axes[2:3])
            (second / 'qc.json').write_text(_json.dumps({'passed': ['role_9']}))
            both = SceneLibrary([root, second], library.embed)
            self.assertEqual(both.pick('冷笑', '无关')[:2], (second / 'wav' / 'role_9.wav', 'role_9'))
            (second / 'index.json').write_text(_json.dumps([dict(id='zh_vo_A_1', restricted=False)]))
            (second / 'wav' / 'zh_vo_A_1.wav').write_bytes(b'')
            (second / 'qc.json').write_text(_json.dumps({'passed': ['zh_vo_A_1']}))
            with self.assertRaisesRegex(ValueError, 'unique'):
                SceneLibrary([root, second], library.embed)

    def test_rejects_empty_stereo_nonfinite_and_excessive_audio(self):
        for samples in [np.array([]), np.zeros((20, 2)), np.array([np.nan]),
                        np.zeros(8000 * 120 + 1)]:
            with self.assertRaises(ValueError):
                pcm_wav(8000, samples)
        with self.assertRaises(ValueError):
            pcm_wav(0, np.zeros(20))


if __name__ == '__main__':
    unittest.main()

import unittest
from qq_chat.speech_runtime import RecoveringEngine, SESSIONS, fade_audio, segment_fades


class Session:
    def __init__(self, cuda): self.cuda = cuda
    def get_providers(self):
        return ['CUDAExecutionProvider', 'CPUExecutionProvider'] if self.cuda else ['CPUExecutionProvider']


class Engine:
    def __init__(self, cuda=True, fail=False):
        for name in SESSIONS: setattr(self, name, Session(cuda))
        self.fail = fail
    def synthesize(self, text, **kwargs):
        if self.fail: raise RuntimeError('GPU out of memory')
        return text


class RecoveryTests(unittest.TestCase):
    def test_cuda_unavailable_or_silent_cpu_fallback(self):
        for fail in (True, False):
            calls = []
            def load(path, provider):
                calls.append((path, provider))
                if provider == 'cuda' and fail: raise RuntimeError('CUDA unavailable')
                return Engine(cuda=False)
            engine = RecoveringEngine(load, 'full', 'saver')
            self.assertEqual(engine.provider, 'cpu')
            self.assertEqual(calls, [('full', 'cuda'), ('saver', 'cpu')])
            self.assertEqual(engine.synthesize('你好', seed=42), '你好')

    def test_inference_oom_recovers_once_and_stays_on_cpu(self):
        calls = []
        def load(path, provider):
            calls.append((path, provider))
            return Engine(fail=provider == 'cuda')
        engine = RecoveringEngine(load, 'full', 'saver')
        self.assertEqual(engine.provider, 'cuda')
        self.assertEqual(engine.synthesize('你好'), '你好')
        self.assertEqual(engine.provider, 'cpu')
        self.assertEqual(engine.synthesize('第二句'), '第二句')
        self.assertEqual(calls, [('full', 'cuda'), ('saver', 'cpu')])

    def test_cpu_failure_is_reported_without_retry_loop(self):
        calls = []
        def load(path, provider):
            calls.append((path, provider))
            return Engine(fail=True)
        engine = RecoveringEngine(load, 'full', 'saver', 'cpu')
        with self.assertRaises(RuntimeError): engine.synthesize('你好')
        self.assertEqual(calls, [('full', 'cpu')])


class SegmentTests(unittest.TestCase):
    def test_fade_preserves_length_center_and_original(self):
        original = [1.0] * 3200
        result = fade_audio(original)
        self.assertEqual(len(result), len(original))
        self.assertEqual(result[0], 0)
        self.assertEqual(result[-1], 0)
        self.assertEqual(result[480:-480], [1.0] * (3200 - 960))
        self.assertEqual(original, [1.0] * 3200)
        self.assertTrue(all(a <= b for a, b in zip(result[:479], result[1:480])))

    def test_short_segments_and_invalid_fade(self):
        for length in (0, 1, 2, 3, 10):
            self.assertEqual(len(fade_audio([1.0] * length)), length)
        with self.assertRaises(ValueError): fade_audio([1.0], milliseconds=30)

    def test_fades_apply_to_every_engine_segment(self):
        class MultiSegment:
            def _synthesize_segment(self, text): return [1.0] * 100
            def synthesize(self):
                return self._synthesize_segment('第一句') + [0.0] * 11200 + self._synthesize_segment('第二句')
        result = segment_fades(MultiSegment()).synthesize()
        self.assertEqual([result[i] for i in (0, 99, 11300, 11399)], [0, 0, 0, 0])
        self.assertEqual(result[100:11300], [0.0] * 11200)

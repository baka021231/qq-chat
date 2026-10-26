import unittest
from qq_chat.speech_runtime import RecoveringEngine, SESSIONS


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

from pathlib import Path
import tempfile
import unittest

from tools.voice_backend import load_reference_text
from qq_chat.speech_runtime import RecoveringEngine, SESSIONS


class ReferenceTests(unittest.TestCase):
    def test_explicit_override_keeps_original_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original.txt'
            override = Path(directory) / 'confirmed.txt'
            original.write_text('同桌，你好~')
            override.write_text('同桌，你好！\n')
            self.assertEqual(load_reference_text(override), '同桌，你好！')
            self.assertEqual(original.read_text(), '同桌，你好~')
            self.assertIsNone(load_reference_text(None))
            override.write_text('  ')
            with self.assertRaises(ValueError): load_reference_text(override)
            with self.assertRaises(FileNotFoundError): load_reference_text(Path(directory) / 'missing')

    def test_confirmed_prompt_survives_gpu_failure(self):
        captured = []
        class Session:
            def get_providers(self): return ['CUDAExecutionProvider', 'CPUExecutionProvider']
        class Engine:
            def __init__(self, provider):
                self.provider = provider
                for name in SESSIONS: setattr(self, name, Session())
            def synthesize(self, text, **options):
                captured.append((self.provider, options['prompt_text']))
                if self.provider == 'cuda': raise RuntimeError('Out of memory')
                return text
        engine = RecoveringEngine(lambda path, provider: Engine(provider), 'full', 'saver')
        self.assertEqual(engine.synthesize('测试', prompt_text='同桌，你好！'), '测试')
        self.assertEqual(captured, [('cuda', '同桌，你好！'), ('cpu', '同桌，你好！')])

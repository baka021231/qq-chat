import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from tools.batch_voice import run_grid
from tools.voice_backend import sampling_parameters


class SamplingTests(unittest.TestCase):
    def test_defaults_and_requested_parameters(self):
        self.assertEqual(sampling_parameters({}), {'seed': 7, 'temperature': 1.0})
        self.assertEqual(sampling_parameters({'seed': 9, 'temperature': .7}), {'seed': 9, 'temperature': .7})
        for params in ({'seed': True}, {'seed': -1}, {'seed': 2**32}, {'temperature': True},
                       {'temperature': float('nan')}, {'temperature': float('inf')}, {'temperature': 0}, {'temperature': 3}):
            with self.subTest(params=params), self.assertRaises(ValueError): sampling_parameters(params)


class GridTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignored_sampling_parameters_fail_the_batch(self):
        script = 'import sys,json\nfor line in sys.stdin: print(json.dumps({"metrics":{"seed":42,"temperature":1.0}}),flush=True)'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'grid'
            with self.assertRaises(ValueError):
                await run_grid([sys.executable, '-u', '-c', script], target, '测试', [0], [.7])
            self.assertEqual(json.loads((target / 'manifest.json').read_text())['status'], 'failed')
            self.assertEqual(list(target.glob('*.wav')), [])

    async def test_each_grid_combination_is_sent_and_recorded_without_overwrite(self):
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 320)
        encoded = base64.b64encode(output.getvalue()).decode()
        script = ('import sys,json\nfor line in sys.stdin:\n'
                  ' r=json.loads(line)\n'
                  f' print(json.dumps({{"wav":{encoded!r},"metrics":{{"seed":r["seed"],"temperature":r["temperature"],"provider":"cpu"}}}}),flush=True)\n')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'grid'
            report = await run_grid([sys.executable, '-u', '-c', script], target, '测试3次~', [0, 1], [.7, 1.3])
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(len(list(target.glob('*.wav'))), 4)
            self.assertEqual({(s['seed'], s['temperature']) for s in report['samples']},
                             {(0, .7), (0, 1.3), (1, .7), (1, 1.3)})
            saved = (target / 'manifest.json').read_bytes()
            with self.assertRaises(FileExistsError):
                await run_grid([sys.executable], target, '测试', [0], [.7])
            self.assertEqual(saved, (target / 'manifest.json').read_bytes())

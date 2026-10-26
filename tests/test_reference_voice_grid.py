import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from tools.batch_voice import run_jobs
from tools.reference_voice_grid import build_jobs, SENTENCES
from tools.voice_backend import validate_reference_text


class ReferenceMatrixTests(unittest.TestCase):
    def test_complete_matrix_and_only_reference_punctuation_changes(self):
        jobs, refs = build_jobs('同桌，你好~我是爱弥斯。')
        self.assertEqual(len(jobs), 30)
        self.assertEqual(len({j['file'] for j in jobs}), 30)
        self.assertEqual({j['seed'] for j in jobs}, {7, 9, 1})
        self.assertEqual({j['temperature'] for j in jobs}, {1.0})
        self.assertEqual(refs['exclamation'], refs['tilde'].replace('~', '！'))
        self.assertTrue({'开心', '平淡', '问句'} <= {s['tone'] for s in SENTENCES})
        for seed in (7, 9, 1):
            for number in range(1, 6):
                pair = [j for j in jobs if j['seed'] == seed and j['sentence_id'] == number]
                self.assertEqual(len(pair), 2)
                self.assertEqual(pair[0]['text'], pair[1]['text'])
                self.assertEqual(pair[0]['prompt_text'].replace('~', '！'), pair[1]['prompt_text'])

    def test_reference_validation(self):
        self.assertEqual(validate_reference_text(' 同桌，你好~ '), '同桌，你好~')
        for text in (None, 3, '', ' ', 'a\0b', 'a' * 1001):
            with self.subTest(text=str(text)[:10]), self.assertRaises(ValueError):
                validate_reference_text(text)


class ReferenceProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_versions_are_sent_separately_and_confirmed(self):
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 320)
        encoded = base64.b64encode(output.getvalue()).decode()
        script = ('import sys,json,hashlib\nfor line in sys.stdin:\n'
                  ' r=json.loads(line)\n'
                  ' m={k:r[k] for k in ("seed","temperature")}\n'
                  ' m["reference_text_sha256"]=hashlib.sha256(r["prompt_text"].encode()).hexdigest()\n'
                  f' print(json.dumps({{"wav":{encoded!r},"metrics":m}}),flush=True)\n')
        jobs, _ = build_jobs('同桌，你好~')
        pair = [jobs[0], jobs[5]]
        with tempfile.TemporaryDirectory() as directory:
            result = await run_jobs([sys.executable, '-u', '-c', script], Path(directory) / 'grid', pair, {})
            self.assertEqual(result['status'], 'complete')
            self.assertEqual([s['prompt_text'] for s in result['samples']], ['同桌，你好~', '同桌，你好！'])
            self.assertNotEqual(result['samples'][0]['metrics']['reference_text_sha256'],
                                result['samples'][1]['metrics']['reference_text_sha256'])

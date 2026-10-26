from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.benchmark_voice import main


class BenchmarkTests(unittest.TestCase):
    def test_original_sample_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / 'before.wav'
            sample.write_bytes(b'original sample')
            with patch('sys.argv', ['benchmark_voice', '--output-dir', directory, '--name', 'before']), \
                    patch('sys.stderr'), patch('tools.benchmark_voice.subprocess.run') as run:
                with self.assertRaises(SystemExit): main()
                run.assert_not_called()
            self.assertEqual(sample.read_bytes(), b'original sample')

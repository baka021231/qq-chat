import unittest
from tools.resynth_reference import ORIGINAL_TEXT, build_jobs
from tools.voice_backend import sampling_parameters


class ResynthesisTests(unittest.TestCase):
    def test_original_text_is_not_cleaned_or_overridden(self):
        jobs = build_jobs(ORIGINAL_TEXT)
        self.assertEqual([j['file'] for j in jobs], ['resynth-7.wav', 'resynth-9.wav', 'resynth-1.wav'])
        for job in jobs:
            self.assertEqual(job['text'], ORIGINAL_TEXT)
            self.assertIn('~', job['text'])
            self.assertNotIn('prompt_text', job)
            self.assertIs(job['expected_reference_override'], False)
            self.assertEqual(job['temperature'], 1.0)
        with self.assertRaises(ValueError): build_jobs(ORIGINAL_TEXT.replace('~', '！'))

    def test_new_default_retains_explicit_seed_override(self):
        self.assertEqual(sampling_parameters({})['seed'], 7)
        self.assertEqual(sampling_parameters({'seed': 42})['seed'], 42)

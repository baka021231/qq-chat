"""Resynthesize the exact original reference sentence without text cleanup."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from tools.batch_voice import run_jobs

ORIGINAL_TEXT = '同桌，你好~我是拉贝尔学部的爱弥斯，和你一样。'


def build_jobs(reference_text):
    if reference_text != ORIGINAL_TEXT:
        raise ValueError('Package reference does not match the requested original sentence')
    digest = hashlib.sha256(reference_text.encode()).hexdigest()
    return [{'file': f'resynth-{seed}.wav', 'seed': seed, 'temperature': 1.0,
             'raw_text': reference_text, 'text': reference_text,
             'expected_reference_text_sha256': digest, 'expected_reference_override': False}
            for seed in (7, 9, 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads((BASE / 'config.json').read_text())
    if cfg['voice']['enabled']:
        raise ValueError('Daily voice must remain disabled')
    command = cfg['voice']['command']
    if any(a == '--reference-text-file' or a.startswith('--reference-text-file=') for a in command):
        raise ValueError('Remove the reference text override before resynthesis')
    package = Path(command[command.index('--model-dir') + 1])
    text = (package / 'reference.txt').read_text().strip()
    audio_digest = hashlib.sha256((package / 'reference.wav').read_bytes()).hexdigest()
    if '--cpu-model-dir' in command:
        fallback = Path(command[command.index('--cpu-model-dir') + 1])
        if ((fallback / 'reference.txt').read_text().strip() != text
                or hashlib.sha256((fallback / 'reference.wav').read_bytes()).hexdigest() != audio_digest):
            raise ValueError('CPU fallback must use the identical reference audio and text')
    model = json.loads((package / 'manifest.json').read_text())
    metadata = {'experiment': 'exact original reference resynthesis', 'reference_text': text,
                'text_cleaning': 'bypassed: preserve exact original sentence including ~',
                'reference_audio_sha256': audio_digest, 'seeds': [7, 9, 1], 'temperature': 1.0,
                'reference_text_override': False, 'daily_voice_enabled': False,
                'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BASE, text=True).strip(),
                'context': {'model': model['name'], 'source_model_version': model['source_model_version'],
                            'precision_profile': model['precision_profile'], 'runtime_revision': model['runtime_revision'],
                            'text_split_method': 'cut3', 'fragment_interval': .35, 'fade_ms': 15,
                            'top_k': 15, 'top_p': 1., 'repetition_penalty': 1.35, 'speed_factor': 1., 'max_steps': 500}}
    asyncio.run(run_jobs(command, args.output_dir, build_jobs(text), metadata))


if __name__ == '__main__':
    main()

"""Compare selected seeds and reference punctuation across five utterances."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from qq_chat.voice import speech_text
from tools.batch_voice import run_jobs

SENTENCES = [
    {'id': 1, 'tone': '开心', 'text': '太好啦！我们终于把这件事做成了，今天一定要好好庆祝。'},
    {'id': 2, 'tone': '平淡', 'text': '我把资料放在桌上了，等你有空的时候再看就好。'},
    {'id': 3, 'tone': '问句', 'text': '你今天过得怎么样？有没有什么有趣的事情想和我说？'},
    {'id': 4, 'tone': '安慰', 'text': '没关系，慢慢来。我在这里陪着你，我们一起想办法。'},
    {'id': 5, 'tone': '认真', 'text': '先确认每一步的结果，再决定下一步怎么做。这一次，我会认真完成。'},
]


def build_jobs(tilde_reference):
    if tilde_reference.count('~') != 1:
        raise ValueError('Expected one wave mark in the original reference')
    references = {'tilde': tilde_reference, 'exclamation': tilde_reference.replace('~', '！')}
    jobs = []
    for seed in (7, 9, 1):
        for label, prompt in references.items():
            for sentence in SENTENCES:
                jobs.append({'file': f'{seed:02d}-{label}-{sentence["id"]:02d}.wav',
                             'seed': seed, 'temperature': 1.0, 'reference_version': label,
                             'sentence_id': sentence['id'], 'tone': sentence['tone'],
                             'raw_text': sentence['text'], 'text': speech_text(sentence['text']),
                             'prompt_text': prompt})
    return jobs, references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads((BASE / 'config.json').read_text())
    if cfg.get('voice', {}).get('enabled', False):
        raise ValueError('Daily voice must remain disabled during this experiment')
    command = cfg['voice']['command']
    model_dir = Path(command[command.index('--model-dir') + 1])
    original = (model_dir / 'reference.txt').read_text().strip()
    jobs, references = build_jobs(original)
    model = json.loads((model_dir / 'manifest.json').read_text())
    metadata = {'experiment': 'reference punctuation across content and tone', 'seeds': [7, 9, 1],
                'temperature': 1.0, 'references': references, 'sentences': SENTENCES,
                'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BASE, text=True).strip(),
                'context': {'model': model['name'], 'source_model_version': model['source_model_version'],
                            'precision_profile': model['precision_profile'], 'runtime_revision': model['runtime_revision'],
                            'reference_audio_sha256': hashlib.sha256((model_dir / 'reference.wav').read_bytes()).hexdigest(),
                            'text_split_method': 'cut3', 'fragment_interval': .35, 'fade_ms': 15,
                            'top_k': 15, 'top_p': 1., 'repetition_penalty': 1.35, 'speed_factor': 1., 'max_steps': 500},
                'daily_voice_enabled': False}
    asyncio.run(run_jobs(command, args.output_dir, jobs, metadata))


if __name__ == '__main__':
    main()

"""Generate a seed/temperature grid with one persistent backend; no QQ sends."""
import argparse
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import time
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from qq_chat.voice import MAX_AUDIO_BYTES, speech_text, validate_wav
from tools.benchmark_voice import TEST_TEXT
from tools.voice_backend import sampling_parameters


async def run_grid(command, output_dir, raw_text, seeds, temperatures, context=None):
    text = speech_text(raw_text)
    if not text or len(text) > 140:
        raise ValueError('Grid test text must fit one 140-character chunk')
    jobs = []
    for seed in seeds:
        for temperature in temperatures:
            params = sampling_parameters({'seed': seed, 'temperature': temperature})
            name = f'seed-{seed:02d}_temp-{temperature:g}'.replace('.', 'p') + '.wav'
            jobs.append((name, params))
    if not jobs or len({name for name, _ in jobs}) != len(jobs):
        raise ValueError('Grid must contain unique seed/temperature combinations')
    # Never overwrite existing comparisons or silently mix two runs.
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'raw_text': raw_text, 'spoken_text': text,
              'seeds': seeds, 'temperatures': temperatures, 'context': context or {}, 'samples': []}
    manifest = output_dir / 'manifest.json'
    def save(): manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    process = None
    try:
        with (output_dir / 'backend.stderr.log').open('wb') as diagnostics:
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=diagnostics, limit=MAX_AUDIO_BYTES * 2)
            for index, (name, params) in enumerate(jobs, 1):
                started = time.perf_counter()
                async with asyncio.timeout(180):
                    process.stdin.write((json.dumps({'text': text, **params}, ensure_ascii=False) + '\n').encode())
                    await process.stdin.drain()
                    response = json.loads(await process.stdout.readline())
                metrics = response.get('metrics', {})
                if any(metrics.get(key) != value for key, value in params.items()):
                    raise ValueError('Backend did not confirm requested sampling parameters')
                audio = base64.b64decode(response['wav'], validate=True)
                validate_wav(audio)
                with (output_dir / name).open('xb') as target: target.write(audio)
                with wave.open(io.BytesIO(audio)) as stream:
                    duration = stream.getnframes() / stream.getframerate()
                report['samples'].append({'file': name, **params, 'duration_seconds': duration,
                    'roundtrip_seconds': time.perf_counter() - started,
                    'sha256': hashlib.sha256(audio).hexdigest(), 'metrics': metrics})
                save()
                print(f'{index}/{len(jobs)} {name} duration={duration:.2f}s provider={metrics.get("provider")}', flush=True)
        report['status'] = 'complete'
        save()
    except BaseException as error:
        report['status'], report['error_type'] = 'failed', type(error).__name__
        save()
        raise
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seeds', type=int, nargs='+', default=list(range(10)))
    parser.add_argument('--temperatures', type=float, nargs='+', default=[.7, 1., 1.3])
    parser.add_argument('--text', default=TEST_TEXT)
    args = parser.parse_args()
    cfg = json.loads((BASE / 'config.json').read_text())['voice']
    command = cfg['command']
    model_dir = Path(command[command.index('--model-dir') + 1])
    model = json.loads((model_dir / 'manifest.json').read_text())
    context = {'model': model['name'], 'source_model_version': model['source_model_version'],
               'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BASE, text=True).strip(),
               'precision_profile': model['precision_profile'], 'runtime_revision': model['runtime_revision'],
               'text_split_method': 'cut3', 'fragment_interval': .35, 'fade_ms': 15,
               'top_k': 15, 'top_p': 1., 'repetition_penalty': 1.35, 'speed_factor': 1., 'max_steps': 500}
    if '--reference-text-file' in command:
        context['reference_text'] = Path(command[command.index('--reference-text-file') + 1]).read_text().strip()
    asyncio.run(run_grid(command, args.output_dir, args.text, args.seeds, args.temperatures, context))


if __name__ == '__main__':
    main()

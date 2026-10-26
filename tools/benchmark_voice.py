"""Save one reproducible local WAV and timing report; never sends QQ messages."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from qq_chat.voice import speech_text, speech_chunks, validate_wav

TEST_TEXT = '同桌，你好~我是爱弥斯。今天完成了3项测试……准备好了吗？Ow< 😊'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--text', default=TEST_TEXT)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
        parser.error('--name must be a simple artifact name')
    text = speech_text(args.text)
    if len(list(speech_chunks(text))) != 1:
        parser.error('Use a test text within one 140-character chunk')
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    targets = {ext: directory / (args.name + ext) for ext in ('.wav', '.json', '.stderr.log')}
    if any(path.exists() for path in targets.values()):
        parser.error('Refusing to overwrite existing benchmark artifacts')
    cfg = json.loads((BASE / 'config.json').read_text())['voice']
    start = time.perf_counter()
    process = subprocess.run(cfg['command'], input=json.dumps({'text': text}, ensure_ascii=False) + '\n',
                             capture_output=True, text=True, timeout=240)
    elapsed = time.perf_counter() - start
    targets['.stderr.log'].write_text(process.stderr)
    if process.returncode:
        raise RuntimeError('Backend failed; see the local benchmark error log')
    response = json.loads(process.stdout)
    audio = base64.b64decode(response['wav'], validate=True)
    validate_wav(audio)
    with targets['.wav'].open('xb') as output:
        output.write(audio)
    with wave.open(str(targets['.wav'])) as stream:
        duration = stream.getnframes() / stream.getframerate()
    report = {'name': args.name, 'raw_text': args.text, 'spoken_text': text, 'seed': 42,
              'cold_total_seconds': elapsed, 'duration_seconds': duration,
              'sha256': hashlib.sha256(audio).hexdigest(), 'backend_metrics': response.get('metrics'),
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BASE, text=True).strip()}
    with targets['.json'].open('x') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()

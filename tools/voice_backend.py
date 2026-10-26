"""Persistent local ONNX adapter; run using the isolated speech Python.

stdin: {"text": "..."} per line; stdout: {"wav": "<base64 PCM WAV>"}.
The engine is loaded once. Diagnostics use stderr, never the protocol stream.
"""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    package = args.model_dir.resolve(strict=True)
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location('aemeath_onnx_engine', package / 'infer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = module.AimisiOnnx(package, module.select_providers('cpu'))
        import soundfile
    for line in sys.stdin:
        try:
            request = json.loads(line)
            text = request['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 160:
                raise ValueError('Expected 1-160 text characters')
            with contextlib.redirect_stdout(sys.stderr):
                audio = engine.synthesize(text, language='auto', seed=42)
                output = io.BytesIO()
                soundfile.write(output, audio, 32000, format='WAV', subtype='PCM_16')
            response = {'wav': base64.b64encode(output.getvalue()).decode('ascii')}
        except Exception as error:
            print('Speech synthesis failed: ' + type(error).__name__, file=sys.stderr, flush=True)
            response = {'error': 'synthesis_failed'}
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()

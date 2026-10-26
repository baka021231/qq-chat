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
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.speech_runtime import RecoveringEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--cpu-model-dir', type=Path)
    parser.add_argument('--provider', choices=('auto', 'cpu', 'cuda'), default='auto')
    args = parser.parse_args()
    package = args.model_dir.resolve(strict=True)
    cpu_package = (args.cpu_model_dir or package).resolve(strict=True)
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location('aemeath_onnx_engine', package / 'infer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        def loader(path, provider):
            if hasattr(module.ort, 'preload_dlls') and provider == 'cuda':
                module.ort.preload_dlls(directory='')
            return module.AimisiOnnx(path, module.select_providers(provider))
        engine = RecoveringEngine(loader, package, cpu_package, args.provider)
        import soundfile
    load_seconds = time.perf_counter() - started
    for line in sys.stdin:
        try:
            request = json.loads(line)
            text = request['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 160:
                raise ValueError('Expected 1-160 text characters')
            with contextlib.redirect_stdout(sys.stderr):
                started = time.perf_counter()
                audio = engine.synthesize(text, language='auto', seed=42)
                synthesis_seconds = time.perf_counter() - started
                output = io.BytesIO()
                soundfile.write(output, audio, 32000, format='WAV', subtype='PCM_16')
            response = {'wav': base64.b64encode(output.getvalue()).decode('ascii'), 'metrics': {
                'provider': engine.provider, 'fallback_reason': engine.fallback_reason,
                'load_seconds': load_seconds, 'synthesis_seconds': synthesis_seconds}}
        except Exception as error:
            print('Speech synthesis failed: ' + type(error).__name__, file=sys.stderr, flush=True)
            response = {'error': 'synthesis_failed'}
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()

"""Persistent local ONNX adapter; run using the isolated speech Python.

stdin: {"text": "..."} per line; stdout: {"wav": "<base64 PCM WAV>"}.
The engine is loaded once. Diagnostics use stderr, never the protocol stream.
"""
import argparse
import base64
import hashlib
import contextlib
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.speech_runtime import RecoveringEngine, segment_fades


def validate_reference_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1000 or '\0' in text:
        raise ValueError('Invalid reference text override')
    return text.strip()


def load_reference_text(path):
    return None if path is None else validate_reference_text(path.read_text(encoding='utf-8'))


def sampling_parameters(request):
    seed = request.get('seed', 42)
    temperature = request.get('temperature', 1.0)
    if type(seed) is not int or not 0 <= seed <= 2 ** 32 - 1:
        raise ValueError('seed must be an unsigned 32-bit integer')
    if (type(temperature) not in (int, float) or not math.isfinite(temperature)
            or not .01 <= temperature <= 2.0):
        raise ValueError('temperature must be between 0.01 and 2.0')
    return {'seed': seed, 'temperature': float(temperature)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--cpu-model-dir', type=Path)
    parser.add_argument('--provider', choices=('auto', 'cpu', 'cuda'), default='auto')
    parser.add_argument('--text-split-method', choices=('cut3', 'cut5'), default='cut3')
    parser.add_argument('--fragment-interval', type=float, default=0.35)
    parser.add_argument('--fade-ms', type=int, choices=(0, *range(10, 21)), default=15)
    parser.add_argument('--reference-text-file', type=Path)
    args = parser.parse_args()
    if not .3 <= args.fragment_interval <= .5:
        parser.error('--fragment-interval must be 0.3-0.5 seconds')
    package = args.model_dir.resolve(strict=True)
    cpu_package = (args.cpu_model_dir or package).resolve(strict=True)
    reference_text = load_reference_text(args.reference_text_file)
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location('aemeath_onnx_engine', package / 'infer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        def loader(path, provider):
            if hasattr(module.ort, 'preload_dlls') and provider == 'cuda':
                module.ort.preload_dlls(directory='')
            loaded = module.AimisiOnnx(path, module.select_providers(provider))
            return segment_fades(loaded, args.fade_ms) if args.fade_ms else loaded
        engine = RecoveringEngine(loader, package, cpu_package, args.provider)
        import soundfile
    load_seconds = time.perf_counter() - started
    for line in sys.stdin:
        try:
            request = json.loads(line)
            text = request['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 160:
                raise ValueError('Expected 1-160 text characters')
            sampling = sampling_parameters(request)
            prompt = validate_reference_text(request['prompt_text']) if 'prompt_text' in request else reference_text
            with contextlib.redirect_stdout(sys.stderr):
                started = time.perf_counter()
                audio = engine.synthesize(text, language='auto', **sampling,
                                          text_split_method=args.text_split_method,
                                          fragment_interval=args.fragment_interval,
                                          prompt_text=prompt, prompt_lang='zh')
                synthesis_seconds = time.perf_counter() - started
                output = io.BytesIO()
                soundfile.write(output, audio, 32000, format='WAV', subtype='PCM_16')
            response = {'wav': base64.b64encode(output.getvalue()).decode('ascii'), 'metrics': {
                'provider': engine.provider, 'fallback_reason': engine.fallback_reason,
                'load_seconds': load_seconds, 'synthesis_seconds': synthesis_seconds,
                'text_split_method': args.text_split_method,
                'fragment_interval': args.fragment_interval, 'fade_ms': args.fade_ms,
                'reference_text_override': prompt is not None,
                'reference_text_sha256': hashlib.sha256(prompt.encode()).hexdigest() if prompt else None,
                **sampling}}
        except Exception as error:
            print('Speech synthesis failed: ' + type(error).__name__, file=sys.stderr, flush=True)
            response = {'error': 'synthesis_failed'}
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()

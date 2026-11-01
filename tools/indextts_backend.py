"""Local V5 IndexTTS adapter for the bridge's persistent JSONL WAV protocol.

Run with the isolated IndexTTS Python. Model files and speaker references are
selected at startup, never from incoming requests: a request names one of the
fixed styles below. No QQ credentials or history.
"""
import argparse
import base64
import contextlib
import inspect
import io
import json
import math
import os
from pathlib import Path
import random
import sys
import textwrap
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.voice import INTENSITIES, STYLES, validate_wav

# The speaker reference drives rhythm and pauses as well as timbre, so each
# style names its main reference. Vectors are IndexTTS2's
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm].
PRESETS = {
    '日常': dict(reference='跟读日常平和原声.wav', vector=None),
    '开心': dict(reference='跟读日常平和原声.wav', vector=[0.5, 0, 0, 0, 0, 0, 0.1, 0]),
    '安慰': dict(reference='跟读日常平和原声.wav', vector=[0, 0, 0.1, 0, 0, 0.15, 0, 0.3]),
}
INTENSITY_SCALE = {'低': 0.6, '中': 1.0, '高': 1.4}
# The package author's runtime played the V5 output at 1.15x.
SPEED = 1.15
# IndexTTS2 defaults; greedy decoding read flat and slow.
SAMPLING = dict(do_sample=True, top_p=0.8, top_k=30, temperature=0.8,
                num_beams=3, repetition_penalty=10.0)
assert tuple(PRESETS) == STYLES and tuple(INTENSITY_SCALE) == INTENSITIES


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {'text', 'seed', 'style', 'intensity'}:
        raise ValueError('Expected text and optional seed, style and intensity only')
    text = request.get('text')
    seed = request.get('seed', 42)
    style = request.get('style', '日常')
    intensity = request.get('intensity', '中')
    if not isinstance(text, str) or not text.strip() or len(text) > 160 or '\0' in text:
        raise ValueError('Expected 1-160 text characters')
    if type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ValueError('Expected unsigned 32-bit seed')
    if style not in PRESETS or intensity not in INTENSITY_SCALE:
        raise ValueError('Unknown style or intensity')
    return text, seed, style, intensity


def emotion_vector(style, intensity):
    """Raw vector before IndexTTS2's own bias and 0.8 total cap; None keeps the reference's emotion."""
    vector = PRESETS[style]['vector']
    if vector is None:
        return None
    return [value * INTENSITY_SCALE[intensity] for value in vector]


def patch_duration(infer_v2):
    """Scale IndexTTS2's semantic-to-mel length, as IndexTTS-2.5's duration_factor does.

    Changes speed without changing pitch. Refuses to start if the engine line moved.
    """
    source = textwrap.dedent(inspect.getsource(infer_v2.IndexTTS2.infer_generator))
    line = 'target_lengths = (code_lens * 1.72).long()'
    if source.count(line) != 1:
        raise RuntimeError('Unsupported IndexTTS engine revision for speed control')
    source = source.replace(line, "target_lengths = (code_lens * 1.72 * getattr(self, 'duration_factor', 1.0)).long()")
    scope = {}
    exec(compile(source, infer_v2.__file__, 'exec'), infer_v2.__dict__, scope)
    infer_v2.IndexTTS2.infer_generator = scope['infer_generator']


def pcm_wav(sample_rate, samples):
    """Convert IndexTTS's integer-amplitude audio without changing its pitch."""
    import numpy as np
    from scipy.signal import resample_poly

    if type(sample_rate) is not int or not 8000 <= sample_rate <= 96000:
        raise ValueError('Invalid source sample rate')
    audio = np.asarray(samples)
    if audio.ndim == 2 and audio.shape[1] == 1:
        audio = audio[:, 0]
    if (audio.ndim != 1 or not 0 < len(audio) <= sample_rate * 120
            or not np.isfinite(audio).all()):
        raise ValueError('Invalid source audio')
    audio = audio.astype(np.float32)
    if sample_rate != 32000:
        divisor = math.gcd(sample_rate, 32000)
        audio = resample_poly(audio, 32000 // divisor, sample_rate // divisor)
    pcm = np.clip(np.rint(audio), -32768, 32767).astype('<i2')
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm.tobytes())
    data = output.getvalue()
    validate_wav(data)
    return data


class IndexSynthesizer:
    def __init__(self, package_dir, device):
        os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                          TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='4')
        self.package = package_dir.resolve(strict=True)
        engine_dir = self.package / 'engine'
        sys.path.insert(0, str(engine_dir))
        import numpy as np
        import torch
        from indextts import infer_v2

        self.np, self.torch = np, torch
        torch.set_num_threads(4)
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable; original ONNX configuration is retained for rollback')
        self.device = 'cuda:0' if device == 'cuda' else 'cpu'
        models = self.package / 'models'
        aux = models / 'hf_cache'
        self.references = {style: self.package / 'references' / preset['reference']
                           for style, preset in PRESETS.items()}
        checkpoint = models / '小爱V5/gpt.pth'
        aux_paths = dict(w2v_bert=str(aux / 'w2v-bert-2.0'),
                         semantic_codec=str(aux / 'semantic_codec_model.safetensors'),
                         campplus=str(aux / 'campplus_cn_common.bin'),
                         bigvgan=str(aux / 'bigvgan'))
        for path in [models / 'config.yaml', *self.references.values(), checkpoint,
                     *(Path(p) for p in aux_paths.values())]:
            if not path.exists():
                raise FileNotFoundError('Missing IndexTTS package asset')

        # The supplied Windows runtime also memory-maps this FP16 checkpoint.
        # assign=True avoids retaining another full FP32 copy in host memory.
        native_loader = infer_v2.load_checkpoint

        def mapped_loader(model, path):
            if Path(path).resolve() != checkpoint.resolve():
                return native_loader(model, path)
            state = torch.load(path, map_location='cpu', mmap=True, weights_only=True)
            model.load_state_dict(state.get('model', state), strict=True, assign=True)
            return {}

        started = time.perf_counter()
        infer_v2.load_checkpoint = mapped_loader
        try:
            self.engine = infer_v2.IndexTTS2(
                cfg_path=str(models / 'config.yaml'), model_dir=str(models),
                device=self.device, use_fp16=device == 'cuda', use_cuda_kernel=False,
                use_deepspeed=False, use_accel=False, use_torch_compile=False,
                use_qwen_emo=False, aux_paths=aux_paths)
        finally:
            infer_v2.load_checkpoint = native_loader
        patch_duration(infer_v2)
        self.engine.duration_factor = 1 / SPEED
        self.load_seconds = time.perf_counter() - started

    def synthesize(self, text, seed, style='日常', intensity='中'):
        random.seed(seed)
        self.np.random.seed(seed)
        self.torch.manual_seed(seed)
        if self.device.startswith('cuda'):
            self.torch.cuda.reset_peak_memory_stats()
        vector = emotion_vector(style, intensity)
        if vector is not None:
            vector = self.engine.normalize_emo_vec(vector)
        started = time.perf_counter()
        with self.torch.inference_mode():
            result = self.engine.infer(
                spk_audio_prompt=str(self.references[style]), text=text, output_path=None,
                emo_audio_prompt=None, emo_alpha=1.0, emo_vector=vector,
                use_random=False, use_emo_text=False, verbose=False,
                max_mel_tokens=1500, max_text_tokens_per_segment=120,
                interval_silence=80, **SAMPLING)
        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError('IndexTTS returned no audio')
        sample_rate, samples = result
        data = pcm_wav(sample_rate, samples)
        metrics = dict(model='aemeath-indextts-v5', provider=self.device,
                       load_seconds=self.load_seconds,
                       synthesis_seconds=time.perf_counter() - started,
                       source_sample_rate=sample_rate, output_sample_rate=32000,
                       seed=seed, style=style, intensity=intensity, speed=SPEED,
                       emotion_vector=vector, do_sample=True)
        if self.device.startswith('cuda'):
            metrics['peak_gpu_allocated_mib'] = round(self.torch.cuda.max_memory_allocated() / 2 ** 20)
        return data, metrics


def serve(source, output, factory):
    engine = None
    for line in source:
        try:
            text, seed, style, intensity = validate_request(json.loads(line))
            with contextlib.redirect_stdout(sys.stderr):
                if engine is None:
                    engine = factory()
                audio, metrics = engine.synthesize(text, seed, style, intensity)
            validate_wav(audio)
            response = {'wav': base64.b64encode(audio).decode('ascii'), 'metrics': metrics}
        except Exception as error:
            # Keep prompts, model internals and file paths out of protocol errors.
            print('IndexTTS synthesis failed: ' + type(error).__name__, file=sys.stderr, flush=True)
            response = {'error': 'synthesis_failed'}
        output.write(json.dumps(response) + '\n')
        output.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--device', choices=('cuda', 'cpu'), default='cuda')
    args = parser.parse_args()
    serve(sys.stdin, sys.stdout, lambda: IndexSynthesizer(args.package_dir, args.device))


if __name__ == '__main__':
    main()

"""Local V5 IndexTTS adapter for the bridge's persistent JSONL WAV protocol.

Run with the isolated IndexTTS Python. Model files and speaker references are
selected at startup, never from incoming requests. No QQ credentials or history.
"""
import argparse
import base64
import contextlib
import io
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.voice import validate_wav


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {'text', 'seed'}:
        raise ValueError('Expected text and optional seed only')
    text = request.get('text')
    seed = request.get('seed', 42)
    if not isinstance(text, str) or not text.strip() or len(text) > 160 or '\0' in text:
        raise ValueError('Expected 1-160 text characters')
    if type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ValueError('Expected unsigned 32-bit seed')
    return text, seed


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
        self.speaker = self.package / 'references/音色.wav'
        self.emotion = self.package / 'references/跟读日常平和原声.wav'
        checkpoint = models / '小爱V5/gpt.pth'
        aux_paths = dict(w2v_bert=str(aux / 'w2v-bert-2.0'),
                         semantic_codec=str(aux / 'semantic_codec_model.safetensors'),
                         campplus=str(aux / 'campplus_cn_common.bin'),
                         bigvgan=str(aux / 'bigvgan'))
        for path in [models / 'config.yaml', self.speaker, self.emotion, checkpoint,
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
        self.load_seconds = time.perf_counter() - started

    def synthesize(self, text, seed):
        random.seed(seed)
        self.np.random.seed(seed)
        self.torch.manual_seed(seed)
        if self.device.startswith('cuda'):
            self.torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        with self.torch.inference_mode():
            result = self.engine.infer(
                spk_audio_prompt=str(self.speaker), text=text, output_path=None,
                emo_audio_prompt=str(self.emotion), emo_alpha=0.55,
                do_sample=False, use_random=False, use_emo_text=False, verbose=False,
                num_beams=1, repetition_penalty=5.0, max_mel_tokens=1500,
                max_text_tokens_per_segment=120, interval_silence=80)
        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError('IndexTTS returned no audio')
        sample_rate, samples = result
        data = pcm_wav(sample_rate, samples)
        metrics = dict(model='aemeath-indextts-v5', provider=self.device,
                       load_seconds=self.load_seconds,
                       synthesis_seconds=time.perf_counter() - started,
                       source_sample_rate=sample_rate, output_sample_rate=32000,
                       seed=seed, do_sample=False, emotion_alpha=0.55)
        if self.device.startswith('cuda'):
            metrics['peak_gpu_allocated_mib'] = round(self.torch.cuda.max_memory_allocated() / 2 ** 20)
        return data, metrics


def serve(source, output, factory):
    engine = None
    for line in source:
        try:
            text, seed = validate_request(json.loads(line))
            with contextlib.redirect_stdout(sys.stderr):
                if engine is None:
                    engine = factory()
                audio, metrics = engine.synthesize(text, seed)
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

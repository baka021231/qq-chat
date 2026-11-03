"""Local V5 IndexTTS adapter for the bridge's persistent JSONL WAV protocol.

Run with the isolated IndexTTS Python. Model files and speaker references are
selected at startup, never from incoming requests: a request names one of the
fixed styles below, or describes a scene that is matched against an offline
clip library. No QQ credentials or history.
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
import re
import sys
import textwrap
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.voice import INTENSITIES, STYLES, validate_wav

# The speaker reference drives rhythm and pauses as well as timbre, so each
# style names its main reference. Vectors are IndexTTS2's
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm].
# The heavy styles take their emotion and slower pacing from the clip itself,
# labelled by the owner's listening; intensity does not apply to them.
PRESETS = {
    '日常': dict(reference='跟读日常平和原声.wav', vector=None),
    '开心': dict(reference='跟读日常平和原声.wav', vector=[0.5, 0, 0, 0, 0, 0, 0.1, 0]),
    '安慰': dict(reference='跟读日常平和原声.wav', vector=[0, 0, 0.1, 0, 0, 0.15, 0, 0.3]),
    '激动': dict(reference='发声状态/激动.wav', vector=None),
    '压着怒气': dict(reference='终极版/压住情绪.wav', vector=None),
    '哽咽': dict(reference='发声状态/哽咽.wav', vector=None),
}
INTENSITY_SCALE = {'低': 0.6, '中': 1.0, '高': 1.4}
# The package author's runtime played the V5 output at 1.15x.
SPEED = 1.15
# IndexTTS2 defaults; greedy decoding read flat and slow.
SAMPLING = dict(do_sample=True, top_p=0.8, top_k=30, temperature=0.8,
                num_beams=3, repetition_penalty=10.0)
assert tuple(PRESETS) == STYLES and tuple(INTENSITY_SCALE) == INTENSITIES


SCENE_MAX_CHARS = 60


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {'text', 'seed', 'style', 'intensity', 'scene'}:
        raise ValueError('Expected text and optional seed, style, intensity and scene only')
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
    scene = request.get('scene')
    if scene is not None and (not isinstance(scene, str) or not scene.strip()
                              or len(scene) > SCENE_MAX_CHARS or '\0' in scene):
        raise ValueError('Expected 1-60 scene characters')
    return text, seed, style, intensity, scene


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


def cap_pauses(samples, sample_rate, longest=1.0, floor_db=-40):
    """Shorten internal silences beyond `longest` seconds; leading and trailing audio is kept.

    Heavy-style clips are half silence and the model copies multi-second gaps.
    Only frames 40 dB below the peak are dropped, from the middle of the gap.
    """
    import numpy as np

    audio = np.asarray(samples).reshape(-1)
    frame = sample_rate // 100
    count = len(audio) // frame
    if count < 3:
        return audio
    frames = audio[:count * frame].astype(np.float64).reshape(count, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    quiet = 20 * np.log10(rms / rms.max()) < floor_db
    keep = np.ones(len(audio), dtype=bool)
    limit, start = int(longest * 100), None
    for index, silent in enumerate(np.append(quiet, False)):
        if silent and start is None:
            start = index
        elif not silent and start is not None:
            if start > 0 and index < count and index - start > limit:
                keep[(start + limit // 2) * frame:(index - limit + limit // 2) * frame] = False
            start = None
    return audio[keep]


class TextEmbedder:
    """bge-small-zh sentence vectors (CLS, L2-normalised), loaded from a local folder only."""
    def __init__(self, model_dir):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        self.model = AutoModel.from_pretrained(str(model_dir), local_files_only=True).eval()

    def __call__(self, texts):
        batch = self.tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors='pt')
        with self.torch.no_grad():
            vectors = self.model(**batch).last_hidden_state[:, 0]
        return self.torch.nn.functional.normalize(vectors, dim=-1).numpy()


class SceneLibrary:
    """Choose the clip whose tone note and line best fit a requested scene.

    Each folder is built offline in workspace/voice-library/<source>: index.json
    (clips with tone notes and the styles each fits), index.npz (their note/line
    embeddings) and qc.json (clips that synthesised cleanly). Restricted clips
    (another character's voice through hers, unsure speaker, battle shouts) and
    failed ones are never chosen.
    """
    NOTE_WEIGHT = 0.75  # how she says it matters more than what was said
    # The small embedder misses emotional direction ("cold reproach" matched "warm
    # and gratified"), so the requested style first narrows the clips to those
    # labeled for it, and the scene ranks only those.
    # A clip used in the last few picks steps aside for one nearly as close, so
    # similar banter does not repeat the same clip line after line.
    RECENT, RECENT_PENALTY = 6, 0.05
    # Light styles never borrow crying, anger or grim narration, whatever the scene says.
    LIGHT_STYLES = ('日常', '开心', '安慰')
    # Imitate the closest clip even when none fits closely: the owner preferred that
    # to falling back to a preset. Only nonsense scenes fall back to the style.
    THRESHOLD = 0.45
    CLIP_ID = re.compile(r'^[A-Za-z0-9_]+$')

    def __init__(self, folders, embed):
        import numpy as np
        self.np, self.embed = np, embed
        folders = [Path(folder).resolve(strict=True) for folder in folders]
        self.log = folders[0] / 'usage.jsonl'
        self.clips, notes, lines, heavy, styles = [], [], [], [], []
        for folder in folders:
            entries = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
            vectors = np.load(folder / 'index.npz')
            passed = set(json.loads((folder / 'qc.json').read_text(encoding='utf-8'))['passed'])
            keep = [i for i, entry in enumerate(entries)
                    if not entry['restricted'] and entry['id'] in passed and self.CLIP_ID.match(entry['id'])
                    and (folder / 'wav' / (entry['id'] + '.wav')).is_file()]
            self.clips += [(entries[i]['id'], folder / 'wav' / (entries[i]['id'] + '.wav')) for i in keep]
            heavy.append(np.array([bool(entries[i].get('heavy')) for i in keep], dtype=bool))
            # A clip without labels (older index) fits every style; an empty list fits none.
            styles += [entries[i].get('styles', STYLES) for i in keep]
            notes.append(vectors['note'][keep])
            lines.append(vectors['line'][keep])
            if keep and entries[keep[0]].get('note'):
                self.check_embedder(folder, entries[keep[0]]['note'], vectors['note'][keep[0]])
        if not self.clips:
            raise ValueError('Scene library has no usable clips')
        if len({ident for ident, _ in self.clips}) != len(self.clips):
            raise ValueError('Clip ids must be unique across libraries')
        self.ids = [ident for ident, _ in self.clips]
        self.notes, self.lines = np.concatenate(notes), np.concatenate(lines)
        self.heavy = np.concatenate(heavy)
        self.fits = {name: np.array([name in labels for labels in styles], dtype=bool) for name in STYLES}
        self.recent = self.recent_picks()

    def check_embedder(self, folder, note, stored):
        """Vectors only compare within one model: re-embed one stored note to catch a
        library built with a different embedder than the one passed with --embedder."""
        vector = self.embed([note])[0]
        if vector.shape != stored.shape or float(vector @ stored) < 0.99:
            raise ValueError(f'{folder.name}/index.npz was built with another embedder; '
                             'rebuild it with build_index.py using the --embedder model')

    def recent_picks(self):
        """Seed recency from the usage log so a restart does not forget the last picks."""
        try:
            rows = [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()[-50:]]
        except (OSError, ValueError):
            return []
        used = [row.get('reference') for row in rows if isinstance(row, dict)]
        return [ident for ident in used if ident in self.ids][-self.RECENT:]

    def pick(self, scene, text, style=None):
        """Return (clip path, clip id, score), or (None, None, score) when nothing fits well."""
        scene_vector, text_vector = self.embed([scene, text])
        scores = (self.NOTE_WEIGHT * (self.notes @ scene_vector)
                  + (1 - self.NOTE_WEIGHT) * (self.lines @ text_vector))
        if style in self.LIGHT_STYLES and not self.heavy.all():
            scores = self.np.where(self.heavy, -1.0, scores)
        if style in self.fits and self.fits[style].any():
            scores = self.np.where(self.fits[style], scores, -1.0)
        recent = self.np.array([ident in self.recent for ident in self.ids])
        best = int(self.np.argmax(scores - self.RECENT_PENALTY * recent))
        score = round(float(scores[best]), 3)
        chosen = self.ids[best] if score >= self.THRESHOLD else None
        if chosen is not None:
            self.recent = (self.recent + [chosen])[-self.RECENT:]
        # Local record of which clip was used, for "that one sounded wrong" follow-ups.
        # Time, clip and score only; never the scene or the spoken text.
        with open(self.log, 'a', encoding='utf-8') as log:
            log.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                  'reference': chosen, 'score': score}) + '\n')
        if chosen is None:
            return None, None, score
        return self.clips[best][1], chosen, score


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
    def __init__(self, package_dir, device, library=None, embedder=None):
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
        self.library = SceneLibrary(library, TextEmbedder(embedder)) if library else None  # list of folders
        self.load_seconds = time.perf_counter() - started

    def synthesize(self, text, seed, style='日常', intensity='中', scene=None):
        random.seed(seed)
        self.np.random.seed(seed)
        self.torch.manual_seed(seed)
        if self.device.startswith('cuda'):
            self.torch.cuda.reset_peak_memory_stats()
        clip, clip_id, score = (self.library.pick(scene, text, style) if scene and self.library
                                else (None, None, None))
        if clip is not None:
            # A matched clip carries its own emotion and pace.
            reference, vector, speed = clip, None, 1.0
        else:
            reference, vector, speed = self.references[style], emotion_vector(style, intensity), SPEED
        if vector is not None:
            vector = self.engine.normalize_emo_vec(vector)
        self.engine.duration_factor = 1 / speed
        started = time.perf_counter()
        with self.torch.inference_mode():
            result = self.engine.infer(
                spk_audio_prompt=str(reference), text=text, output_path=None,
                emo_audio_prompt=None, emo_alpha=1.0, emo_vector=vector,
                use_random=False, use_emo_text=False, verbose=False,
                max_mel_tokens=1500, max_text_tokens_per_segment=120,
                interval_silence=80, **SAMPLING)
        if not isinstance(result, tuple) or len(result) != 2:
            raise ValueError('IndexTTS returned no audio')
        sample_rate, samples = result
        data = pcm_wav(sample_rate, cap_pauses(samples, sample_rate))
        metrics = dict(model='aemeath-indextts-v5', provider=self.device,
                       load_seconds=self.load_seconds,
                       synthesis_seconds=time.perf_counter() - started,
                       source_sample_rate=sample_rate, output_sample_rate=32000,
                       seed=seed, style=style, intensity=intensity, speed=speed,
                       emotion_vector=vector, do_sample=True,
                       scene_reference=clip_id, scene_score=score)
        if self.device.startswith('cuda'):
            metrics['peak_gpu_allocated_mib'] = round(self.torch.cuda.max_memory_allocated() / 2 ** 20)
        return data, metrics


def serve(source, output, factory):
    engine = None
    for line in source:
        try:
            request = json.loads(line)
            if request == {'warmup': True}:
                with contextlib.redirect_stdout(sys.stderr):
                    if engine is None:
                        engine = factory()
                response = {'ready': True}
                output.write(json.dumps(response) + '\n')
                output.flush()
                continue
            text, seed, style, intensity, scene = validate_request(request)
            with contextlib.redirect_stdout(sys.stderr):
                if engine is None:
                    engine = factory()
                audio, metrics = engine.synthesize(text, seed, style, intensity, scene)
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
    parser.add_argument('--library', type=Path, action='append',
                        help='scene clip library folder (index.json, index.npz, qc.json, wav/); repeatable')
    parser.add_argument('--embedder', type=Path, help='local bge-small-zh folder; required with --library')
    args = parser.parse_args()
    if bool(args.library) != bool(args.embedder):
        parser.error('--library and --embedder go together')
    serve(sys.stdin, sys.stdout,
          lambda: IndexSynthesizer(args.package_dir, args.device, args.library, args.embedder))


if __name__ == '__main__':
    main()

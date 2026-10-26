"""Backend policy without ONNX imports: GPU verification and CPU recovery."""
import gc

SESSIONS = ('hubert', 'speaker', 't2s_encoder', 't2s_first', 't2s_stage', 'vits')


def has_cuda(engine):
    return all('CUDAExecutionProvider' in getattr(engine, name).get_providers() for name in SESSIONS)


class RecoveringEngine:
    def __init__(self, loader, model_dir, cpu_model_dir, provider='auto'):
        self.loader = loader
        self.cpu_model_dir = cpu_model_dir
        self.engine = None
        self.provider = 'cpu'
        self.fallback_reason = None
        if provider != 'cpu':
            try:
                self.engine = loader(model_dir, 'cuda')
                if not has_cuda(self.engine):
                    raise RuntimeError('CUDA provider was not activated')
                self.provider = 'cuda'
                return
            except Exception as error:
                self.fallback_reason = type(error).__name__
                self.engine = None
                gc.collect()
        self.engine = loader(cpu_model_dir if provider != 'cpu' else model_dir, 'cpu')

    def synthesize(self, text, **options):
        try:
            return self.engine.synthesize(text, **options)
        except Exception as error:
            if self.provider != 'cuda':
                raise
            self.fallback_reason = type(error).__name__
            self.engine = None
            gc.collect()
            self.engine = self.loader(self.cpu_model_dir, 'cpu')
            self.provider = 'cpu'
            return self.engine.synthesize(text, **options)

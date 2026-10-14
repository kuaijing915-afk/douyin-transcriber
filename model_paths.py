"""Prefer this project's downloaded model files; otherwise fetch via FunASR."""
from pathlib import Path
ROOT = Path(__file__).resolve().parent
MODELS = {
    'paraformer-zh': 'iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch',
    'fsmn-vad': 'iic/speech_fsmn_vad_zh-cn-16k-common-pytorch',
    'ct-punc': 'iic/punc_ct-transformer_cn-en-common-vocab471067-large',
}
def model_path(name):
    for base in (ROOT / '.runtime' / 'models', ROOT / '.runtime' / 'models' / 'models'):
        candidate = base / MODELS[name]
        if (candidate / 'config.yaml').is_file() and (candidate / 'model.pt').is_file():
            return str(candidate)
    return name

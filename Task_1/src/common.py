"""Shared constants, paths and helpers for Task 1 (math fine-tuning vs. retention)."""
from __future__ import annotations

import json
import os
import random
import re
import unicodedata
import warnings
from pathlib import Path

import numpy as np
import torch

# --------------------------------------------------------------------------- #
# Fixed experimental settings (taken directly from the assignment text)
# --------------------------------------------------------------------------- #
SEED = 6304
MODEL_ID = "Qwen/Qwen3-0.6B"
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

MAX_LEN = 2048
N_TRAIN = 40_000
MICRO_BS = 2
GRAD_ACC = 16
EFF_BS = MICRO_BS * GRAD_ACC                 # 32
TOTAL_UPDATES = N_TRAIN // EFF_BS            # 1250
WARMUP_UPDATES = 38
CKPT_UPDATES = [312, 625, 937, 1250]         # 9,984 / 20,000 / 29,984 / 40,000 examples
MID_UPDATE = 625
FINAL_UPDATE = 1250
GRAD_CLIP = 1.0
WEIGHT_DECAY = 0.01
ADAM_BETAS = (0.9, 0.999)
ADAM_EPS = 1e-8
LORA_DROPOUT = 0.05

GSM8K_MAX_NEW_TOKENS = 1024
QUAL_MAX_NEW_TOKENS = 512

METHODS = {
    "fullft": dict(kind="full", lr=2e-5, l2sp=0.0, label="Full FT"),
    "l2sp":   dict(kind="full", lr=2e-5, l2sp=1e-3, label="Full FT + L2-SP"),
    "lora4":  dict(kind="lora", r=4, alpha=8, lr=2e-4, label="LoRA-4"),
    "lora16": dict(kind="lora", r=16, alpha=32, lr=2e-4, label="LoRA-16"),
    "oplora": dict(kind="oplora", r=16, alpha=32, k=128, lr=2e-4, label="OPLoRA-16"),
}

# the seven evaluation conditions: name -> (method, update) ; baseline -> None
CONDITIONS = {
    "baseline":     None,
    "fullft_mid":   ("fullft", MID_UPDATE),
    "fullft_final": ("fullft", FINAL_UPDATE),
    "l2sp_final":   ("l2sp", FINAL_UPDATE),
    "lora4_final":  ("lora4", FINAL_UPDATE),
    "lora16_final": ("lora16", FINAL_UPDATE),
    "oplora_final": ("oplora", FINAL_UPDATE),
}
CONDITION_LABELS = {
    "baseline": "Baseline", "fullft_mid": "Full FT (mid, 625)", "fullft_final": "Full FT (final)",
    "l2sp_final": "Full FT + L2-SP", "lora4_final": "LoRA-4", "lora16_final": "LoRA-16",
    "oplora_final": "OPLoRA-16",
}

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get("T1_ARTIFACTS", ROOT / "artifacts"))
DATA_DIR = ARTIFACTS / "data"
RUNS_DIR = ARTIFACTS / "runs"
EVAL_MODELS_DIR = ARTIFACTS / "eval_models"
RESULTS_DIR = ARTIFACTS / "results"
DIAG_DIR = ARTIFACTS / "diagnostics"
QUAL_DIR = ARTIFACTS / "qualitative"
REPORT_DIR = ARTIFACTS / "report_assets"
PROBE_SET_PATH = DATA_DIR / "probe_set.json"
TRAIN_DATA_PATH = DATA_DIR / "train_tokenized.pt"


def run_dir(method: str, root: Path | None = None) -> Path:
    return Path(root or RUNS_DIR) / method


def ckpt_dir(method: str, step: int, root: Path | None = None) -> Path:
    return run_dir(method, root) / f"ckpt_update_{step:04d}"


def results_dir(cond: str) -> Path:
    return RESULTS_DIR / cond


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def set_seed(seed: int = SEED) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def dir_size(path: Path) -> int:
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def write_json(path: Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def normalize_text(s: str) -> str:
    """Text normalisation used for GSM8K-test decontamination: NFKC, lowercase,
    punctuation -> space, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# Models / tokenizer
# --------------------------------------------------------------------------- #
def build_tiny_model():
    """Tiny random Qwen3 used only by the CPU smoke tests (--tiny)."""
    from transformers import Qwen3Config, Qwen3ForCausalLM
    torch.manual_seed(SEED)
    cfg = Qwen3Config(vocab_size=128, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
                      num_attention_heads=4, num_key_value_heads=2, head_dim=16,
                      max_position_embeddings=256, tie_word_embeddings=True)
    return Qwen3ForCausalLM(cfg)


def load_base_model(dtype=torch.float32, tiny: bool = False):
    from transformers import AutoModelForCausalLM
    if tiny:
        return build_tiny_model().to(dtype)
    return AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype=dtype)


def cast_params_(model, dtype) -> None:
    """Cast parameters only (buffers such as RoPE inv_freq stay FP32)."""
    for p in model.parameters():
        p.data = p.data.to(dtype)


def load_tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(MODEL_ID)


_THINK_COND = "enable_thinking is defined and enable_thinking is false"


def save_nothink_tokenizer(out_dir: Path) -> None:
    """Save the Qwen3 tokenizer into `out_dir` with a chat template that *always* uses the
    non-thinking form (empty <think></think> block). lm-eval versions that cannot forward
    `enable_thinking=False` to apply_chat_template then still produce the right prompt."""
    tok = load_tokenizer()
    tmpl = tok.chat_template
    if isinstance(tmpl, str) and _THINK_COND in tmpl:
        tok.chat_template = tmpl.replace(_THINK_COND, "true")
    else:
        warnings.warn("Could not patch the Qwen3 chat template for non-thinking mode; "
                      "relying on chat_template_args in lm-eval.")
    tok.save_pretrained(out_dir)

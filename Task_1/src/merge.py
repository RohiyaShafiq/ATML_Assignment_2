"""Build FP16 evaluation models for the 7 conditions (adapters merged into fresh backbones) and
check merged-vs-unmerged logits.

    python src/merge.py --condition all
    python src/merge.py --check            # merged vs unmerged logits for the 3 adapter methods
"""
from __future__ import annotations

import argparse
from pathlib import Path

import torch

import adapters as AD
from common import (CONDITIONS, EVAL_MODELS_DIR, FINAL_UPDATE, LORA_DROPOUT, METHODS, RESULTS_DIR, SEED, RUNS_DIR,
                    ckpt_dir, load_base_model, load_tokenizer, run_dir, save_nothink_tokenizer, write_json)


def adapter_file(method: str, step: int, runs_root=None) -> Path:
    name = "adapter_projected.pt" if METHODS[method]["kind"] == "oplora" else "adapter.pt"
    return ckpt_dir(method, step, runs_root) / "model" / name


def materialize(cond: str, force: bool = False, runs_root=None) -> Path:
    """Return a directory with an FP16 HF model (+ non-thinking tokenizer) for `cond`."""
    out = EVAL_MODELS_DIR / cond
    if (out / "config.json").exists() and not force:
        return out
    out.mkdir(parents=True, exist_ok=True)
    spec = CONDITIONS[cond]
    if spec is None:
        model = load_base_model(torch.float32)
    else:
        method, step = spec
        cfg = METHODS[method]
        if cfg["kind"] == "full":
            from transformers import AutoModelForCausalLM
            model = AutoModelForCausalLM.from_pretrained(ckpt_dir(method, step, runs_root) / "model", torch_dtype=torch.float32)
        else:
            model = load_base_model(torch.float32)                     # fresh backbone copy
            ad = torch.load(adapter_file(method, step, runs_root), weights_only=False)
            AD.merge_adapters_(model, ad["adapters"], cfg["alpha"] / cfg["r"])   # projected factors for OPLoRA
    model.to(torch.float16).save_pretrained(out, safe_serialization=True)
    save_nothink_tokenizer(out)
    return out


@torch.no_grad()
def check_merge(method: str, step: int = FINAL_UPDATE, runs_root=None) -> dict:
    """Unmerged (training-time wrapper incl. OP projection from the *raw* factors, dropout off) vs
    merged (projected factors folded into W) logits, FP32, on a small batch."""
    cfg = METHODS[method]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = load_tokenizer(); tok.padding_side = "right"
    texts = [tok.apply_chat_template([{"role": "user", "content": q}], tokenize=False, add_generation_prompt=True,
                                     enable_thinking=False) + a
             for q, a in [("Natalia sold clips to 48 of her friends in April. How many in total if she sold half as many in May?", "48/2 = 24. 48+24 = 72. The answer is: 72"),
                          ("What is 12 times 7?", "12 * 7 = 84. The answer is: 84"),
                          ("Write a short greeting.", "Hello! Nice to meet you."),
                          ("Solve x + 3 = 10.", "x = 10 - 3 = 7. The answer is: 7")]]
    batch = tok(texts, return_tensors="pt", padding=True).to(device)

    raw = torch.load(ckpt_dir(method, step, runs_root) / "model" / "adapter.pt", weights_only=False)["adapters"]
    bases = None
    if cfg["kind"] == "oplora":
        bases = {n: (u.to(device), v.to(device)) for n, (u, v) in
                 torch.load(run_dir(method, runs_root) / "oplora_bases.pt", map_location="cpu").items()}
    m_un = load_base_model(torch.float32).to(device).eval()
    AD.inject_adapters(m_un, cfg["r"], cfg["alpha"], 0.0, SEED, bases)
    AD.load_adapter_state(m_un, raw)
    lg_un = m_un(**batch).logits.float()
    del m_un

    ad = torch.load(adapter_file(method, step, runs_root), weights_only=False)["adapters"]
    m_mg = load_base_model(torch.float32).to(device).eval()
    AD.merge_adapters_(m_mg, ad, cfg["alpha"] / cfg["r"])
    lg_mg = m_mg(**batch).logits.float()
    mask = batch["attention_mask"].bool()
    d = (lg_un - lg_mg)[mask].abs()
    agree = (lg_un.argmax(-1) == lg_mg.argmax(-1))[mask].float().mean().item()
    res = {"method": method, "max_abs_diff": d.max().item(), "mean_abs_diff": d.mean().item(),
           "top1_agreement": agree, "passed": bool(d.max().item() < 1e-2 and agree > 0.999)}
    print(res)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", default=None, help="condition name or 'all'")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.condition:
        for c in (CONDITIONS if a.condition == "all" else [a.condition]):
            print("materialized", c, "->", materialize(c, a.force))
    if a.check:
        out = [check_merge(m) for m in ("lora4", "lora16", "oplora")]
        write_json(RESULTS_DIR / "merge_checks.json", out)

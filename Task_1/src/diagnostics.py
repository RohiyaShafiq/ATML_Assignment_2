"""Weight-space and activation diagnostics (FP32).

    python src/diagnostics.py
Variants: Full FT (mid, final), Full FT + L2-SP, LoRA-4, LoRA-16, OPLoRA-16 (final).
Layers 0, 13, 27 (zero-based) `down_proj`; protected rank k = 128 from the starting weight.
"""
from __future__ import annotations

import argparse

import pandas as pd
import torch

import adapters as AD
from common import (CONDITIONS, DIAG_DIR, METHODS, PROBE_SET_PATH, SEED, ckpt_dir, load_base_model, load_tokenizer,
                    read_json, write_json)
from data import chat_prompt_text, gsm8k_eval_prompt, hellaswag_eval_prompt, make_probe_set
from merge import adapter_file

LAYERS = [0, 13, 27]
K = 128
VARIANTS = ["fullft_mid", "fullft_final", "l2sp_final", "lora4_final", "lora16_final", "oplora_final"]


def pname(layer): return f"model.layers.{layer}.mlp.down_proj"


@torch.no_grad()
def delta_w(cond: str, layer: int, device) -> torch.Tensor:
    method, step = CONDITIONS[cond]
    cfg = METHODS[method]
    if cfg["kind"] == "full":
        from transformers import AutoModelForCausalLM
        m = AutoModelForCausalLM.from_pretrained(ckpt_dir(method, step) / "model", torch_dtype=torch.float32)
        return (m.get_submodule(pname(layer)).weight.detach() - _W0[layer]).to(device)
    ad = torch.load(adapter_file(method, step), weights_only=False)      # projected factors for OPLoRA
    return AD.delta_from_factors(ad["adapters"][pname(layer)], cfg["alpha"] / cfg["r"]).to(device)


def probe_prompts(tok):
    from datasets import load_dataset
    probe = read_json(PROBE_SET_PATH) if PROBE_SET_PATH.exists() else make_probe_set()
    gsm = load_dataset("openai/gsm8k", "main", split="test")
    hs = load_dataset("Rowan/hellaswag", split="validation")
    math = [chat_prompt_text(tok, gsm8k_eval_prompt(gsm[i]["question"])) for i in probe["gsm8k_test_ids"]]
    nonmath = [hellaswag_eval_prompt(hs[i]) for i in probe["hellaswag_validation_ids"]]
    return {"math(gsm8k)": math, "non-math(hellaswag)": nonmath}


@torch.no_grad()
def cache_inputs(model, tok, prompts, device):
    """z = input of down_proj at the last (non-padding) prompt token, starting model, FP32."""
    store = {l: [] for l in LAYERS}
    hooks = [model.get_submodule(pname(l)).register_forward_hook(
        (lambda l: lambda m, inp, out: store[l].append(inp[0][0, -1].detach().float().cpu()))(l)) for l in LAYERS]
    for p in prompts:                                                    # batch size 1 -> no padding
        ids = tok(p, return_tensors="pt", add_special_tokens=False).to(device)
        model(**ids)
    for h in hooks:
        h.remove()
    return {l: torch.stack(v) for l, v in store.items()}


def main():
    argparse.ArgumentParser().parse_args()
    global _W0
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    base = load_base_model(torch.float32).to(device).eval()
    tok = load_tokenizer()
    _W0 = {l: base.get_submodule(pname(l)).weight.detach().clone().cpu() for l in LAYERS}

    # protected bases from the same starting weights (FP32)
    bases = {}
    for l in LAYERS:
        U, S, Vh = torch.linalg.svd(_W0[l].to(device), full_matrices=False)
        bases[l] = (U[:, :K].contiguous(), Vh[:K].T.contiguous())

    # activation cache (starting model), saved for reuse
    prompts = probe_prompts(tok)
    cache = {dom: cache_inputs(base, tok, ps, device) for dom, ps in prompts.items()}
    torch.save(cache, DIAG_DIR / "cached_down_proj_inputs.pt")
    del base

    wrows, arows = [], []
    for cond in VARIANTS:
        for l in LAYERS:
            dW = delta_w(cond, l, device)
            W0 = _W0[l].to(device)
            Uk, Vk = bases[l]
            nrm = dW.norm()
            rho = ((Uk.T @ dW).norm() ** 2 / nrm ** 2).item() if nrm > 0 else float("nan")   # = ||Uk Uk^T dW||_F^2 / ||dW||_F^2
            eta = (nrm / W0.norm()).item()
            wrows.append({"condition": cond, "layer": l, "rho_k": rho, "eta": eta, "dW_fro": nrm.item(),
                          "left_residual_rel": ((Uk.T @ dW).norm() / nrm).item() if nrm > 0 else float("nan"),
                          "right_residual_rel": ((dW @ Vk).norm() / nrm).item() if nrm > 0 else float("nan")})
            for dom, zs in cache.items():
                z = zs[l].to(device)                                     # (n, 3072)
                R = (z @ dW.T).norm(dim=1) / ((z @ W0.T).norm(dim=1) + 1e-8)
                for i, r in enumerate(R.tolist()):
                    arows.append({"condition": cond, "layer": l, "domain": dom, "probe_idx": i, "R": r})
            print(cond, l, wrows[-1])

    w = pd.DataFrame(wrows); a = pd.DataFrame(arows)
    w.to_csv(DIAG_DIR / "weight_space.csv", index=False); a.to_csv(DIAG_DIR / "activation_R_per_example.csv", index=False)
    summ = a.groupby(["condition", "layer", "domain"])["R"].agg(["mean", "median"]).reset_index()
    summ.to_csv(DIAG_DIR / "activation_R_summary.csv", index=False)
    piv = summ.pivot_table(index=["condition", "layer"], columns="domain", values="mean").reset_index()
    piv["non_math_over_math"] = piv["non-math(hellaswag)"] / piv["math(gsm8k)"]
    out = w.merge(piv, on=["condition", "layer"])
    out.to_csv(DIAG_DIR / "diagnostics_table.csv", index=False)
    write_json(DIAG_DIR / "meta.json", {"k": K, "layers": LAYERS, "seed": SEED, "variants": VARIANTS,
                                        "note": "OPLoRA dW uses projected factors; constraints checked via left/right_residual_rel."})
    print("saved diagnostics ->", DIAG_DIR)


if __name__ == "__main__":
    main()

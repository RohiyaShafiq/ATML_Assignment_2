"""Benchmark evaluation with lm-evaluation-harness for one condition.

    python src/evaluate.py --condition baseline
    python src/evaluate.py --condition fullft_final --tasks gsm8k,hellaswag,boolq,arc_easy
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

from common import (CONDITIONS, GSM8K_MAX_NEW_TOKENS, SEED, load_tokenizer, results_dir, write_json)
from merge import materialize

TASK_NAMES = {"gsm8k": "gsm8k_cot", "hellaswag": "hellaswag", "boolq": "boolq", "arc_easy": "arc_easy"}
PRIMARY = {"gsm8k": "exact_match,flexible-extract", "hellaswag": "acc_norm,none",
           "boolq": "acc,none", "arc_easy": "acc_norm,none"}


def _first_str(x):
    while isinstance(x, (list, tuple)) and x:
        x = x[0]
    return x if isinstance(x, str) else str(x)


def run_lm_eval(model_dir, task, chat, batch_size, limit):
    import lm_eval
    kw = dict(model="hf", model_args=f"pretrained={model_dir},dtype=float16,trust_remote_code=False",
              tasks=[task], num_fewshot=0, batch_size=batch_size, device="cuda", limit=limit,
              log_samples=True, apply_chat_template=chat,
              random_seed=SEED, numpy_random_seed=SEED, torch_random_seed=SEED, fewshot_random_seed=SEED)
    params = inspect.signature(lm_eval.simple_evaluate).parameters
    if chat:
        if "chat_template_args" in params:
            kw["chat_template_args"] = {"enable_thinking": False}
        kw["gen_kwargs"] = f"max_gen_toks={GSM8K_MAX_NEW_TOKENS}"      # greedy + stock `until` kept (merged)
    return lm_eval.simple_evaluate(**kw)


def process_gsm8k(res, tok, model_dir, out_dir):
    samples = res["samples"][TASK_NAMES["gsm8k"]]
    by_doc = {}
    for s in samples:
        d = by_doc.setdefault(s["doc_id"], {"doc_id": s["doc_id"], "target": str(s.get("target"))})
        raw = _first_str(s["resps"])
        d["raw_response"] = raw
        d[f"extracted_{s['filter']}"] = _first_str(s["filtered_resps"])
        d[f"correct_{s['filter']}"] = float(s.get("exact_match", 0.0))
    rows = []
    for d in by_doc.values():
        n_tok = len(tok(d["raw_response"], add_special_tokens=False)["input_ids"])
        d["response_tokens"] = n_tok
        d["truncated"] = n_tok >= GSM8K_MAX_NEW_TOKENS                 # hit the 1,024-token cap
        d["unextractable"] = d.get("extracted_flexible-extract", "") in ("[invalid]", "")
        rows.append(d)
    rows.sort(key=lambda r: r["doc_id"])
    with open(out_dir / "gsm8k_samples.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    n = len(rows)
    ext = [r for r in rows if not r["unextractable"]]
    return {"n": n,
            "flexible_em_recomputed": sum(r.get("correct_flexible-extract", 0) for r in rows) / n,
            "strict_em_recomputed": sum(r.get("correct_strict-match", 0) for r in rows) / n,
            "flexible_em_among_extractable": (sum(r.get("correct_flexible-extract", 0) for r in ext) / len(ext)) if ext else None,
            "truncation_rate": sum(r["truncated"] for r in rows) / n,
            "unextractable_rate": sum(r["unextractable"] for r in rows) / n,
            "mean_response_tokens": sum(r["response_tokens"] for r in rows) / n}


def process_ll(res, task, out_dir, name):
    rows = []
    for s in res["samples"][task]:
        ll = [float(r[0]) if isinstance(r, (list, tuple)) else float(r) for r in _flatten_resps(s["resps"])]
        rows.append({"doc_id": s["doc_id"], "target": s.get("target"), "loglikelihoods": ll,
                     "pred": max(range(len(ll)), key=ll.__getitem__) if ll else None,
                     "acc": s.get("acc"), "acc_norm": s.get("acc_norm")})
    with open(out_dir / f"{name}_samples.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def _flatten_resps(resps):
    out = []
    for r in resps:
        out.append(r[0] if isinstance(r, list) and r and isinstance(r[0], (list, tuple)) else r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", required=True, choices=list(CONDITIONS))
    ap.add_argument("--tasks", default="gsm8k,hellaswag,boolq,arc_easy")
    ap.add_argument("--gen_batch_size", type=int, default=32)
    ap.add_argument("--ll_batch_size", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="debug only; the real runs use complete splits")
    a = ap.parse_args()

    model_dir = materialize(a.condition)
    out_dir = results_dir(a.condition); out_dir.mkdir(parents=True, exist_ok=True)
    mpath = out_dir / "metrics.json"
    metrics = json.loads(mpath.read_text()) if mpath.exists() else {"condition": a.condition}
    tok = load_tokenizer()

    for t in [x.strip() for x in a.tasks.split(",")]:
        task = TASK_NAMES[t]
        chat = t == "gsm8k"
        res = run_lm_eval(model_dir, task, chat, a.gen_batch_size if chat else a.ll_batch_size, a.limit)
        raw = res["results"][task]
        entry = {k: v for k, v in raw.items() if isinstance(v, (int, float))}
        entry["primary"] = raw[PRIMARY[t]]
        entry["n_examples"] = res.get("n-samples", {}).get(task)
        if chat:
            entry["extra"] = process_gsm8k(res, tok, model_dir, out_dir)
        else:
            process_ll(res, task, out_dir, t)
        metrics[t] = entry
        write_json(out_dir / f"lm_eval_{t}.json", {k: v for k, v in res.items() if k != "samples"})
        write_json(mpath, metrics)
        print(a.condition, t, "primary =", entry["primary"])


if __name__ == "__main__":
    main()

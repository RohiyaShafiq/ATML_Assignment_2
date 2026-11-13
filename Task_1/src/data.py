"""Training-data preparation (MetaMathQA) and probe-set construction.

    python src/data.py --stage all
"""
from __future__ import annotations

import argparse
import random
import re
from collections import Counter

import torch
from common import (DATA_DIR, MAX_LEN, MODEL_ID, N_TRAIN, PROBE_SET_PATH, SEED, TRAIN_DATA_PATH,
                    load_tokenizer, normalize_text, write_json)


# --------------------------------------------------------------------------- #
# Prompt builders (shared with diagnostics)
# --------------------------------------------------------------------------- #
def chat_prompt_text(tok, user_text: str, system: str | None = None) -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user_text}]
    return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)


def gsm8k_eval_prompt(question: str) -> str:
    """lm-eval `gsm8k_cot` zero-shot document text."""
    return f"Q: {question}\nA:"


def _hs_preprocess(text: str) -> str:
    text = text.strip()
    text = text.replace(" [title]", ". ")
    text = re.sub("\\[.*?\\]", "", text)
    return text.replace("  ", " ")


def hellaswag_eval_prompt(doc: dict) -> str:
    """lm-eval `hellaswag` context (query) construction."""
    return _hs_preprocess(doc["activity_label"] + ": " + doc["ctx_a"] + " " + doc["ctx_b"].capitalize())


# --------------------------------------------------------------------------- #
# Training data
# --------------------------------------------------------------------------- #
def prepare_train_data() -> None:
    from datasets import load_dataset
    tok = load_tokenizer()
    gsm_test = load_dataset("openai/gsm8k", "main", split="test")
    banned = {normalize_text(q) for q in gsm_test["question"]}
    ds = load_dataset("meta-math/MetaMathQA", split="train")
    im_end = tok.convert_tokens_to_ids("<|im_end|>")

    ids_list, plen_list, src_idx = [], [], []
    excluded = Counter()
    scanned = 0
    for i, ex in enumerate(ds):
        scanned += 1
        q, r = (ex["query"] or "").strip(), (ex["response"] or "").strip()
        if not q or not r:
            excluded["empty"] += 1
            continue
        if normalize_text(ex["query"]) in banned or normalize_text(ex.get("original_question", "")) in banned:
            excluded["gsm8k_test_match"] += 1
            continue
        prompt = chat_prompt_text(tok, q)
        assert prompt.endswith("<think>\n\n</think>\n\n"), "non-thinking template not applied"
        p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
        r_ids = tok(r, add_special_tokens=False)["input_ids"] + [im_end]
        if len(p_ids) + len(r_ids) > MAX_LEN:      # never truncate: drop instead
            excluded["too_long"] += 1
            continue
        ids_list.append(p_ids + r_ids)
        plen_list.append(len(p_ids))
        src_idx.append(i)
        if len(ids_list) == N_TRAIN:
            break
    assert len(ids_list) == N_TRAIN, f"only {len(ids_list)} eligible examples"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    torch.save({"input_ids": ids_list, "prompt_len": plen_list, "source_index": src_idx,
                "pad_id": tok.pad_token_id, "eos_id": im_end}, TRAIN_DATA_PATH)
    write_json(DATA_DIR / "train_meta.json", {
        "n_selected": len(ids_list), "n_scanned": scanned, "excluded": dict(excluded),
        "first_source_index": src_idx[0], "last_source_index": src_idx[-1],
        "mean_len": sum(map(len, ids_list)) / len(ids_list), "model": MODEL_ID,
        "response_loss_tokens": int(sum(len(x) - p for x, p in zip(ids_list, plen_list)))})
    print("train data:", {"selected": len(ids_list), "scanned": scanned, "excluded": dict(excluded)})


# --------------------------------------------------------------------------- #
# Probe set
# --------------------------------------------------------------------------- #
def make_probe_set(n: int = 32) -> dict:
    from datasets import load_dataset
    gsm = load_dataset("openai/gsm8k", "main", split="test")
    hs = load_dataset("Rowan/hellaswag", split="validation")
    probe = {"seed": SEED,
             "gsm8k_test_ids": sorted(random.Random(SEED).sample(range(len(gsm)), n)),
             "hellaswag_validation_ids": sorted(random.Random(SEED).sample(range(len(hs)), n))}
    write_json(PROBE_SET_PATH, probe)
    print("saved probe set ->", PROBE_SET_PATH)
    return probe


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["train", "probe", "all"], default="all")
    a = ap.parse_args()
    if a.stage in ("probe", "all"):
        make_probe_set()
    if a.stage in ("train", "all"):
        prepare_train_data()

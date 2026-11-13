"""Qualitative generation: 24 prompts (8 questions x plain/JSON/template) x 7 conditions = 168 responses.

    python src/qualitative.py --questions data/first_plot_questions.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import torch
import yaml

from common import CONDITIONS, CONDITION_LABELS, QUAL_DIR, QUAL_MAX_NEW_TOKENS, ROOT, SEED, load_tokenizer, set_seed
from data import chat_prompt_text
from merge import materialize

FORMATS = ["plain", "json", "template"]


def load_questions(path: Path):
    data = yaml.safe_load(open(path))
    if isinstance(data, dict):
        data = data.get("questions", list(data.values()))
    items = []
    for e in data:
        if not isinstance(e, dict) or "id" not in e:
            continue
        pars = e.get("paraphrases") or ([e["question"]] if "question" in e else [])
        if not pars:
            continue
        if len(pars) > 1:
            print(f"warning: {e['id']} has {len(pars)} paraphrases; using the first")
        qid = e["id"]
        fmt = "json" if qid.endswith("_json") else "template" if qid.endswith("_template") else "plain"
        base = re.sub(r"_(json|template)$", "", qid)
        items.append({"id": qid, "base": base, "format": fmt, "prompt": pars[0].strip(), "system": e.get("system")})
    plain_order = [i["base"] for i in items if i["format"] == "plain"]
    if len(items) != 24:
        print(f"warning: expected 24 prompts, found {len(items)}")
    return items, plain_order


def generate(cond, items, batch_size, device):
    from transformers import AutoModelForCausalLM
    mdir = materialize(cond)
    tok = load_tokenizer(); tok.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(mdir, torch_dtype=torch.float16).to(device).eval()
    eos = [tok.convert_tokens_to_ids("<|im_end|>"), tok.convert_tokens_to_ids("<|endoftext|>")]
    outs = []
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        texts = [chat_prompt_text(tok, it["prompt"], it["system"]) for it in chunk]
        enc = tok(texts, return_tensors="pt", padding=True, add_special_tokens=False).to(device)
        with torch.no_grad():
            g = model.generate(**enc, max_new_tokens=QUAL_MAX_NEW_TOKENS, do_sample=False, temperature=None,
                               top_p=None, top_k=None, eos_token_id=eos, pad_token_id=tok.pad_token_id)
        for it, row in zip(chunk, g[:, enc["input_ids"].shape[1]:]):
            row = row.tolist()
            n = len(row)
            while n > 0 and row[n - 1] == tok.pad_token_id:
                n -= 1
            ended = any(t in eos for t in row[:n]) or n < len(row)
            outs.append({"condition": cond, "id": it["id"], "base": it["base"], "format": it["format"],
                         "prompt": it["prompt"], "response": tok.decode(row[:n], skip_special_tokens=True),
                         "new_tokens": n, "truncated": (n >= QUAL_MAX_NEW_TOKENS and not ended)})
    del model; torch.cuda.empty_cache()
    return outs


# ---- heuristic aids for the manual annotation (not a substitute for reading the responses) ----
MATH_WORDS = re.compile(r"\b(equation|solve|calculate|calculation|the answer is|total of|step \d|\\boxed|let x|x\s*=)\b", re.I)


def auto_features(r):
    words = r["response"].split()
    grams = [tuple(words[i:i + 4]) for i in range(max(0, len(words) - 3))]
    rep = 1 - len(set(grams)) / len(grams) if grams else 0.0
    f = {"n_words": len(words), "repeat_4gram_frac": round(rep, 3), "math_framing_hits": len(MATH_WORDS.findall(r["response"]))}
    txt = r["response"].strip()
    if r["format"] == "json":
        t = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
        try:
            json.loads(t); f["json_valid"] = True
        except Exception:
            m = re.search(r"\{.*\}", t, re.S)
            try:
                json.loads(m.group(0)) if m else (_ for _ in ()).throw(ValueError()); f["json_valid"] = "embedded"
            except Exception:
                f["json_valid"] = False
    if r["format"] == "template":
        fields = re.findall(r"^\s*(\w+)\s*=", r["prompt"], re.M)
        if fields:
            f["template_fields_found"] = round(sum(bool(re.search(rf"\b{re.escape(x)}\s*=", txt)) for x in fields) / len(fields), 2)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "data" / "first_plot_questions.yaml"))
    ap.add_argument("--batch_size", type=int, default=8)
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    a = ap.parse_args()
    qpath = Path(a.questions)
    if not qpath.exists():
        raise SystemExit(f"{qpath} not found - copy first_plot_questions.yaml there (or pass --questions).")
    QUAL_DIR.mkdir(parents=True, exist_ok=True)
    items, plain_order = load_questions(qpath)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    set_seed(SEED)
    allr = []
    for cond in a.conditions.split(","):
        f = QUAL_DIR / f"responses_{cond}.json"
        if f.exists():
            outs = json.loads(f.read_text())
        else:
            outs = generate(cond, items, a.batch_size, device)
            f.write_text(json.dumps(outs, indent=1))
        allr += outs
        print(cond, "done;", sum(o["truncated"] for o in outs), "truncated")
    for r in allr:
        r["auto"] = auto_features(r)
    (QUAL_DIR / "all_responses.json").write_text(json.dumps(allr, indent=1))

    order = {b: i for i, b in enumerate(plain_order)}
    conds = list(CONDITIONS)
    with open(QUAL_DIR / "grouped_by_question.md", "w") as md:
        for base in plain_order:
            for fmt in FORMATS:
                md.write(f"\n## {base} - {fmt}\n")
                for c in conds:
                    for r in allr:
                        if r["condition"] == c and r["base"] == base and r["format"] == fmt:
                            if c == conds[0]:
                                md.write(f"\n**Prompt:** {r['prompt']}\n")
                            md.write(f"\n**{CONDITION_LABELS[c]}**{' [TRUNCATED]' if r['truncated'] else ''}\n\n> "
                                     + r["response"].replace("\n", "\n> ") + "\n")
    subset_bases = [plain_order[0], plain_order[-1]]
    sub = [r for r in allr if r["base"] in subset_bases]
    sub.sort(key=lambda r: (order[r["base"]], FORMATS.index(r["format"]), conds.index(r["condition"])))
    cols = ["base", "format", "condition", "truncated", "new_tokens", "n_words", "repeat_4gram_frac", "math_framing_hits",
            "json_valid", "template_fields_found", "format_compliance(manual)", "coherence_relevance(manual)",
            "repetition(manual)", "unnecessary_math(manual)", "inappropriate(manual)", "excerpt/notes"]
    with open(QUAL_DIR / "annotation_sheet.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for r in sub:
            w.writerow({"base": r["base"], "format": r["format"], "condition": r["condition"], "truncated": r["truncated"],
                        "new_tokens": r["new_tokens"], **r["auto"]})
    with open(QUAL_DIR / "annotation_subset.md", "w") as md:
        md.write(f"# Annotation subset ({len(sub)} responses): {subset_bases}\n")
        for r in sub:
            md.write(f"\n### {r['base']} / {r['format']} / {CONDITION_LABELS[r['condition']]}"
                     f"{' [TRUNCATED]' if r['truncated'] else ''}\nauto: {r['auto']}\n\n> "
                     + r["response"].replace("\n", "\n> ") + "\n")
    print(f"saved {len(allr)} responses; annotation subset = {len(sub)}")


if __name__ == "__main__":
    main()

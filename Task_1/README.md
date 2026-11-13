# Task 1 – Math fine-tuning vs. retention (Qwen3-0.6B, MetaMathQA)

Five variants (Full FT, Full FT + L2-SP, LoRA-4, LoRA-16, OPLoRA-16), seven evaluation
conditions, weight/activation diagnostics, qualitative generation and report tables/figures.

## Layout
```
src/common.py       constants (seed 6304, schedule, methods, paths), helpers
src/data.py         MetaMathQA selection (first 40,000 eligible), GSM8K-test decontamination, probe set (32+32 ids)
src/adapters.py     own LoRA / OPLoRA (effective factors B~, A~; FP32 bases; export of projected factors)
src/train.py        one training run per method (AMP fp16, GradScaler, checkpoints, resume, L2-SP, logging)
src/merge.py        FP16 eval models for the 7 conditions (adapters merged into fresh backbones) + merged/unmerged logit check
src/evaluate.py     lm-evaluation-harness runs (GSM8K gsm8k_cot, HellaSwag, BoolQ, ARC-Easy) + per-example outputs
src/diagnostics.py  rho_k, eta (layers 0/13/27 down_proj), activation ratio R(z) on the cached probe inputs
src/qualitative.py  24 prompts x 7 conditions = 168 responses, grouped file, 42-response annotation subset
src/analyze.py      tables + figures -> artifacts/report_assets/
tests/test_core.py  CPU unit tests (LoRA/OPLoRA constraints, merge equivalence, loss masking)
scripts/run_all.sh  whole pipeline in order
```

## Run
```bash
pip install -r requirements.txt
cp /path/to/first_plot_questions.yaml data/first_plot_questions.yaml   # NOT included: you must supply it
bash scripts/run_all.sh
```
Everything is written under `artifacts/` (override with `T1_ARTIFACTS=/big/disk`). Full FT checkpoints
store FP32 weights + FP32 AdamW state (~2.4 GB + ~4.8 GB each, four checkpoints per Full FT run).
Smoke test without GPU/hub: `PYTHONPATH=src python src/train.py --method oplora --tiny --out_root /tmp/t` and
`python tests/test_core.py`.

Resume: `python src/train.py --method fullft --resume artifacts/runs/fullft/ckpt_update_0625`
(optimizer, scheduler, GradScaler, RNG and data position restored; data order is fixed, so position = update × 32).

## How the spec is implemented (and judgement calls)
* **Data**: scan MetaMathQA in source order; drop empty query/response, any query/original_question whose normalised
  text (NFKC, lowercase, punctuation→space) equals a GSM8K-test question, and examples whose *formatted* length
  (chat prompt incl. `<think>\n\n</think>\n\n` + response + `<|im_end|>`) exceeds 2,048 tokens. Stops at 40,000. No shuffling/truncation.
  Prompt and response are tokenised separately and concatenated; loss only on response + `<|im_end|>`.
* **Loss**: per-microbatch CE *sum* divided by the number of valid response tokens of the whole 32-example effective batch.
* **Precision**: Full FT = FP32 master weights + fp16 autocast; adapters = FP16 frozen backbone (parameters only, RoPE buffers stay FP32) + FP32 adapters.
  `GradScaler`, `unscale_` before clipping, skipped updates logged (`skipped` per update + total in `resources.json`).
* **Weight decay** 0.01 on parameters with ≥2 dims only (also applies to LoRA A/B, as the rule is generic).
* **LR schedule**: linear warmup over 38 updates (reaches peak at update 38) then cosine to 0 at update 1,250.
* **L2-SP**: Ω = Σ‖θ−θ0‖² over unique parameters (tied embedding/LM head counted once), not averaged; θ0 on GPU in FP32;
  the penalty gradient is added once per optimizer update (through the GradScaler). SFT loss, Ω, λΩ, total logged separately.
  Full FT keeps θ0 on CPU only to log displacement ‖θ−θ0‖₂ at each checkpoint.
* **LoRA/OPLoRA**: own implementation; A ~ kaiming-uniform(a=√5), B=0, seeded per module name so LoRA-16 and OPLoRA share identical raw factors.
  OPLoRA bases (k=128) from FP32 SVD once; projections computed with autocast disabled; `adapter_projected.pt` exported at each checkpoint with
  numerical check of Uᵀ ΔW≈0 and ΔW V≈0 (stored in `resources.json → checkpoints → constraint_check`).
* **Evaluation**: each condition is materialised as an FP16 HF model (`artifacts/eval_models/<cond>`) with a tokenizer whose chat template is
  patched to always emit the empty think block (non-thinking), and `chat_template_args={"enable_thinking": False}` is also passed when the installed
  lm-eval supports it. GSM8K: `gsm8k_cot`, 0-shot, chat template, greedy, `max_gen_toks=1024`, stock stop sequences/filters. Others: stock completion prompts, no chat template.
  Truncation flag = generated text ≥1,024 tokens; unextractable = flexible filter returned `[invalid]`.
* **Diagnostics**: ΔW in FP32 from the saved FP32 weights (Full FT) or from factors (LoRA raw, OPLoRA projected). ρ_k computed as ‖UkᵀΔW‖²/‖ΔW‖²
  (identical to ‖UkUkᵀΔW‖² for orthonormal Uk). z cached with forward hooks at the last prompt token (GSM8K: chat-templated `Q: …\nA:`; HellaSwag: raw context).
* **Qualitative**: the loader expects the usual structure (`id`, `paraphrases`, optional `system`; ids ending `_json` / `_template` mark the formats).
  The 42-response annotation subset (first & last plain question) is exported with automatic helper features (JSON validity, template field coverage, 4-gram repetition,
  maths-keyword hits) – the actual annotation of format compliance, coherence, inappropriate content etc. is manual (`annotation_sheet.csv`).

## Notes / caveats
* Not executed end-to-end in the authoring environment (no GPU, no network): the code was only syntax-checked there. Run the unit tests and the `--tiny` smoke test first.
* `first_plot_questions.yaml` was not provided and is not included.
* Batched HF generation (left padding) is used for GSM8K/qualitative generation; greedy outputs can differ slightly from batch-size-1 runs. Use `--gen_batch_size 1` / `--batch_size 1` for strict determinism.
* Wall-clock times are not comparable across methods without accounting for the different LR (2e-5 vs 2e-4) and method overheads (all logged).

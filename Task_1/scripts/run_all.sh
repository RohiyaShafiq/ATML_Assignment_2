#!/usr/bin/env bash
# Full pipeline. Needs one CUDA GPU (>= 24 GB recommended) and internet for HF hub / datasets.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src

python src/data.py --stage all                      # probe set (before any results!) + training data
python src/evaluate.py --condition baseline         # shared baseline, evaluated once

for m in fullft l2sp lora4 lora16 oplora; do
  python src/train.py --method "$m"                 # add --resume artifacts/runs/$m/ckpt_update_XXXX to continue
done

python src/merge.py --check                         # merged vs unmerged logits (LoRA-4/16, OPLoRA)

for c in fullft_mid fullft_final l2sp_final lora4_final lora16_final oplora_final; do
  python src/evaluate.py --condition "$c"
done

python src/diagnostics.py
python src/qualitative.py --questions data/first_plot_questions.yaml
python src/analyze.py

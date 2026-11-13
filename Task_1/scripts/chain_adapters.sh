source /content/drive/MyDrive/task1/scripts/lib.sh
for m in lora4 lora16 oplora; do train_one $m; done
eval_one baseline
python src/merge.py --check 2>&1 | tee $D/logs/merge_check.log
for c in lora4_final lora16_final oplora_final; do eval_one $c; done
echo ALL_DONE_ADAPTERS

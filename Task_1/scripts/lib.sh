# helper functions used by the chains below
cd /content/drive/MyDrive/task1
A=/content/artifacts

train_one () {
  m=$1
  if [ -f $A/runs/$m/resources.json ]; then echo "[skip] train $m (finished)"; return; fi
  echo "=== train $m $(date)"
  python src/train.py --method $m 2>&1 | tee -a $D/logs/train_$m.log
  if [ -f $A/runs/$m/resources.json ]; then find $A/runs/$m/ -name trainer_state.pt -delete; fi
}

eval_one () {
  c=$1; shift
  if [ -f $A/results/$c/.done ]; then echo "[skip] eval $c (finished)"; return; fi
  echo "=== eval $c $(date)"
  python src/evaluate.py --condition $c "$@" 2>&1 | tee -a $D/logs/eval_$c.log
  if [ ${PIPESTATUS[0]} -eq 0 ]; then touch $A/results/$c/.done; fi
}

save_full () {   # save_full <method> <ckpt steps...>  : copy FP32 models to Drive
  m=$1; shift
  [ -f $A/runs/$m/resources.json ] || return
  if [ "$(readlink -f $A/runs/$m)" = "$(readlink -f $D/runs/$m)" ]; then return; fi
  mkdir -p $D/runs/$m
  for s in "$@"; do
    mkdir -p $D/runs/$m/ckpt_update_$s
    [ -d $D/runs/$m/ckpt_update_$s/model ] || cp -r $A/runs/$m/ckpt_update_$s/model $D/runs/$m/ckpt_update_$s/
  done
  cp $A/runs/$m/train_log.jsonl $D/runs/$m/
  cp $A/runs/$m/resources.json $D/runs/$m/
  echo "saved $m to Drive"
}

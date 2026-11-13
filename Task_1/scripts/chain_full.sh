source /content/drive/MyDrive/task1/scripts/lib.sh
train_one fullft
eval_one fullft_mid
eval_one fullft_final
save_full fullft 0625 1250
train_one l2sp
eval_one l2sp_final
save_full l2sp 1250
echo ALL_DONE_FULL

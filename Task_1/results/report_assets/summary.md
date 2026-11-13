## Benchmarks (primary metric, change vs baseline)

| Condition | GSM8K (flex EM) | HellaSwag (acc_norm) | BoolQ (acc) | ARC-Easy (acc_norm) |
|---|---|---|---|---|
| Baseline | 43.37 | 47.32 | 64.37 | 56.06 |
| Full FT (mid, 625) | 56.10 (+12.74 pp) | 45.66 (-1.66 pp) | 58.01 (-6.36 pp) | 62.58 (+6.52 pp) |
| Full FT (final) | 64.59 (+21.23 pp) | 45.82 (-1.50 pp) | 59.63 (-4.74 pp) | 63.72 (+7.66 pp) |
| Full FT + L2-SP | 65.13 (+21.76 pp) | 46.61 (-0.71 pp) | 63.00 (-1.38 pp) | 63.97 (+7.91 pp) |
| LoRA-4 | 59.06 (+15.69 pp) | 47.37 (+0.05 pp) | 69.82 (+5.44 pp) | 62.75 (+6.69 pp) |
| LoRA-16 | 63.00 (+19.64 pp) | 47.26 (-0.06 pp) | 71.25 (+6.88 pp) | 62.33 (+6.27 pp) |
| OPLoRA-16 | 62.02 (+18.65 pp) | 47.34 (+0.02 pp) | 71.47 (+7.09 pp) | 63.22 (+7.15 pp) |

## Raw / normalized / GSM8K extras

| Condition | hellaswag acc | hellaswag acc_norm | arc_easy acc | arc_easy acc_norm | boolq acc | gsm8k strict EM | gsm8k truncated % | gsm8k unextractable % | gsm8k mean tokens |
|---|---|---|---|---|---|---|---|---|---|
| Baseline | 37.56 | 47.32 | 60.90 | 56.06 | 64.37 | 0.00 | 0.83 | 0.00 | 226 |
| Full FT (mid, 625) | 37.03 | 45.66 | 65.61 | 62.58 | 58.01 | 0.00 | 1.14 | 0.00 | 145 |
| Full FT (final) | 37.29 | 45.82 | 66.67 | 63.72 | 59.63 | 0.00 | 0.53 | 0.00 | 138 |
| Full FT + L2-SP | 37.61 | 46.61 | 66.62 | 63.97 | 63.00 | 0.00 | 0.30 | 0.00 | 137 |
| LoRA-4 | 37.72 | 47.37 | 65.11 | 62.75 | 69.82 | 0.00 | 1.06 | 0.00 | 144 |
| LoRA-16 | 37.68 | 47.26 | 64.98 | 62.33 | 71.25 | 0.00 | 0.76 | 0.00 | 142 |
| OPLoRA-16 | 37.81 | 47.34 | 65.70 | 63.22 | 71.47 | 0.00 | 0.83 | 0.00 | 141 |

## Controlled comparisons (final checkpoints)

| Comparison | GSM8K (flex EM) diff (pp) | HellaSwag (acc_norm) diff (pp) | BoolQ (acc) diff (pp) | ARC-Easy (acc_norm) diff (pp) |
|---|---|---|---|---|
| Rank: LoRA-16 vs LoRA-4 | +3.94 | -0.11 | +1.44 | -0.42 |
| Regularization: L2-SP vs Full FT | +0.53 | +0.80 | +3.36 | +0.25 |
| Orthogonal protection: OPLoRA vs LoRA-16 | -0.99 | +0.08 | +0.21 | +0.88 |
| Full FT vs LoRA-16 | +1.59 | -1.44 | -11.62 | +1.39 |
| Progression: Full FT final vs mid | +8.49 | +0.16 | +1.62 | +1.14 |

## Resources

| Method | Trainable params | Train time (h) | Peak GPU mem (GiB) | Final model/adapter size (MiB) | Skipped updates | Method-specific overhead | ||θ-θ0||₂ final |
|---|---|---|---|---|---|---|---|
| Full FT | 596,049,920 | 3.84 | 13.08 | 2273.8 | 0 | θ0 storage 2274 MiB (cpu) | 10.641 |
| Full FT + L2-SP | 596,049,920 | 3.88 | 12.92 | 2273.8 | 0 | θ0 storage 2274 MiB (gpu); θ0 transfer 0.0s | 5.248 |
| LoRA-4 | 2,523,136 | 2.60 | 5.80 | 9.7 | 0 |  | nan |
| LoRA-16 | 10,092,544 | 2.62 | 5.35 | 38.6 | 0 |  | nan |
| OPLoRA-16 | 10,092,544 | 3.52 | 5.87 | 77.2 | 0 | bases 308 MiB; export 0.4s/39 MiB | nan |

## Full FT vs L2-SP

| Run | λ | mean SFT loss (last 50) | disp mid | disp final | final Ω | final λΩ |
|---|---|---|---|---|---|---|
| Full FT | 0.00 | 0.1840 | 10.003 | 10.641 | nan | nan |
| Full FT + L2-SP | 0.00 | 0.1844 | 5.589 | 5.248 | 2.7546e+01 | 2.7546e-02 |

## Diagnostics (mean over layers)

| condition | rho_k | eta | R_math | R_nonmath | ratio |
|---|---|---|---|---|---|
| Full FT (final) | 0.1324 | 0.0142 | 0.04202 | 0.02806 | 0.8277 |
| Full FT (mid, 625) | 0.1327 | 0.01368 | 0.04134 | 0.02761 | 0.822 |
| Full FT + L2-SP | 0.1297 | 0.009242 | 0.03212 | 0.02316 | 0.8371 |
| LoRA-16 | 0.1292 | 0.01464 | 0.0471 | 0.02228 | 0.8291 |
| LoRA-4 | 0.124 | 0.009176 | 0.04589 | 0.01401 | 0.6139 |
| OPLoRA-16 | 1.272e-09 | 0.01448 | 0.05442 | 0.02305 | 0.8063 |

## Merged vs unmerged logits

```

[
 {
  "method": "lora4",
  "max_abs_diff": 0.000148773193359375,
  "mean_abs_diff": 7.40786981623387e-06,
  "top1_agreement": 1.0,
  "passed": true
 },
 {
  "method": "lora16",
  "max_abs_diff": 0.00010466575622558594,
  "mean_abs_diff": 7.0028963818913326e-06,
  "top1_agreement": 1.0,
  "passed": true
 },
 {
  "method": "oplora",
  "max_abs_diff": 0.00012302398681640625,
  "mean_abs_diff": 7.0785890784463845e-06,
  "top1_agreement": 1.0,
  "passed": true
 }
]

```
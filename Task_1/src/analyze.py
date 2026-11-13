"""Build the tables and figures required for the report.

    python src/analyze.py
Outputs -> artifacts/report_assets/
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import (CONDITION_LABELS, CONDITIONS, DIAG_DIR, METHODS, REPORT_DIR, RESULTS_DIR, RUNS_DIR, read_json,
                    read_jsonl)

BENCH = {"gsm8k": "GSM8K (flex EM)", "hellaswag": "HellaSwag (acc_norm)", "boolq": "BoolQ (acc)", "arc_easy": "ARC-Easy (acc_norm)"}


def md_table(df: pd.DataFrame, floatfmt="{:.2f}") -> str:
    cols = list(df.columns)
    rows = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        rows.append("| " + " | ".join(floatfmt.format(v) if isinstance(v, float) else str(v) for v in r) + " |")
    return "\n".join(rows)


def load_metrics():
    out = {}
    for c in CONDITIONS:
        p = RESULTS_DIR / c / "metrics.json"
        if p.exists():
            out[c] = read_json(p)
    return out


def benchmark_table(M):
    base = M.get("baseline")
    rows, rows_raw = [], []
    for c, m in M.items():
        row = {"Condition": CONDITION_LABELS[c]}
        for b, name in BENCH.items():
            if b in m:
                v = 100 * m[b]["primary"]
                row[name] = f"{v:.2f}"
                if base and c != "baseline" and b in base:
                    row[name] += f" ({v - 100 * base[b]['primary']:+.2f} pp)"
        rows.append(row)
        raw = {"Condition": CONDITION_LABELS[c]}
        for b in ("hellaswag", "arc_easy"):
            if b in m:
                raw[f"{b} acc"] = f"{100*m[b].get('acc,none', float('nan')):.2f}"; raw[f"{b} acc_norm"] = f"{100*m[b].get('acc_norm,none', float('nan')):.2f}"
        if "boolq" in m:
            raw["boolq acc"] = f"{100*m['boolq'].get('acc,none', float('nan')):.2f}"
        if "gsm8k" in m:
            e = m["gsm8k"].get("extra", {})
            raw["gsm8k strict EM"] = f"{100*m['gsm8k'].get('exact_match,strict-match', float('nan')):.2f}"
            raw["gsm8k truncated %"] = f"{100*e.get('truncation_rate', float('nan')):.2f}"
            raw["gsm8k unextractable %"] = f"{100*e.get('unextractable_rate', float('nan')):.2f}"
            raw["gsm8k mean tokens"] = f"{e.get('mean_response_tokens', float('nan')):.0f}"
        rows_raw.append(raw)
    return pd.DataFrame(rows), pd.DataFrame(rows_raw)


def controlled(M):
    pairs = [("Rank: LoRA-16 vs LoRA-4", "lora16_final", "lora4_final"),
             ("Regularization: L2-SP vs Full FT", "l2sp_final", "fullft_final"),
             ("Orthogonal protection: OPLoRA vs LoRA-16", "oplora_final", "lora16_final"),
             ("Full FT vs LoRA-16", "fullft_final", "lora16_final"),
             ("Progression: Full FT final vs mid", "fullft_final", "fullft_mid")]
    rows = []
    for name, a, b in pairs:
        if a in M and b in M:
            r = {"Comparison": name}
            for k, lab in BENCH.items():
                if k in M[a] and k in M[b]:
                    r[lab + " diff (pp)"] = f"{100*(M[a][k]['primary']-M[b][k]['primary']):+.2f}"
            rows.append(r)
    return pd.DataFrame(rows)


def plot_losses():
    fig, ax = plt.subplots(1, 3, figsize=(17, 4.2))
    for m, cfg in METHODS.items():
        log = read_jsonl(RUNS_DIR / m / "train_log.jsonl")
        if not log:
            continue
        u = np.array([r["update"] for r in log]); l = np.array([r["sft_loss"] for r in log])
        sm = pd.Series(l).rolling(20, min_periods=1).mean()
        ax[0].plot(u, sm, label=cfg["label"]); ax[1].plot(u, [r["lr"] for r in log], label=cfg["label"])
    ax[0].set(xlabel="optimizer update", ylabel="SFT loss (20-update moving avg)", title="SFT loss"); ax[0].legend()
    ax[1].set(xlabel="optimizer update", ylabel="learning rate", title="LR schedule"); ax[1].set_yscale("log")
    log = read_jsonl(RUNS_DIR / "l2sp" / "train_log.jsonl")
    if log:
        u = [r["update"] for r in log]
        ax[2].plot(u, [r["sft_loss"] for r in log], label="SFT loss"); ax[2].plot(u, [r["lam_omega"] for r in log], label="λΩ")
        ax[2].plot(u, [r["total_loss"] for r in log], label="total"); ax[2].set_yscale("log"); ax[2].legend()
        ax[2].set(xlabel="optimizer update", title="L2-SP objective (λ=1e-3)")
    fig.tight_layout(); fig.savefig(REPORT_DIR / "fig_losses.png", dpi=160); plt.close(fig)


def resources_table():
    rows = []
    for m, cfg in METHODS.items():
        p = RUNS_DIR / m / "resources.json"
        if not p.exists():
            continue
        r = read_json(p); ck = r["checkpoints"].get(str(1250), {})
        row = {"Method": cfg["label"], "Trainable params": f"{r['trainable_params']:,}",
               "Train time (h)": f"{r['train_time_s']/3600:.2f}",
               "Peak GPU mem (GiB)": f"{(r['peak_gpu_mem_allocated_bytes'] or 0)/2**30:.2f}",
               "Final model/adapter size (MiB)": f"{ck.get('model_dir_bytes', 0)/2**20:.1f}",
               "Skipped updates": r["skipped_updates"]}
        oh = r.get("overhead", {})
        extra = []
        if "ref_storage_bytes" in oh: extra.append(f"θ0 storage {oh['ref_storage_bytes']/2**20:.0f} MiB ({oh['ref_on']})")
        if "ref_transfer_s" in oh: extra.append(f"θ0 transfer {oh['ref_transfer_s']:.1f}s")
        if "basis_storage_bytes" in oh: extra.append(f"bases {oh['basis_storage_bytes']/2**20:.0f} MiB")
        if isinstance(oh.get("basis_preprocess_s"), float): extra.append(f"SVD {oh['basis_preprocess_s']:.0f}s")
        if "export_time_s" in ck: extra.append(f"export {ck['export_time_s']:.1f}s/{ck['export_bytes']/2**20:.0f} MiB")
        row["Method-specific overhead"] = "; ".join(extra)
        if "displacement_l2" in ck: row["||θ-θ0||₂ final"] = f"{ck['displacement_l2']:.3f}"
        rows.append(row)
    return pd.DataFrame(rows)


def regularization_summary():
    rows = []
    for m in ("fullft", "l2sp"):
        p = RUNS_DIR / m / "resources.json"
        if not p.exists():
            continue
        r = read_json(p); log = read_jsonl(RUNS_DIR / m / "train_log.jsonl")
        row = {"Run": METHODS[m]["label"], "λ": METHODS[m]["l2sp"],
               "mean SFT loss (last 50)": f"{np.mean([x['sft_loss'] for x in log[-50:]]):.4f}",
               "disp mid": f"{r['checkpoints'].get('625', {}).get('displacement_l2', float('nan')):.3f}",
               "disp final": f"{r['checkpoints'].get('1250', {}).get('displacement_l2', float('nan')):.3f}"}
        if m == "l2sp" and log:
            row["final Ω"] = f"{log[-1]['omega']:.4e}"; row["final λΩ"] = f"{log[-1]['lam_omega']:.4e}"
        rows.append(row)
    return pd.DataFrame(rows)


def plot_benchmarks(M):
    base = M.get("baseline")
    if not base:
        return
    conds = [c for c in M if c != "baseline"]
    fig, ax = plt.subplots(figsize=(11, 4.2)); w = 0.8 / len(conds)
    for i, c in enumerate(conds):
        d = [100 * (M[c][b]["primary"] - base[b]["primary"]) if b in M[c] and b in base else np.nan for b in BENCH]
        ax.bar(np.arange(len(BENCH)) + i * w, d, w, label=CONDITION_LABELS[c])
    ax.axhline(0, color="k", lw=0.8); ax.set_xticks(np.arange(len(BENCH)) + 0.4 - w / 2); ax.set_xticklabels(BENCH.values())
    ax.set_ylabel("change vs baseline (pp)"); ax.legend(fontsize=8); fig.tight_layout()
    fig.savefig(REPORT_DIR / "fig_benchmarks.png", dpi=160); plt.close(fig)


def diagnostics_outputs():
    p = DIAG_DIR / "diagnostics_table.csv"
    if not p.exists():
        return None
    d = pd.read_csv(p)
    g = d.groupby("condition").agg(rho_k=("rho_k", "mean"), eta=("eta", "mean"), R_math=("math(gsm8k)", "mean"),
                                   R_nonmath=("non-math(hellaswag)", "mean"), ratio=("non_math_over_math", "mean")).reset_index()
    g["condition"] = g["condition"].map(CONDITION_LABELS)
    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    ax[0].bar(g["condition"], g["rho_k"]); ax[0].set_title("ρ_k (mean over layers 0/13/27)")
    ax[1].bar(g["condition"], g["eta"]); ax[1].set_title("η = ||ΔW||/||W0||"); ax[1].set_yscale("log")
    x = np.arange(len(g)); ax[2].bar(x - 0.2, g["R_math"], 0.4, label="math"); ax[2].bar(x + 0.2, g["R_nonmath"], 0.4, label="non-math")
    ax[2].set_xticks(x); ax[2].set_xticklabels(g["condition"]); ax[2].set_yscale("log"); ax[2].legend(); ax[2].set_title("mean R(z)")
    for a in ax:
        a.tick_params(axis="x", rotation=40)
    fig.tight_layout(); fig.savefig(REPORT_DIR / "fig_diagnostics.png", dpi=160); plt.close(fig)
    return g


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    M = load_metrics()
    out = []
    if M:
        t, raw = benchmark_table(M); c = controlled(M)
        t.to_csv(REPORT_DIR / "table_benchmarks.csv", index=False); raw.to_csv(REPORT_DIR / "table_raw_and_gsm8k_extras.csv", index=False)
        c.to_csv(REPORT_DIR / "table_controlled_comparisons.csv", index=False)
        out += ["## Benchmarks (primary metric, change vs baseline)", md_table(t), "## Raw / normalized / GSM8K extras", md_table(raw),
                "## Controlled comparisons (final checkpoints)", md_table(c)]
        plot_benchmarks(M)
    plot_losses()
    rt, rg = resources_table(), regularization_summary()
    rt.to_csv(REPORT_DIR / "table_resources.csv", index=False); rg.to_csv(REPORT_DIR / "table_regularization.csv", index=False)
    out += ["## Resources", md_table(rt), "## Full FT vs L2-SP", md_table(rg)]
    g = diagnostics_outputs()
    if g is not None:
        g.to_csv(REPORT_DIR / "table_diagnostics.csv", index=False); out += ["## Diagnostics (mean over layers)", md_table(g, "{:.4g}")]
    mc = RESULTS_DIR / "merge_checks.json"
    if mc.exists():
        out += ["## Merged vs unmerged logits", "```", json.dumps(read_json(mc), indent=1), "```"]
    (REPORT_DIR / "summary.md").write_text("\n\n".join(out))
    print("\n\n".join(out))


if __name__ == "__main__":
    main()

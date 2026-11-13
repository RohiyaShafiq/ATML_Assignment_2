"""One training run (one of the five variants).

    python src/train.py --method fullft|l2sp|lora4|lora16|oplora [--resume <ckpt_dir>]
    python src/train.py --method oplora --tiny          # CPU smoke test with a random tiny Qwen3
"""
from __future__ import annotations

import argparse
import math
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

import adapters as AD
from common import (ADAM_BETAS, ADAM_EPS, CKPT_UPDATES, EFF_BS, GRAD_ACC, GRAD_CLIP, LORA_DROPOUT, METHODS,
                    MICRO_BS, RUNS_DIR, SEED, TOTAL_UPDATES, TRAIN_DATA_PATH, WARMUP_UPDATES, WEIGHT_DECAY,
                    append_jsonl, cast_params_, ckpt_dir, dir_size, load_base_model, read_jsonl, run_dir,
                    set_seed, write_json)


# --------------------------------------------------------------------------- #
def parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=list(METHODS), required=True)
    ap.add_argument("--out_root", default=str(RUNS_DIR))
    ap.add_argument("--data_path", default=str(TRAIN_DATA_PATH))
    ap.add_argument("--resume", default=None, help="checkpoint dir (ckpt_update_XXXX) to resume from")
    ap.add_argument("--max_updates", type=int, default=None, help="stop early (schedule unchanged)")
    ap.add_argument("--tiny", action="store_true", help="CPU smoke test: tiny random model + synthetic data")
    return ap.parse_args()


def synthetic_data(n):
    g = torch.Generator().manual_seed(SEED)
    ids, pl = [], []
    for _ in range(n):
        L = int(torch.randint(12, 40, (1,), generator=g)); p = int(torch.randint(3, 8, (1,), generator=g))
        ids.append(torch.randint(1, 126, (L,), generator=g).tolist() + [127]); pl.append(p)
    return {"input_ids": ids, "prompt_len": pl, "pad_id": 0, "eos_id": 127}


def collate(data, idxs, pad_id):
    L = max(len(data["input_ids"][i]) for i in idxs)
    ids = torch.full((len(idxs), L), pad_id, dtype=torch.long)
    mask = torch.zeros((len(idxs), L), dtype=torch.long)
    labels = torch.full((len(idxs), L), -100, dtype=torch.long)
    for j, i in enumerate(idxs):
        x, p = data["input_ids"][i], data["prompt_len"][i]
        t = torch.tensor(x)
        ids[j, :len(x)] = t; mask[j, :len(x)] = 1
        labels[j, p:len(x)] = t[p:]                   # response tokens + end-of-turn token only
    return ids, mask, labels


def ce_sum(logits, labels):
    """Sum of next-token CE over valid (response) positions."""
    sl, lab = logits[:, :-1], labels[:, 1:]
    m = lab != -100
    return F.cross_entropy(sl[m].float(), lab[m], reduction="sum")


@torch.no_grad()
def displacement(params, ref, device):
    tot = torch.zeros((), dtype=torch.float64, device=device)
    for p, r in zip(params, ref):
        tot += ((p.detach().float() - r.to(device).float()) ** 2).sum().double()
    return tot.sqrt().item()


def rng_state():
    return {"torch": torch.get_rng_state(), "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None,
            "py": random.getstate(), "np": np.random.get_state()}


def set_rng_state(s):
    torch.set_rng_state(s["torch"])
    if s["cuda"] is not None and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(s["cuda"])
    random.setstate(s["py"]); np.random.set_state(s["np"])


# --------------------------------------------------------------------------- #
def main():
    args = parse()
    cfg = dict(METHODS[args.method])
    tiny = args.tiny
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    use_amp = device.type == "cuda"
    total, warmup, ckpts = (TOTAL_UPDATES, WARMUP_UPDATES, list(CKPT_UPDATES))
    if tiny:
        total, warmup, ckpts = 8, 2, [2, 4, 6, 8]
        if cfg["kind"] == "oplora":
            cfg["k"] = 8
    out_root = Path(args.out_root)
    rdir = run_dir(args.method, out_root); rdir.mkdir(parents=True, exist_ok=True)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats()
    set_seed(SEED)

    data = synthetic_data(total * EFF_BS) if tiny else torch.load(args.data_path, weights_only=False)
    assert len(data["input_ids"]) >= total * EFF_BS
    pad_id = data["pad_id"]

    resume = Path(args.resume) if args.resume else None
    rstate = torch.load(resume / "trainer_state.pt", weights_only=False, map_location="cpu") if resume else None
    start = rstate["step"] if rstate else 0

    # ---------------------------------------------------------------- model
    overhead, kind = {}, cfg["kind"]
    if kind == "full":
        if resume:
            from transformers import AutoModelForCausalLM
            model = AutoModelForCausalLM.from_pretrained(resume / "model", torch_dtype=torch.float32)
        else:
            model = load_base_model(torch.float32, tiny)
        model.to(device)
    else:
        model = load_base_model(torch.float32, tiny).to(device)
        bases = None
        if kind == "oplora":
            bp = rdir / "oplora_bases.pt"
            if bp.exists():
                bases = {n: (u.to(device), v.to(device)) for n, (u, v) in torch.load(bp, map_location="cpu").items()}
                overhead["basis_preprocess_s"] = "loaded_from_disk"
            else:
                t0 = time.time(); bases = AD.compute_bases(model, cfg["k"])
                if device.type == "cuda": torch.cuda.synchronize()
                overhead["basis_preprocess_s"] = time.time() - t0
                torch.save({n: (u.cpu(), v.cpu()) for n, (u, v) in bases.items()}, bp)
            overhead["basis_storage_bytes"] = AD.bases_nbytes(bases)
        cast_params_(model, torch.float16 if use_amp else torch.float32)     # frozen backbone
        for p in model.parameters():
            p.requires_grad_(False)
        AD.inject_adapters(model, cfg["r"], cfg["alpha"], LORA_DROPOUT, SEED, bases)  # FP32 adapters
        if resume:
            AD.load_adapter_state(model, torch.load(resume / "model" / "adapter.pt", weights_only=False)["adapters"])
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()

    named = [(n, p) for n, p in model.named_parameters() if p.requires_grad]   # tied params counted once
    params = [p for _, p in named]
    n_trainable = sum(p.numel() for p in params)
    n_total = sum(p.numel() for p in model.parameters())
    print(f"[{args.method}] trainable={n_trainable:,} total={n_total:,} device={device}")

    # ------------------------------------------------------------ optimizer
    decay = [p for n, p in named if p.ndim >= 2 and not n.endswith(".bias")]
    no_decay = [p for n, p in named if not (p.ndim >= 2 and not n.endswith(".bias"))]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": WEIGHT_DECAY},
                             {"params": no_decay, "weight_decay": 0.0}],
                            lr=cfg["lr"], betas=ADAM_BETAS, eps=ADAM_EPS)

    def lr_lambda(step):                                   # step = completed updates
        if step < warmup:
            return (step + 1) / warmup
        prog = (step - warmup) / max(1, total - warmup)
        return 0.5 * (1.0 + math.cos(math.pi * prog))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)
    scaler = torch.amp.GradScaler(device.type if use_amp else "cpu", enabled=use_amp)

    lam = cfg.get("l2sp", 0.0)
    ref = None
    if kind == "full":
        t0 = time.time()
        if lam > 0:        # theta_0 kept on GPU (needed every update)
            ref = [p.detach().clone() for p in params]
            if device.type == "cuda": torch.cuda.synchronize()
            overhead["ref_transfer_s"] = time.time() - t0
        else:              # only needed for displacement at checkpoints -> keep on CPU
            ref = [p.detach().cpu().clone() for p in params]
        overhead["ref_storage_bytes"] = sum(r.numel() * r.element_size() for r in ref)
        overhead["ref_on"] = "gpu" if lam > 0 else "cpu"
        if resume:         # theta_0 must be the ORIGINAL checkpoint, not the resumed weights
            base0 = load_base_model(torch.float32, tiny)
            b0 = dict(base0.named_parameters())
            ref = [b0[n].detach().to(ref[0].device if lam > 0 else "cpu").clone() for n, _ in named]
            del base0

    if resume:
        opt.load_state_dict(rstate["optimizer"]); sched.load_state_dict(rstate["scheduler"])
        scaler.load_state_dict(rstate["scaler"]); set_rng_state(rstate["rng"])
    else:
        set_seed(SEED)

    log_path = rdir / "train_log.jsonl"
    if resume:
        kept = [r for r in read_jsonl(log_path) if r["update"] <= start]
        log_path.write_text("".join(__import__("json").dumps(r) + "\n" for r in kept))
    else:
        log_path.write_text("")
    ckpt_meta = rstate.get("ckpt_meta", {}) if rstate else {}
    train_time = rstate.get("train_time_s", 0.0) if rstate else 0.0
    skipped_total = rstate.get("skipped_total", 0) if rstate else 0

    # ------------------------------------------------------------- training
    stop_at = min(total, args.max_updates) if args.max_updates else total
    for step in range(start, stop_at):
        t_up = time.time()
        lo = step * EFF_BS
        micro = [collate(data, list(range(lo + i, lo + i + MICRO_BS)), pad_id) for i in range(0, EFF_BS, MICRO_BS)]
        n_tok = sum(int((lab[:, 1:] != -100).sum()) for _, _, lab in micro)      # valid tokens, effective batch
        opt.zero_grad(set_to_none=True)
        ce_total = torch.zeros((), device=device, dtype=torch.float64)
        for ids, mask, lab in micro:
            ids, mask, lab = ids.to(device), mask.to(device), lab.to(device)
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
                logits = model(input_ids=ids, attention_mask=mask).logits
            ce = ce_sum(logits, lab)
            scaler.scale(ce / n_tok).backward()
            ce_total += ce.detach().double()
        sft_loss = ce_total.item() / n_tok

        rec = {"update": step + 1, "examples": (step + 1) * EFF_BS, "lr": opt.param_groups[0]["lr"],
               "sft_loss": sft_loss, "valid_tokens": n_tok}
        if lam > 0:
            omega = sum(((p - r) ** 2).sum() for p, r in zip(params, ref))      # sum over unique params, not averaged
            pen = lam * omega
            scaler.scale(pen).backward()
            rec.update(omega=omega.item(), lam_omega=pen.item(), total_loss=sft_loss + pen.item())

        scaler.unscale_(opt)
        gn = torch.nn.utils.clip_grad_norm_(params, GRAD_CLIP)
        prev_scale = scaler.get_scale()
        scaler.step(opt); scaler.update()
        skipped = bool((scaler.get_scale() < prev_scale) or (not torch.isfinite(gn)))
        sched.step()
        skipped_total += int(skipped)
        if device.type == "cuda": torch.cuda.synchronize()
        dt = time.time() - t_up
        train_time += dt
        rec.update(grad_norm=float(gn), loss_scale=scaler.get_scale(), skipped=skipped, step_time_s=dt)
        append_jsonl(log_path, rec)
        if (step + 1) % 10 == 0 or step == start:
            print(f"[{args.method}] upd {step+1}/{total} loss {sft_loss:.4f} lr {rec['lr']:.2e} gn {float(gn):.2f}"
                  + (f" omega {rec['omega']:.3e}" if lam > 0 else "") + (" SKIPPED" if skipped else ""), flush=True)

        # ----------------------------------------------------- checkpointing
        if (step + 1) in ckpts:
            cd = ckpt_dir(args.method, step + 1, out_root); (cd / "model").mkdir(parents=True, exist_ok=True)
            meta = {"update": step + 1, "examples_seen": (step + 1) * EFF_BS}
            t_ck = time.time()
            if kind == "full":
                model.save_pretrained(cd / "model", safe_serialization=True)       # FP32 weights
                meta["displacement_l2"] = displacement(params, ref, device)
            else:
                torch.save({"method": args.method, "r": cfg["r"], "alpha": cfg["alpha"],
                            "adapters": AD.adapter_state(model)}, cd / "model" / "adapter.pt")
                if kind == "oplora":
                    t_ex = time.time()
                    proj, chk = AD.export_projected(model)
                    torch.save({"method": args.method, "r": cfg["r"], "alpha": cfg["alpha"], "adapters": proj},
                               cd / "model" / "adapter_projected.pt")
                    meta["export_time_s"] = time.time() - t_ex
                    meta["export_bytes"] = dir_size(cd / "model" / "adapter_projected.pt")
                    meta["constraint_check"] = chk
            torch.save({"step": step + 1, "optimizer": opt.state_dict(), "scheduler": sched.state_dict(),
                        "scaler": scaler.state_dict(), "rng": rng_state(), "examples_seen": (step + 1) * EFF_BS,
                        "data_position": (step + 1) * EFF_BS, "train_time_s": train_time,
                        "skipped_total": skipped_total, "ckpt_meta": ckpt_meta}, cd / "trainer_state.pt")
            meta["model_dir_bytes"] = dir_size(cd / "model")
            meta["trainer_state_bytes"] = dir_size(cd / "trainer_state.pt")
            meta["save_time_s"] = time.time() - t_ck
            ckpt_meta[str(step + 1)] = meta
            print(f"[{args.method}] checkpoint {step+1}: {meta}", flush=True)

    res = {"method": args.method, "label": cfg["label"], "lr": cfg["lr"], "l2sp_lambda": lam,
           "trainable_params": n_trainable, "total_params_in_model": n_total,
           "train_time_s": train_time, "updates_done": stop_at, "skipped_updates": skipped_total,
           "peak_gpu_mem_allocated_bytes": torch.cuda.max_memory_allocated() if device.type == "cuda" else None,
           "peak_gpu_mem_reserved_bytes": torch.cuda.max_memory_reserved() if device.type == "cuda" else None,
           "overhead": overhead, "checkpoints": ckpt_meta, "device": str(device), "amp_fp16": use_amp}
    write_json(rdir / "resources.json", res)
    print("done:", {k: v for k, v in res.items() if k != "checkpoints"})


if __name__ == "__main__":
    main()

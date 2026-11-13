#!/usr/bin/env python3
"""Resumable Task-1 pipeline (Colab).

Everything that matters lives on Google Drive:
  <D>/runs/<method>/...      checkpoints, train_log.jsonl, resources.json
  <D>/results/<cond>/.done   finished evaluations
  <D>/state/pipeline_state.json   ledger: every stage, every attempt, resumed-from, exit codes, times
  <D>/logs/                  pipeline.log + one append-only log per training / evaluation

Re-running this script after ANY runtime crash is safe:
  * finished stages are detected from real artifacts on Drive and skipped
  * an interrupted training run continues from its newest *valid* checkpoint (never restarts)
  * an interrupted evaluation is simply redone (it is short)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROJ = Path(os.environ.get("T1_PROJ", "/content/drive/MyDrive/task1"))
D = Path(os.environ.get("D", "/content/drive/MyDrive/task1_artifacts"))
A = Path(os.environ.get("T1_ARTIFACTS", "/content/artifacts"))
LOGS, STATE_DIR = D / "logs", D / "state"
STATE_FILE = STATE_DIR / "pipeline_state.json"
MARKERS = STATE_DIR / "done"

FULL = {"fullft", "l2sp"}                      # full-parameter runs: big checkpoints, local disk + synced to Drive
KEEP = {"fullft": [625, 1250], "l2sp": [1250]}  # full-model checkpoints that must stay on Drive (evals/diagnostics)
FINAL = 1250
MAX_RETRIES = int(os.environ.get("T1_MAX_RETRIES", "2"))   # automatic retries of a failed stage (resume, not restart)
SYNC_EVERY_S = int(os.environ.get("T1_SYNC_EVERY", "30"))
PY = sys.executable
SESSION = datetime.now().strftime("%Y%m%d-%H%M%S")

ENV = dict(os.environ, PYTHONPATH=str(PROJ / "src"), T1_ARTIFACTS=str(A), D=str(D),
           PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True", PYTHONUNBUFFERED="1")


# ------------------------------------------------------------------ logging / state
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGS / "pipeline.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def update_state(key, history=None, **kw):
    st = load_state()
    s = st.setdefault("stages", {}).setdefault(key, {})
    s.update(kw)
    if history:
        s.setdefault("history", []).append(history)
    st["last_update"] = now()
    atomic_write(STATE_FILE, json.dumps(st, indent=2))


def set_pipeline(**kw):
    st = load_state()
    st.setdefault("pipeline", {}).update(kw)
    st["last_update"] = now()
    atomic_write(STATE_FILE, json.dumps(st, indent=2))


# ------------------------------------------------------------------ file validity
def zip_ok(p: Path):
    """torch.save files are zip archives; a half-written one has no central directory."""
    try:
        return p.is_file() and p.stat().st_size > 0 and zipfile.is_zipfile(p)
    except Exception:
        return False


def model_ok(model_dir: Path):
    if (model_dir / "adapter.pt").exists():
        return zip_ok(model_dir / "adapter.pt")
    return any(model_dir.glob("model*.safetensors"))


def ckpt_valid(c: Path):
    """trainer_state.pt is written LAST by train.py, so a readable one means the whole checkpoint is complete."""
    return zip_ok(c / "trainer_state.pt") and model_ok(c / "model")


def list_ckpts(root: Path):
    out = []
    if root.is_dir():
        for p in root.iterdir():
            m = re.fullmatch(r"ckpt_update_(\d+)", p.name)
            if m and p.is_dir():
                out.append((int(m.group(1)), p))
    return sorted(out)


def latest_valid(root: Path):
    for step, p in reversed(list_ckpts(root)):
        if ckpt_valid(p):
            return step, p
    return None


def copy_file_atomic(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def copy_dir_atomic(src: Path, dst: Path):
    tmp = dst.with_name(dst.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp)
    if dst.exists():
        shutil.rmtree(dst)
    os.rename(tmp, dst)


# ------------------------------------------------------------------ directory layout
def link(local: Path, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    if local.is_symlink():
        if os.path.realpath(local) == os.path.realpath(target):
            return
        local.unlink()
    elif local.exists():                        # a real dir from an older session: merge into Drive, then link
        shutil.copytree(local, target, dirs_exist_ok=True)
        shutil.rmtree(local)
    local.symlink_to(target)


def setup_dirs():
    for p in (LOGS, STATE_DIR, MARKERS):
        p.mkdir(parents=True, exist_ok=True)
    (A / "runs").mkdir(parents=True, exist_ok=True)
    for d in ("data", "results", "diagnostics", "qualitative", "report_assets"):
        link(A / d, D / d)
    for m in ("lora4", "lora16", "oplora"):    # small runs: written straight to Drive
        link(A / "runs" / m, D / "runs" / m)
    for m in FULL:
        (D / "runs" / m).mkdir(parents=True, exist_ok=True)


def ensure_view(m):
    """Make runs/<m> reachable locally for evaluation / diagnostics (full runs live on Drive once finished)."""
    loc = A / "runs" / m
    if loc.exists() or loc.is_symlink():
        return
    if m in FULL and train_done(m):
        loc.symlink_to(D / "runs" / m)


# ------------------------------------------------------------------ truth: what is finished?
def train_done(m):
    if (D / "runs" / m / "resources.json").exists():
        return True
    if m not in FULL:                           # final adapter exists, trainer state gone (old chain deleted it)
        fin = D / "runs" / m / f"ckpt_update_{FINAL:04d}"
        if model_ok(fin / "model") and not ckpt_valid(fin):
            return True
    return False


def eval_done(c):
    return (A / "results" / c / ".done").exists()


def marker_done(key):
    return (MARKERS / key).exists()


def data_done():
    return zip_ok(D / "data" / "train_tokenized.pt") and (D / "data" / "probe_set.json").exists() \
        and (D / "data" / "train_meta.json").exists()


# ------------------------------------------------------------------ running subprocesses with Drive logs
def run_cmd(cmd, logname, header):
    logf = LOGS / logname
    with open(logf, "ab") as lf:
        lf.write(f"\n\n===== [{now()}] session {SESSION} | {header} =====\n".encode())
        lf.flush()
        p = subprocess.Popen(cmd, cwd=PROJ, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            while True:
                chunk = p.stdout.read1(4096)
                if not chunk:
                    break
                lf.write(chunk); lf.flush()
                sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
            rc = p.wait()
        finally:
            if p.poll() is None:
                p.terminate()
        lf.write(f"\n===== [{now()}] exit code {rc} =====\n".encode())
    return rc


# ------------------------------------------------------------------ full-FT: local disk <-> Drive sync
def sync_full(m):
    """Copy new valid checkpoints local -> Drive (log first, then ckpts, each atomically); prune old state."""
    src, dst = A / "runs" / m, D / "runs" / m
    if not src.is_dir() or src.is_symlink():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for t in dst.glob("*.tmp"):
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
    if (src / "train_log.jsonl").exists():
        copy_file_atomic(src / "train_log.jsonl", dst / "train_log.jsonl")
    for step, c in list_ckpts(src):
        tgt = dst / c.name
        if (tgt / ".synced").exists() or not ckpt_valid(c):
            continue
        t0 = time.time()
        copy_dir_atomic(c, tgt)
        (tgt / ".synced").write_text(now())
        log(f"[sync] {m} update {step} -> Drive ({time.time() - t0:.0f}s)")
    synced = [s for s, p in list_ckpts(dst) if (p / ".synced").exists()]
    if not synced:
        return
    newest = max(synced)
    for root in (dst, src):                      # prune: only the newest ckpt keeps trainer state; keep big models only if needed
        for step, p in list_ckpts(root):
            if step >= newest:
                continue
            if root is src and step not in synced:
                continue
            ts = p / "trainer_state.pt"
            if ts.exists():
                ts.unlink()
            if step not in KEEP[m]:
                shutil.rmtree(p, ignore_errors=True)


class Syncer(threading.Thread):
    def __init__(self, m):
        super().__init__(daemon=True)
        self.m, self.ev = m, threading.Event()

    def run(self):
        while not self.ev.wait(SYNC_EVERY_S):
            try:
                sync_full(self.m)
            except Exception as e:
                log(f"[sync] {self.m} warning: {e!r}")

    def stop(self):
        self.ev.set()
        self.join()
        try:
            sync_full(self.m)
        except Exception as e:
            log(f"[sync] {self.m} final warning: {e!r}")


def prepare_resume(m):
    """Return (step, ckpt_path) to resume from, or None for a fresh start."""
    loc, drv = A / "runs" / m, D / "runs" / m
    if m not in FULL:
        return latest_valid(drv)
    if loc.is_symlink():
        loc.unlink()
    loc.mkdir(parents=True, exist_ok=True)
    l, d = latest_valid(loc), latest_valid(drv)
    if d and (not l or d[0] > l[0]):
        log(f"restoring {m} update {d[0]} from Drive to local disk ...")
        if (drv / "train_log.jsonl").exists():
            copy_file_atomic(drv / "train_log.jsonl", loc / "train_log.jsonl")
        copy_dir_atomic(d[1], loc / d[1].name)
        l = (d[0], loc / d[1].name)
    return l


def finalize_full(m):
    sync_full(m)
    copy_file_atomic(A / "runs" / m / "resources.json", D / "runs" / m / "resources.json")   # = "done" marker
    for root in (D / "runs" / m, A / "runs" / m):                                           # optimizer state no longer needed
        ts = root / f"ckpt_update_{FINAL:04d}" / "trainer_state.pt"
        if ts.exists():
            ts.unlink()


# ------------------------------------------------------------------ stages
def train_run(m):
    key = f"train_{m}"
    full = m in FULL
    if train_done(m):
        if not (D / "runs" / m / "resources.json").exists():
            log(f"WARNING {m}: final adapter found but resources.json missing -> treated as trained "
                f"(resource table will lack this row)")
        ensure_view(m)
        return True
    for attempt in range(1, MAX_RETRIES + 2):
        res = prepare_resume(m)
        cmd = [PY, "-u", "src/train.py", "--method", m] + (["--resume", str(res[1])] if res else [])
        how = f"RESUME from update {res[0]}" if res else "FRESH start"
        header = f"train {m} | attempt {attempt} | {how}"
        log(header)
        t0 = time.time()
        update_state(key, status="running", resumed_from=res[0] if res else 0)
        syncer = Syncer(m) if full else None
        if syncer:
            syncer.start()
        try:
            rc = run_cmd(cmd, f"train_{m}.log", header)
        finally:
            if syncer:
                syncer.stop()
        update_state(key, history={"session": SESSION, "start": now(), "how": how, "rc": rc,
                                   "seconds": round(time.time() - t0)})
        if rc == 0 and (A / "runs" / m / "resources.json").exists():
            if full:
                finalize_full(m)
            log(f"train {m} finished")
            return True
        log(f"train {m} failed (rc={rc}); newest valid checkpoint will be used for the retry")
        time.sleep(5)
    return False


def eval_run(c, method=None, extra=()):
    key = f"eval_{c}"
    if method:
        ensure_view(method)
    (A / "results" / c).mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_RETRIES + 2):
        header = f"eval {c} | attempt {attempt}"
        log(header)
        t0 = time.time()
        update_state(key, status="running")
        rc = run_cmd([PY, "-u", "src/evaluate.py", "--condition", c, *extra], f"eval_{c}.log", header)
        update_state(key, history={"session": SESSION, "start": now(), "rc": rc, "seconds": round(time.time() - t0)})
        if rc == 0:
            (A / "results" / c / ".done").write_text(now())
            return True
        time.sleep(5)
    return False


def cmd_run(key, script_args, logname, done_check=None, blocking=True):
    for attempt in range(1, MAX_RETRIES + 2):
        header = f"{key} | attempt {attempt}"
        log(header)
        t0 = time.time()
        update_state(key, status="running")
        rc = run_cmd([PY, "-u", *script_args], logname, header)
        update_state(key, history={"session": SESSION, "start": now(), "rc": rc, "seconds": round(time.time() - t0)})
        if rc == 0:
            (MARKERS / key).write_text(now())
            return True
    if not blocking:
        log(f"WARNING: {key} failed but is non-blocking")
        (MARKERS / key).write_text("failed-nonblocking " + now())
        return True
    return False


def build_stages():
    S = []                                         # (key, done_fn, run_fn)

    def cmd(key, args, logname, done=None, blocking=True):
        S.append((key, done or (lambda k=key: marker_done(k)),
                  lambda: cmd_run(key, args, logname, blocking=blocking)))

    def train(m):
        S.append((f"train_{m}", lambda: train_done(m), lambda: train_run(m)))

    def ev(c, m=None):
        S.append((f"eval_{c}", lambda: eval_done(c), lambda: eval_run(c, m)))

    cmd("data", ["src/data.py", "--stage", "all"], "data.log", done=data_done)
    cmd("tests", ["tests/test_core.py"], "tests.log", blocking=False)
    ev("baseline")
    for m in ("lora4", "lora16", "oplora"):
        train(m)
        if m == "oplora":
            cmd("merge_check", ["src/merge.py", "--check"], "merge_check.log",
                done=lambda: marker_done("merge_check") and (A / "results" / "merge_checks.json").exists())
        ev(f"{m}_final", m)
    train("fullft"); ev("fullft_mid", "fullft"); ev("fullft_final", "fullft")
    train("l2sp"); ev("l2sp_final", "l2sp")
    cmd("diagnostics", ["src/diagnostics.py"], "diagnostics.log")
    cmd("qualitative", ["src/qualitative.py", "--questions", "data/first_plot_questions.yaml"], "qualitative.log")
    cmd("analyze", ["src/analyze.py"], "analyze.log")
    return S


# ------------------------------------------------------------------ entry points
def status():
    setup_dirs()
    st = load_state().get("stages", {})
    print(f"{'stage':22} {'state':10} detail")
    print("-" * 70)
    for key, done, _ in build_stages():
        d = done()
        detail = ""
        if key.startswith("train_"):
            m = key[6:]
            v = latest_valid(D / "runs" / m) or (latest_valid(A / "runs" / m) if m in FULL else None)
            if not d and v:
                detail = f"resumable from update {v[0]}/{FINAL}"
            n = len(st.get(key, {}).get("history", []))
            detail += f"  (attempts logged: {n})" if n else ""
        elif key in st and st[key].get("history"):
            detail = f"attempts logged: {len(st[key]['history'])}"
        print(f"{key:22} {'DONE' if d else 'pending':10} {detail}")
    p = load_state().get("pipeline", {})
    if p:
        print("\npipeline:", p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--only", default=None, help="comma list of stage keys (default: all)")
    args = ap.parse_args()
    if args.status:
        status(); return
    setup_dirs()
    stages = build_stages()
    if args.only:
        want = set(args.only.split(","))
        stages = [s for s in stages if s[0] in want]
    try:
        import torch
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "NO GPU"
    except Exception:
        gpu = "unknown"
    log(f"=== pipeline session {SESSION} started | GPU: {gpu}")
    set_pipeline(status="running", session=SESSION, started=now(), gpu=gpu)
    for key, done, run in stages:
        if done():
            log(f"[skip] {key} (already finished)")
            if key.startswith("train_"):
                ensure_view(key[6:])
            continue
        update_state(key, status="running", last_session=SESSION)
        ok = run()
        if not ok:
            update_state(key, status="FAILED")
            set_pipeline(status=f"FAILED at {key}", ended=now())
            log(f"!!! {key} FAILED after retries - fix the cause, then just start the pipeline again")
            sys.exit(1)
        update_state(key, status="done", finished=now())
    set_pipeline(status="ALL_DONE", ended=now())
    log("ALL_DONE")


if __name__ == "__main__":
    main()

"""Own LoRA / OPLoRA implementation (so LoRA-16 and OPLoRA share *identical* initial factors).

OPLoRA effective factors (computed in FP32, never as dense projection matrices):
    B~ = B - U_k (U_k^T B),   A~ = A - (A V_k) V_k^T,   dW = (alpha/r) B~ A~
"""
from __future__ import annotations

import math
import zlib

import torch
import torch.nn as nn
import torch.nn.functional as F
from common import TARGET_MODULES


def target_linear_names(model) -> list[str]:
    return [n for n, m in model.named_modules()
            if isinstance(m, nn.Linear) and n.rsplit(".", 1)[-1] in TARGET_MODULES and ".layers." in n]


class LoRALinear(nn.Module):
    def __init__(self, base: nn.Linear, r: int, alpha: float, dropout: float, name: str, seed: int,
                 Uk: torch.Tensor | None = None, Vk: torch.Tensor | None = None):
        super().__init__()
        self.base, self.r, self.scale, self.name = base, r, alpha / r, name
        in_f, out_f = base.in_features, base.out_features
        # deterministic per-module init -> identical for LoRA-16 and OPLoRA-16
        g = torch.Generator().manual_seed((seed + zlib.crc32(name.encode())) % (2 ** 31))
        bound = 1.0 / math.sqrt(in_f)                    # kaiming_uniform(a=sqrt(5))
        self.A = nn.Parameter(torch.empty(r, in_f, dtype=torch.float32).uniform_(-bound, bound, generator=g))
        self.B = nn.Parameter(torch.zeros(out_f, r, dtype=torch.float32))
        self.dropout = nn.Dropout(dropout)
        self.register_buffer("Uk", None if Uk is None else Uk.float(), persistent=False)
        self.register_buffer("Vk", None if Vk is None else Vk.float(), persistent=False)

    def effective_factors(self):
        A, B = self.A, self.B
        if self.Uk is not None:
            with torch.autocast(device_type=A.device.type, enabled=False):
                B = B - self.Uk @ (self.Uk.transpose(0, 1) @ B)
                A = A - (A @ self.Vk) @ self.Vk.transpose(0, 1)
        return A, B

    def forward(self, x):
        out = self.base(x)
        A, B = self.effective_factors()
        h = self.dropout(x)
        lo = F.linear(F.linear(h, A.to(h.dtype)), B.to(h.dtype))
        return out + (self.scale * lo).to(out.dtype)


def inject_adapters(model, r, alpha, dropout, seed, bases=None) -> dict:
    """Replace each target nn.Linear by a LoRALinear. `bases`: name -> (Uk, Vk) for OPLoRA."""
    wrapped = {}
    for name in target_linear_names(model):
        parent_name, _, child = name.rpartition(".")
        parent = model.get_submodule(parent_name)
        base = getattr(parent, child)
        Uk, Vk = bases[name] if bases is not None else (None, None)
        lora = LoRALinear(base, r, alpha, dropout, name, seed, Uk, Vk)
        lora.to(base.weight.device)                      # moves A,B,bases only (dtype untouched)
        setattr(parent, child, lora)
        wrapped[name] = lora
    return wrapped


def iter_lora(model):
    for n, m in model.named_modules():
        if isinstance(m, LoRALinear):
            yield n, m


@torch.no_grad()
def compute_bases(model, k: int) -> dict:
    """Leading-k left/right singular bases of every target W0, computed once in FP32."""
    bases = {}
    for name in target_linear_names(model):
        W = model.get_submodule(name).weight.detach().float()
        U, S, Vh = torch.linalg.svd(W, full_matrices=False)
        kk = min(k, S.numel())
        bases[name] = (U[:, :kk].contiguous(), Vh[:kk].transpose(0, 1).contiguous())
    return bases


def bases_nbytes(bases) -> int:
    return sum(u.numel() * u.element_size() + v.numel() * v.element_size() for u, v in bases.values())


def adapter_state(model) -> dict:
    return {n: {"A": m.A.detach().cpu().clone(), "B": m.B.detach().cpu().clone()} for n, m in iter_lora(model)}


@torch.no_grad()
def load_adapter_state(model, state: dict) -> None:
    for n, m in iter_lora(model):
        m.A.copy_(state[n]["A"]); m.B.copy_(state[n]["B"])


@torch.no_grad()
def export_projected(model):
    """Projected (effective) factors for inference + numerical check of the OPLoRA constraints
    U_k^T dW = 0 and dW V_k = 0 (reported relative to ||dW||_F; max over modules)."""
    out, left, right = {}, 0.0, 0.0
    for n, m in iter_lora(model):
        A, B = m.effective_factors()
        out[n] = {"A": A.detach().cpu().clone(), "B": B.detach().cpu().clone()}
        if m.Uk is not None:
            dW = B @ A
            nrm = dW.norm().clamp_min(1e-30)
            left = max(left, ((m.Uk.T @ dW).norm() / nrm).item())
            right = max(right, ((dW @ m.Vk).norm() / nrm).item())
    return out, {"max_rel_UkT_dW": left, "max_rel_dW_Vk": right}


@torch.no_grad()
def merge_adapters_(model, adapters: dict, scale: float) -> None:
    """W <- W + scale * B @ A (FP32 math). Pass projected factors for OPLoRA."""
    for name, ab in adapters.items():
        W = model.get_submodule(name).weight
        delta = scale * (ab["B"].to(W.device, torch.float32) @ ab["A"].to(W.device, torch.float32))
        W.add_(delta.to(W.dtype))


def delta_from_factors(ab: dict, scale: float) -> torch.Tensor:
    return scale * (ab["B"].float() @ ab["A"].float())

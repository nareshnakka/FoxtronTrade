"""
Balanced Algo — FastTrack rules with a 70/30 directional hedge.

Analyzed side gets ~70% of lots; opposite ATM side gets ~30%.
Exit is driven by the primary (analyzed) leg; hedge closes with it.
"""
from __future__ import annotations

from typing import Any


ALGO_NAME = "Balanced"


def hedge_enabled(cfg: dict[str, Any] | None) -> bool:
    c = cfg or {}
    if bool(c.get("hedge_mode", False)):
        return True
    return str(c.get("algo_name") or c.get("profile_name") or "").lower() == "balanced"


def primary_pct(cfg: dict[str, Any] | None = None) -> float:
    c = cfg or {}
    return float(c.get("hedge_primary_pct", 0.70))


def other_pct(cfg: dict[str, Any] | None = None) -> float:
    c = cfg or {}
    return float(c.get("hedge_other_pct", 0.30))


def opposite_side(side: str) -> str:
    return "PE" if side == "CE" else "CE"


def split_hedge_lots(lots: int, cfg: dict[str, Any] | None = None) -> tuple[int, int]:
    """
    Split total FastTrack lots into (primary, hedge).
    Ensures at least 1 hedge lot when total ≥ 2.
    """
    n = max(0, int(lots))
    if n < 2 or not hedge_enabled(cfg):
        return n, 0
    p_frac = primary_pct(cfg)
    p = max(1, int(round(n * p_frac)))
    h = n - p
    if h < 1:
        p = max(1, n - 1)
        h = n - p
    if p < 1:
        return n, 0
    return p, h


def balanced_cfg_overlay(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flags to layer on FastTrack overrides."""
    c = cfg or {}
    return {
        "profile_name": ALGO_NAME,
        "algo_name": ALGO_NAME,
        "hedge_mode": True,
        "hedge_primary_pct": float(c.get("hedge_primary_pct", 0.70)),
        "hedge_other_pct": float(c.get("hedge_other_pct", 0.30)),
    }

"""
Daily soft scalp — cash small PE moves on chop / flat days the primary path skips.

Learned 2026-11-03 from 75 non-traded days (primary 12/90):
  unlock chop + PE SR_REJECT only in the ~11:45–13:00 window,
  vs_open≥40, +5% first-target / −2.5% stop scalp.
  CE soft and morning soft stole primary winners (Oct28); late soft
  knives (Oct5/7 after 13:00). Keep PE-only midday.
"""
from __future__ import annotations

from typing import Any


def soft_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enable_daily_soft_scalp", False))


def soft_exit_overlay(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tight scalp exits for soft fills (override percent manager)."""
    c = cfg or {}
    return {
        "use_percent_exits": True,
        "stop_loss_pct": float(c.get("daily_soft_stop_loss_pct", 2.5)),
        "target_pct_low": float(c.get("daily_soft_target_pct_low", 5.0)),
        "target_pct_high": float(c.get("daily_soft_target_pct_high", 12.0)),
        "lock_profit_pct": float(c.get("daily_soft_lock_profit_pct", 0.8)),
        "trail_giveback_pct": float(c.get("daily_soft_trail_giveback_pct", 2.0)),
        "time_stop_minutes": float(c.get("daily_soft_time_stop_minutes", 14)),
        "exit_at_first_target": True,
        "stop_grace_minutes": float(c.get("daily_soft_stop_grace_minutes", 2)),
        "stop_grace_pct": float(c.get("daily_soft_stop_grace_pct", 4.0)),
    }


def soft_entry_overlay(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Merge onto base cfg for analyze_market on soft path.
    PE-only: absurd CE floor + no CE orb/SR reclaim.
    """
    return {
        **soft_exit_overlay(cfg),
        "block_structure_in_chop": False,
        "allow_pe_breakdown_in_chop": True,
        "prefer_sr_dip_entry": True,
        "enable_sr_reclaim": False,
        "enable_sr_reject": bool(cfg.get("daily_soft_enable_sr_reject", True)),
        "enable_orb_breakout": False,
        "enable_orb_breakdown": True,
        "block_orb_chase_without_retest": True,
        "breakout_max_extension_pts": float(
            cfg.get("daily_soft_breakout_max_extension_pts", 35)
        ),
        "min_path_efficiency": float(cfg.get("daily_soft_min_path_efficiency", 0.12)),
        "require_session_extreme": bool(
            cfg.get("daily_soft_require_session_extreme", False)
        ),
        "directional_min_confidence": float(
            cfg.get("daily_soft_min_confidence", 0.60)
        ),
        "directional_min_margin": float(cfg.get("daily_soft_min_margin", 0.10)),
        "signal_persist_minutes": int(cfg.get("daily_soft_persist_minutes", 1)),
        "ce_min_vs_open": 9999.0,
        "pe_min_vs_open": float(cfg.get("daily_soft_pe_min_vs_open", 40)),
        "block_ce_mid_chase": True,
        # keep soft from late knives
        "no_entry_after_minutes": int(
            cfg.get("daily_soft_no_entry_after_minutes", 280)
        ),
        # Soft path itself still respects prefer_after via analyze_market;
        # hard delay is enforced by the caller (i >= daily_soft_entry_after_minutes).
    }


def merge_soft_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**cfg, **soft_entry_overlay(cfg)}


def soft_lots(cfg: dict[str, Any], primary_lots: int) -> int:
    """Cap soft size — learned with 8 lots; never exceed primary sizing."""
    cap = int(cfg.get("daily_soft_max_lots", 8))
    floor = int(cfg.get("daily_soft_min_lots", 4))
    n = min(primary_lots, cap) if primary_lots > 0 else cap
    return max(floor, n)


def tag_soft_event(event: str | None) -> str:
    e = (event or "SOFT").strip() or "SOFT"
    if e.upper().startswith("DAILY_SOFT"):
        return e
    return f"DAILY_SOFT:{e}"

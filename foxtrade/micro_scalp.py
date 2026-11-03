"""
Timed 10–15pt spot micro-scalp — morning-drive pullback resume.

Learned 2026-11-03 on days still flat after primary+soft PE:
  After first 45m UP drive ≥55pts, enter CE on 30–70% pullback resume.
  Bank at +12 spot pts (or +4% prem). Window ~10:10–11:45.
  Skip if giveback from session high >70 (Oct26 knife) or vs_open <40.
  Micro/soft wins do NOT bank the day — primary SR can still fire.
"""
from __future__ import annotations

from typing import Any


def micro_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enable_micro_scalp", False))


def micro_exit_overlay(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    c = cfg or {}
    return {
        "use_percent_exits": True,
        "stop_loss_pct": float(c.get("micro_scalp_stop_loss_pct", 2.2)),
        "target_pct_low": float(c.get("micro_scalp_target_pct_low", 4.0)),
        "target_pct_high": float(c.get("micro_scalp_target_pct_high", 8.0)),
        "lock_profit_pct": float(c.get("micro_scalp_lock_profit_pct", 0.4)),
        "trail_giveback_pct": float(c.get("micro_scalp_trail_giveback_pct", 1.2)),
        "time_stop_minutes": float(c.get("micro_scalp_time_stop_minutes", 15)),
        "exit_at_first_target": True,
        "stop_grace_minutes": float(c.get("micro_scalp_stop_grace_minutes", 1)),
        "stop_grace_pct": float(c.get("micro_scalp_stop_grace_pct", 3.0)),
    }


def micro_lots(cfg: dict[str, Any], primary_lots: int) -> int:
    cap = int(cfg.get("micro_scalp_max_lots", 6))
    floor = int(cfg.get("micro_scalp_min_lots", 4))
    n = min(primary_lots, cap) if primary_lots > 0 else cap
    return max(floor, n)


def spot_target_pts(cfg: dict[str, Any]) -> float:
    return float(cfg.get("micro_scalp_spot_target_pts", 12))


def favorable_spot_move(side: str, entry_spot: float, spot: float) -> float:
    if side == "CE":
        return float(spot) - float(entry_spot)
    return float(entry_spot) - float(spot)


def spot_bank_hit(
    *,
    side: str,
    entry_spot: float,
    spot: float,
    entry_px: float,
    px: float,
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Exit when spot has moved +target in our favor and premium is green."""
    tgt = spot_target_pts(cfg)
    fav = favorable_spot_move(side, entry_spot, spot)
    if fav >= tgt and float(px) >= float(entry_px):
        return {
            "action": "exit",
            "reason": f"micro spot +{tgt:g} ({fav:.1f})",
            "fav_spot": fav,
        }
    return None


def detect_pullback_resume(
    closes: list[float],
    cfg: dict[str, Any],
    *,
    bar_i: int | None = None,
) -> dict[str, Any]:
    """
    One-shot signal at the latest bar (or bar_i).
    Returns {allow_entry, side, event, reason, confidence, drive, pb}.
    """
    i = len(closes) - 1 if bar_i is None else int(bar_i)
    empty = {
        "allow_entry": False,
        "side": None,
        "event": None,
        "reason": "micro: no signal",
        "confidence": 0.0,
        "drive": 0.0,
        "pb": 0.0,
    }
    if not micro_enabled(cfg) or i < 10 or len(closes) <= i:
        return empty

    drive_n = int(cfg.get("micro_scalp_drive_minutes", 45))
    after = int(cfg.get("micro_scalp_entry_after_minutes", 55))
    until = int(cfg.get("micro_scalp_no_entry_after_minutes", 150))
    min_drive = float(cfg.get("micro_scalp_min_drive_pts", 45))
    pb_lo = float(cfg.get("micro_scalp_pb_frac_lo", 0.30))
    pb_hi = float(cfg.get("micro_scalp_pb_frac_hi", 0.70))
    confirm = int(cfg.get("micro_scalp_confirm_bars", 1))
    both = bool(cfg.get("micro_scalp_both_sides", True))
    min_vs = float(cfg.get("micro_scalp_min_vs_open", 40))
    max_giveback = float(cfg.get("micro_scalp_max_giveback_from_extreme", 80))

    if i < after or i >= until:
        return {**empty, "reason": f"micro: outside window {after}-{until}m"}
    if len(closes) < drive_n + 5:
        return {**empty, "reason": "micro: need morning drive bars"}

    o = float(closes[0])
    drive = float(closes[drive_n - 1]) - o
    if abs(drive) < min_drive:
        return {**empty, "reason": f"micro: |drive| {abs(drive):.0f}<{min_drive:.0f}"}

    side = "CE" if drive > 0 else "PE"
    if not both and side != "CE":
        return {**empty, "reason": "micro: PE disabled"}

    extreme = max(closes[:drive_n]) if side == "CE" else min(closes[:drive_n])
    # Session extreme so far (not just drive window) — avoid late knife reclaim
    sess_ext = max(closes[: i + 1]) if side == "CE" else min(closes[: i + 1])
    spot = float(closes[i])
    vs_open = spot - o
    if side == "CE" and vs_open < min_vs:
        return {
            **empty,
            "side": side,
            "drive": drive,
            "reason": f"micro: CE vs_open {vs_open:.0f}<{min_vs:.0f}",
        }
    if side == "PE" and vs_open > -min_vs:
        return {
            **empty,
            "side": side,
            "drive": drive,
            "reason": f"micro: PE vs_open {vs_open:.0f}>-{-min_vs:.0f}",
        }

    if side == "CE":
        pb = float(extreme) - spot
        giveback = float(sess_ext) - spot
        depth_ok = pb_lo * abs(drive) <= pb <= pb_hi * abs(drive)
        if confirm <= 1:
            resume = spot > float(closes[i - 1])
        else:
            resume = spot > float(closes[i - 1]) >= float(closes[i - 2]) - 2
    else:
        pb = spot - float(extreme)
        giveback = spot - float(sess_ext)
        depth_ok = pb_lo * abs(drive) <= pb <= pb_hi * abs(drive)
        if confirm <= 1:
            resume = spot < float(closes[i - 1])
        else:
            resume = spot < float(closes[i - 1]) <= float(closes[i - 2]) + 2

    if giveback > max_giveback:
        return {
            **empty,
            "side": side,
            "drive": drive,
            "pb": pb,
            "reason": f"micro: giveback {giveback:.0f}>{max_giveback:.0f} from extreme",
        }
    if not depth_ok:
        return {
            **empty,
            "side": side,
            "drive": drive,
            "pb": pb,
            "reason": f"micro: pb {pb:.0f} not in {pb_lo:.0%}-{pb_hi:.0%} of drive",
        }
    if not resume:
        return {
            **empty,
            "side": side,
            "drive": drive,
            "pb": pb,
            "reason": "micro: waiting resume bar",
        }

    conf = min(0.85, 0.55 + abs(drive) / 400.0)
    return {
        "allow_entry": True,
        "side": side,
        "event": "MICRO_PB_RESUME",
        "reason": (
            f"micro {side} drive {drive:+.0f} pb {pb:.0f} "
            f"resume @ +{i}m"
        ),
        "confidence": conf,
        "drive": drive,
        "pb": pb,
    }

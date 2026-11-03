"""
Sudden spike / fall detection + pre-event sensing.

Designed for ~1s LTP samples from MarketHub:
  - Impulse: sharp move in last N seconds with acceleration
  - Pre-event: smaller move that aligns with macro (VIX/fut/news) — act early
"""

from __future__ import annotations

from typing import Any


def detect_impulse(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Returns:
      allow_entry, side, confidence, event, reason, mode (impulse|pre_event),
      move_pts, velocity, seconds
    """
    cfg = cfg or {}
    macro = macro or {}
    out: dict[str, Any] = {
        "allow_entry": False,
        "side": None,
        "confidence": 0.0,
        "event": None,
        "reason": "",
        "mode": None,
        "move_pts": 0.0,
        "velocity": 0.0,
        "seconds": 0,
    }
    if not cfg.get("impulse_entry_enabled", True):
        out["reason"] = "impulse disabled"
        return out
    if len(closes) < 6:
        out["reason"] = "warming"
        return out

    look = max(3, int(cfg.get("impulse_lookback_sec", 8)))
    look = min(look, len(closes) - 1)
    min_pts = float(cfg.get("impulse_min_pts", 22.0))
    pre_pts = float(cfg.get("pre_event_min_pts", 10.0))
    accel_need = float(cfg.get("impulse_accel_ratio", 1.55))
    min_conf = float(cfg.get("impulse_min_confidence", 0.72))

    spot = float(closes[-1])
    start = float(closes[-(look + 1)])
    move = spot - start
    out["move_pts"] = round(move, 2)
    out["seconds"] = look
    out["velocity"] = round(move / max(look, 1), 3)

    # Acceleration: 2nd half of window vs 1st half
    mid = max(2, look // 2)
    first = float(closes[-look - 1 + mid]) - start if look >= 4 else move * 0.4
    second = spot - float(closes[-look - 1 + mid]) if look >= 4 else move * 0.6
    # Same direction + second half stronger
    accel_ok = False
    if move > 0 and second > 0 and abs(first) > 0.5:
        accel_ok = second >= abs(first) * accel_need * 0.5 or second >= first * accel_need
    elif move < 0 and second < 0 and abs(first) > 0.5:
        accel_ok = abs(second) >= abs(first) * accel_need * 0.5 or abs(second) >= abs(first) * accel_need
    elif abs(move) >= min_pts:
        # Extreme speed alone counts even if first half was flat
        accel_ok = abs(out["velocity"]) >= float(cfg.get("impulse_min_velocity", 2.2))

    # Continuity: last 3 ticks not reversing hard
    cont = True
    if len(closes) >= 4:
        a, b, c = float(closes[-3]), float(closes[-2]), float(closes[-1])
        if move > 0:
            cont = c >= b - 3 and b >= a - 5
        else:
            cont = c <= b + 3 and b <= a + 5

    hint = str(macro.get("side_hint") or "").upper()
    sm = str(macro.get("smart_money") or "").lower()
    hb = str(macro.get("headline_bias") or "").lower()
    vix = macro.get("vix")
    vix_chg = macro.get("vix_change_pct")

    side = "CE" if move > 0 else "PE" if move < 0 else None
    if side is None:
        out["reason"] = "flat"
        return out

    # Macro conflict: don't buy CE into risk-off spike trap (often short covering then dump)
    if side == "CE" and (hb == "risk_off" or sm == "short_pressure"):
        if abs(move) < min_pts * 1.35:
            out["reason"] = "CE impulse blocked (risk-off / short pressure)"
            return out
    if side == "PE" and hb == "risk_on" and sm == "long_pressure" and abs(move) < min_pts * 1.2:
        out["reason"] = "PE impulse blocked (risk-on / long pressure)"
        return out

    # --- Full impulse ---
    if abs(move) >= min_pts and accel_ok and cont:
        conf = min(0.95, 0.70 + abs(move) / 120.0 + abs(out["velocity"]) / 20.0)
        if hint == side:
            conf = min(0.97, conf + 0.05)
        if conf < min_conf:
            out["reason"] = f"impulse conf {conf:.2f} < {min_conf}"
            out["confidence"] = conf
            out["side"] = side
            return out
        out.update(
            {
                "allow_entry": True,
                "side": side,
                "confidence": round(conf, 3),
                "event": "IMPULSE_SPIKE" if side == "CE" else "IMPULSE_FALL",
                "mode": "impulse",
                "reason": (
                    f"{'spike' if side == 'CE' else 'fall'} "
                    f"{move:+.0f}pts/{look}s vel={out['velocity']:.1f}/s"
                ),
            }
        )
        return out

    # --- Pre-event: smaller move + macro already pointing that way ---
    pre_align = (
        hint == side
        or (side == "PE" and (sm == "short_pressure" or hb == "risk_off"))
        or (side == "CE" and (sm == "long_pressure" or hb == "risk_on"))
        or (side == "PE" and vix is not None and float(vix) >= 18)
        or (side == "PE" and vix_chg is not None and float(vix_chg) >= 3)
        or (side == "CE" and vix_chg is not None and float(vix_chg) <= -3)
    )
    if (
        cfg.get("pre_event_entry_enabled", True)
        and abs(move) >= pre_pts
        and cont
        and pre_align
        and (accel_ok or abs(out["velocity"]) >= float(cfg.get("pre_event_min_velocity", 1.2)))
    ):
        conf = min(0.90, 0.68 + abs(move) / 100.0 + 0.06)
        if conf < float(cfg.get("pre_event_min_confidence", 0.70)):
            out["reason"] = "pre-event conf low"
            return out
        out.update(
            {
                "allow_entry": True,
                "side": side,
                "confidence": round(conf, 3),
                "event": "PRE_EVENT",
                "mode": "pre_event",
                "reason": (
                    f"pre-event {side} {move:+.0f}pts/{look}s "
                    f"(macro {hint or hb or sm or 'align'})"
                ),
            }
        )
        return out

    out["reason"] = f"no impulse ({move:+.0f}/{look}s)"
    out["side"] = side
    return out


def impulse_exit_overrides(cfg: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
    """Tight/fast exit profile for impulse & pre-event trades."""
    base = dict(cfg)
    if mode == "pre_event":
        base.update(
            {
                "stop_loss_pct": float(cfg.get("pre_event_stop_loss_pct", 4.0)),
                "target_pct_low": float(cfg.get("pre_event_target_pct", 5.0)),
                "target_pct_high": float(cfg.get("pre_event_target_pct_high", 15.0)),
                "exit_at_first_target": True,
                "lock_profit_pct": float(cfg.get("pre_event_lock_pct", 1.0)),
                "trail_giveback_pct": float(cfg.get("pre_event_trail_pct", 3.0)),
                "time_stop_minutes": float(cfg.get("pre_event_time_stop_min", 5.0)),
            }
        )
    else:
        base.update(
            {
                "stop_loss_pct": float(cfg.get("impulse_stop_loss_pct", 3.5)),
                "target_pct_low": float(cfg.get("impulse_target_pct", 4.0)),
                "target_pct_high": float(cfg.get("impulse_target_pct_high", 12.0)),
                "exit_at_first_target": True,
                "lock_profit_pct": float(cfg.get("impulse_lock_pct", 1.0)),
                "trail_giveback_pct": float(cfg.get("impulse_trail_pct", 2.5)),
                "time_stop_minutes": float(cfg.get("impulse_time_stop_min", 3.0)),
            }
        )
    return base

"""
Trend Follow — always ride session trend via breakout / breakdown.

Uses existing structure+ORB break detection, but insists on trend alignment
and fires on trend *change* when a confirming break prints the new side.

  UP + BREAKOUT      → CE
  DOWN + BREAKDOWN   → PE
  bias flip + break  → new side (trend change)
"""

from __future__ import annotations

from typing import Any

from foxtrade.expert_filters import detect_breakout_breakdown
from foxtrade.session_trend import compute_session_trend


def trend_follow_enabled(cfg: dict[str, Any] | None) -> bool:
    return bool((cfg or {}).get("enable_trend_follow", True))


def _continuation_break(
    closes: list[float], cfg: dict[str, Any]
) -> dict[str, Any]:
    """
    Rolling swing break for trend continuation.

    Unlike institutional detect_breakout_breakdown, this does NOT require
    price to sit at the day extreme — so mid-session trend legs still fire.
    """
    look = int(cfg.get("trend_follow_lookback_minutes", 25))
    hold_n = int(cfg.get("trend_follow_hold_minutes", 3))
    buf = float(cfg.get("trend_follow_break_buffer_pts", 6))
    if len(closes) < look + hold_n + 5:
        return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}
    spot = float(closes[-1])
    prior = closes[-(look + hold_n) : -hold_n]
    if len(prior) < 8:
        return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}
    swing_hi = max(prior)
    swing_lo = min(prior)
    recent = closes[-hold_n:]
    held_high = min(recent) >= swing_hi - buf * 0.35
    held_low = max(recent) <= swing_lo + buf * 0.35
    if spot >= swing_hi + buf and held_high:
        strength = min(1.0, (spot - swing_hi) / max(buf * 2.5, 1))
        return {
            "event": "BREAKOUT",
            "prefer_side": "CE",
            "reason": f"trend BO above {swing_hi:.0f} (+{spot - swing_hi:.0f})",
            "strength": round(strength, 3),
            "level": swing_hi,
            "style": "trend_cont",
        }
    if spot <= swing_lo - buf and held_low:
        strength = min(1.0, (swing_lo - spot) / max(buf * 2.5, 1))
        return {
            "event": "BREAKDOWN",
            "prefer_side": "PE",
            "reason": f"trend BD below {swing_lo:.0f} ({spot - swing_lo:.0f})",
            "strength": round(strength, 3),
            "level": swing_lo,
            "style": "trend_cont",
        }
    return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}


def detect_trend_follow(
    closes: list[float],
    cfg: dict[str, Any] | None = None,
    *,
    ema9: float | None = None,
    ema21: float | None = None,
    prior_bias: str | None = None,
) -> dict[str, Any]:
    """
    Returns allow_entry + side when a trend-aligned breakout/breakdown fires.
    """
    cfg = cfg or {}
    empty: dict[str, Any] = {
        "allow_entry": False,
        "side": None,
        "confidence": 0.0,
        "event": None,
        "reason": "trend_follow idle",
        "bias": None,
        "break_event": None,
        "trend_change": False,
        "level": None,
        "strength": 0.0,
    }
    if not trend_follow_enabled(cfg):
        return {**empty, "reason": "trend_follow disabled"}
    if len(closes) < 20:
        return {**empty, "reason": "warming"}

    after = int(cfg.get("trend_follow_entry_after_minutes", 20))
    no_after = int(cfg.get("trend_follow_no_entry_after_minutes", 300))
    mins = max(0, len(closes) - 1)
    if mins < after:
        return {**empty, "reason": f"wait +{after}m"}
    if mins >= no_after:
        return {**empty, "reason": f"no entry after +{no_after}m"}

    day_open = float(closes[0])
    spot = float(closes[-1])
    trend = compute_session_trend(
        spot=spot,
        day_open=day_open,
        closes=closes,
        ema9=ema9,
        ema21=ema21,
        cfg=cfg,
    )
    bias = str(trend.get("bias") or "FLAT")
    prefer = trend.get("prefer_side")
    # Prefer continuation break; fall back to ORB/structure if that prints.
    brk = _continuation_break(closes, cfg)
    if not brk.get("event"):
        brk_cfg = {
            **cfg,
            "entry_style": str(cfg.get("trend_follow_entry_style", "orb")),
            "enable_orb_breakout": True,
            "enable_orb_breakdown": True,
            # Trend days often have wide OR — allow larger OR for this path only
            "orb_max_range_pts": float(
                cfg.get("trend_follow_orb_max_range_pts", 260)
            ),
        }
        brk = detect_breakout_breakdown(closes, cfg=brk_cfg)
    ev = brk.get("event")
    brk_side = brk.get("prefer_side")
    strength = float(brk.get("strength") or 0)
    min_str = float(cfg.get("trend_follow_min_break_strength", 0.28))

    empty["bias"] = bias
    empty["break_event"] = ev
    empty["level"] = brk.get("level")
    empty["strength"] = strength

    if ev not in ("BREAKOUT", "BREAKDOWN") or brk_side not in ("CE", "PE"):
        return {**empty, "reason": f"trend {bias} — no break"}
    if strength < min_str:
        return {
            **empty,
            "reason": f"break weak {strength:.2f}<{min_str:.2f}",
        }

    prior = (prior_bias or "").upper() or None
    trend_change = bool(
        prior in ("UP", "DOWN")
        and bias in ("UP", "DOWN")
        and prior != bias
    )
    # Align break with session bias (or accept flip on confirmed trend change).
    aligned = (
        (ev == "BREAKOUT" and brk_side == "CE" and (prefer == "CE" or bias == "UP"))
        or (
            ev == "BREAKDOWN"
            and brk_side == "PE"
            and (prefer == "PE" or bias == "DOWN")
        )
    )
    flip_ok = False
    if trend_change and bool(cfg.get("trend_follow_on_change", True)):
        # New UP → need BREAKOUT/CE; new DOWN → BREAKDOWN/PE
        flip_ok = (bias == "UP" and brk_side == "CE") or (
            bias == "DOWN" and brk_side == "PE"
        )

    if not aligned and not flip_ok:
        return {
            **empty,
            "reason": f"break {ev} fights trend {bias}",
        }

    # Momentum must confirm the break side
    look = int(cfg.get("trend_momentum_minutes", 10))
    mom = spot - float(closes[-(look + 1)]) if len(closes) > look else 0.0
    min_mom = float(cfg.get("trend_follow_min_mom", 8.0))
    if brk_side == "CE" and mom < min_mom:
        return {**empty, "reason": f"CE needs mom≥{min_mom:g} (got {mom:.0f})"}
    if brk_side == "PE" and mom > -min_mom:
        return {**empty, "reason": f"PE needs mom≤−{min_mom:g} (got {mom:.0f})"}

    vs_open = spot - day_open
    min_vs = float(cfg.get("trend_follow_min_vs_open", 25.0))
    if brk_side == "CE" and vs_open < min_vs and not flip_ok:
        return {**empty, "reason": f"CE vs_open {vs_open:.0f}<{min_vs:g}"}
    if brk_side == "PE" and vs_open > -min_vs and not flip_ok:
        return {**empty, "reason": f"PE vs_open {vs_open:.0f}>-{min_vs:g}"}

    conf = 0.58 + min(0.28, strength * 0.35) + min(0.12, abs(mom) / 200.0)
    if trend.get("block_counter_trend"):
        conf = min(0.95, conf + 0.06)
    if flip_ok:
        conf = min(0.95, conf + 0.05)
        event = f"TREND_CHANGE_{ev}"
        reason = (
            f"trend change {prior}→{bias} + {ev} "
            f"{brk.get('reason') or ''}".strip()
        )
    else:
        event = f"TREND_{ev}"
        reason = f"trend {bias} + {brk.get('reason') or ev}"

    min_conf = float(cfg.get("trend_follow_min_confidence", 0.60))
    if conf < min_conf:
        return {
            **empty,
            "reason": f"conf {conf:.0%}<{min_conf:.0%}",
            "confidence": round(conf, 3),
        }

    return {
        "allow_entry": True,
        "side": brk_side,
        "confidence": round(conf, 3),
        "event": event,
        "reason": reason,
        "bias": bias,
        "break_event": ev,
        "trend_change": bool(flip_ok),
        "level": brk.get("level"),
        "strength": round(strength, 3),
        "vs_open": round(vs_open, 1),
        "momentum": round(mom, 1),
        "trend": trend,
    }


def trend_follow_should_exit_open(
    *,
    open_side: str,
    closes: list[float],
    cfg: dict[str, Any] | None = None,
    ema9: float | None = None,
    ema21: float | None = None,
) -> dict[str, Any]:
    """Exit open long when opposite trend break confirms a flip."""
    cfg = cfg or {}
    out = {"action": "hold", "reason": ""}
    if not trend_follow_enabled(cfg):
        return out
    if not bool(cfg.get("trend_follow_exit_on_flip", True)):
        return out
    if open_side not in ("CE", "PE") or len(closes) < 20:
        return out
    sig = detect_trend_follow(
        closes, cfg, ema9=ema9, ema21=ema21, prior_bias=None
    )
    if not sig.get("allow_entry"):
        return out
    new_side = sig.get("side")
    if new_side and new_side != open_side and (
        sig.get("trend_change") or sig.get("break_event")
    ):
        return {
            "action": "exit",
            "reason": f"trend flip → {new_side}: {sig.get('reason')}",
        }
    return out

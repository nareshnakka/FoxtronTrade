"""
ATM parallel-channel scanner (CE + PE).

Charts (user):
  - Ascending channel → LONG that option (bottom-edge entry → trail to top;
    or break ABOVE top → huge/fast add).
  - Descending channel → SHORT bias (top-edge fade → trail to bottom;
    or break BELOW bottom → huge/fast leave / flatten).

Runs beside dual/fast-open. Entries use algo id ``atm_channel``.
Scan always updates status for the criteria banner when enabled.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from foxtrade import broker
from foxtrade import trade_store
from foxtrade.position_integrity import can_open_new, open_for_algo

IST = ZoneInfo("Asia/Kolkata")
ALGO_ID = "atm_channel"
_DATA = Path(__file__).resolve().parent.parent / "data"
_REENTRY_PATH = _DATA / "atm_reentry_cooldown.json"
_reentry_lock = threading.Lock()

# Module-level NFO instruments cache — kite.instruments() is huge and must NEVER
# run on the engine tick thread (it freezes heartbeat → stall kill loop).
_nfo_cache_lock = threading.Lock()
_nfo_nifty_opts: list[dict[str, Any]] = []
_nfo_cache_ts: float = 0.0
_NFO_CACHE_TTL = 30 * 60  # 30 minutes


def _get_nifty_opts_cached(*, force: bool = False) -> list[dict[str, Any]]:
    global _nfo_nifty_opts, _nfo_cache_ts
    now = time.time()
    with _nfo_cache_lock:
        if (
            not force
            and _nfo_nifty_opts
            and now - _nfo_cache_ts < _NFO_CACHE_TTL
        ):
            return list(_nfo_nifty_opts)
    try:
        from foxtrade.kite_cache import get_instruments

        inst = get_instruments("NFO", force=force)
        nifty = [
            i
            for i in inst
            if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")
        ]
        with _nfo_cache_lock:
            _nfo_nifty_opts = nifty
            _nfo_cache_ts = time.time()
        return list(nifty)
    except Exception:
        with _nfo_cache_lock:
            return list(_nfo_nifty_opts)


def channel_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("atm_channel_enabled", False))


def _load_reentry_until() -> dict[str, float]:
    try:
        if not _REENTRY_PATH.exists():
            return {}
        raw = json.loads(_REENTRY_PATH.read_text(encoding="utf-8"))
        out: dict[str, float] = {}
        now = time.time()
        for k, v in (raw or {}).items():
            try:
                ts = float(v)
            except (TypeError, ValueError):
                continue
            if ts > now:
                out[str(k)] = ts
        return out
    except Exception:
        return {}


def _save_reentry_until(mp: dict[str, float]) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        now = time.time()
        clean = {k: float(v) for k, v in mp.items() if float(v) > now}
        _REENTRY_PATH.write_text(json.dumps(clean, indent=0), encoding="utf-8")
    except Exception:
        pass


def _set_reentry_cooldown(sym: str, seconds: float) -> None:
    if not sym or seconds <= 0:
        return
    with _reentry_lock:
        mp = _load_reentry_until()
        mp[str(sym)] = max(float(mp.get(str(sym)) or 0), time.time() + float(seconds))
        # Also cool the whole algo so CE/PE flip-spam cannot chain
        mp["*"] = max(float(mp.get("*") or 0), time.time() + float(seconds))
        _save_reentry_until(mp)


def _reentry_blocked(sym: str) -> bool:
    with _reentry_lock:
        mp = _load_reentry_until()
    now = time.time()
    return now < float(mp.get(str(sym)) or 0) or now < float(mp.get("*") or 0)


def _atm_exit_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Ensure ATM min-hold floors the global entry hold (never 0 by accident)."""
    out = dict(cfg or {})
    base = max(0.0, float(out.get("entry_min_hold_minutes") or 0))
    atm_hold = max(0.0, float(out.get("atm_channel_min_hold_minutes") or 5))
    out["entry_min_hold_minutes"] = max(base, atm_hold, 5.0)
    return out


def _long_stop(px: float, channel_stop: float, sl_pct: float) -> float:
    """
    Long stop must sit below entry with real room.
    Prior bug used max(channel, pct) which kept hairline channel stops (~0.05pt).
    """
    px = float(px)
    pct_stop = px * (1.0 - max(0.5, float(sl_pct)) / 100.0)
    ch = float(channel_stop) if channel_stop and channel_stop > 0 else pct_stop
    # Wider (lower) stop wins; never above entry - 0.4%
    stop = min(ch, pct_stop)
    return min(stop, px * 0.996)


def _target_for_entry(px: float, upper: float, width: float, *, is_break: bool) -> float:
    """Target must sit above the fill.

    A break only fires once price is already through the upper band, so that
    band is behind the fill. Using it as the target booked the trade on the
    next tick and the same signal bought again — one order a second.
    """
    px = float(px or 0)
    upper = float(upper or 0)
    width = max(float(width or 0), 0.0)
    if not is_break and upper > px > 0:
        return round(upper, 2)
    lift = max(width, px * 0.015) if px > 0 else width
    base = px if px > 0 else upper
    return round(base + lift, 2)


def _apply_opp_break(
    signals: dict[str, Any],
    *,
    ce_px: float,
    pe_px: float,
) -> dict[str, Any]:
    """CE DESC break ↓ → PE BUY_BREAK (and mirror). Mutates a copy."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in signals.items()}
    ce_s = out.get("CE") or {}
    pe_s = out.get("PE") or {}
    if (
        ce_s.get("ok")
        and ce_s.get("bias") == "DESC"
        and ce_s.get("signal") == "BREAK_DOWN"
        and pe_s.get("side_action") not in ("BUY_BREAK", "BUY_EDGE")
    ):
        px = float(pe_px or pe_s.get("price") or 0)
        out["PE"] = {
            **pe_s,
            "ok": True,
            "bias": pe_s.get("bias") or "ASC",
            "side_action": "BUY_BREAK",
            "signal": "OPP_CE_BREAK_DOWN",
            "reason": "CE DESC break ↓ → PE huge/fast long",
            "confidence": max(float(pe_s.get("confidence") or 0), 0.72),
            "upper": pe_s.get("upper") or (px * 1.05 if px else None),
            "lower": pe_s.get("lower") or (px * 0.97 if px else None),
            "stop_px": pe_s.get("stop_px") or (px * 0.978 if px else None),
            "target_px": pe_s.get("target_px") or (px * 1.05 if px else None),
            "price": pe_s.get("price") or px,
        }
    if (
        pe_s.get("ok")
        and pe_s.get("bias") == "DESC"
        and pe_s.get("signal") == "BREAK_DOWN"
        and (out.get("CE") or {}).get("side_action") not in ("BUY_BREAK", "BUY_EDGE")
    ):
        ce2 = out.get("CE") or {}
        px = float(ce_px or ce2.get("price") or 0)
        out["CE"] = {
            **ce2,
            "ok": True,
            "bias": ce2.get("bias") or "ASC",
            "side_action": "BUY_BREAK",
            "signal": "OPP_PE_BREAK_DOWN",
            "reason": "PE DESC break ↓ → CE huge/fast long",
            "confidence": max(float(ce2.get("confidence") or 0), 0.72),
            "upper": ce2.get("upper") or (px * 1.05 if px else None),
            "lower": ce2.get("lower") or (px * 0.97 if px else None),
            "stop_px": ce2.get("stop_px") or (px * 0.978 if px else None),
            "target_px": ce2.get("target_px") or (px * 1.05 if px else None),
            "price": ce2.get("price") or px,
        }
    return out


def detect_atm_channel_entry(
    ce_candles: list[dict[str, float]],
    pe_candles: list[dict[str, float]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Pure entry pick for backtest / offline replay (no Kite).
    Returns allow_entry + side + channel mode when BUY_BREAK / BUY_EDGE fires.
    """
    empty: dict[str, Any] = {
        "allow_entry": False,
        "side": None,
        "event": None,
        "reason": "atm_channel: no signal",
        "confidence": 0.0,
        "channel_mode": None,
        "stop_px": None,
        "target_px": None,
        "signals": {},
    }
    if not channel_enabled(cfg) or not bool(cfg.get("atm_channel_entries", True)):
        return {**empty, "reason": "atm_channel: disabled"}

    ce_det = detect_parallel_channel(ce_candles, cfg)
    pe_det = detect_parallel_channel(pe_candles, cfg)
    ce_px = float(ce_candles[-1]["close"]) if ce_candles else 0.0
    pe_px = float(pe_candles[-1]["close"]) if pe_candles else 0.0
    signals = _apply_opp_break(
        {"CE": ce_det, "PE": pe_det}, ce_px=ce_px, pe_px=pe_px
    )
    empty["signals"] = signals

    prefer_break = bool(cfg.get("atm_channel_prefer_break", True))
    allow_edge = bool(cfg.get("atm_channel_allow_edge", True))
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for side, det in signals.items():
        if not isinstance(det, dict) or not det.get("ok"):
            continue
        action = det.get("side_action")
        if action == "BUY_BREAK":
            ranked.append((0 if prefer_break else 1, side, det))
        elif action == "BUY_EDGE" and allow_edge:
            ranked.append((1 if prefer_break else 0, side, det))
    if not ranked:
        why = (signals.get("CE") or {}).get("reason") or (signals.get("PE") or {}).get(
            "reason"
        ) or "no buy"
        return {**empty, "reason": f"atm_channel: {why}"}

    ranked.sort(key=lambda x: (x[0], -float(x[2].get("confidence") or 0)))
    _prio, side, det = ranked[0]
    is_break = det.get("side_action") == "BUY_BREAK"
    ch_mode = "BREAK" if is_break else "EDGE"
    px = float(det.get("price") or (ce_px if side == "CE" else pe_px) or 0)
    sl_pct = float(
        cfg.get(
            "atm_channel_break_sl_pct" if is_break else "atm_channel_edge_sl_pct",
            2.2 if is_break else 1.5,
        )
    )
    stop = float(det.get("stop_px") or (px * (1.0 - sl_pct / 100.0) if px else 0))
    if px > 0:
        stop = max(stop, px * (1.0 - sl_pct / 100.0))
    target = _target_for_entry(
        px,
        float(det.get("upper") or 0),
        float(det.get("width") or 0),
        is_break=is_break,
    )
    return {
        "allow_entry": True,
        "side": side,
        "event": f"ATM_{ch_mode}_{det.get('signal') or ch_mode}",
        "reason": f"atm_channel {ch_mode}: {det.get('reason')}",
        "confidence": float(det.get("confidence") or 0.7),
        "channel_mode": ch_mode,
        "stop_px": round(stop, 2) if stop else None,
        "target_px": round(target, 2) if target else None,
        "bias": det.get("bias"),
        "signal": det.get("signal"),
        "signals": signals,
    }


def atm_channel_lots(cfg: dict[str, Any], *, is_break: bool, base_lots: int = 1) -> int:
    base = max(1, int(base_lots or 1))
    if is_break:
        lots = max(base, int(cfg.get("atm_channel_break_lots") or max(base * 2, 2)))
        return min(lots, int(cfg.get("atm_channel_break_max_lots", 8)))
    lots = max(1, int(cfg.get("atm_channel_edge_lots") or base))
    return min(lots, int(cfg.get("atm_channel_edge_max_lots", 4)))


def atm_channel_should_exit(
    *,
    px: float,
    entry_px: float,
    peak: float,
    stop_px: float,
    target_px: float,
    channel_mode: str,
    side_det: dict[str, Any] | None,
    cfg: dict[str, Any],
    held_minutes: float = 0.0,
    confidence: float | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    """Exit rules mirroring AtmChannelBook._manage_open (no broker)."""
    from foxtrade.expert_filters import entry_hold_gate

    det = side_det or {}
    cfg = _atm_exit_cfg(cfg)
    gate = entry_hold_gate(
        entry=entry_px,
        px=px,
        held_minutes=held_minutes,
        cfg=cfg,
        confidence=confidence,
        side=side,
    )
    # Confidence-scaled premium loss may cut during the min-hold window.
    if gate["hard_loss_exit"]:
        return {"action": "exit", "reason": gate["reason"], "peak": max(peak, px)}
    # Tight channel % SL only after min-hold (unless loss-points already hit).
    if (
        not gate["in_hold"]
        and stop_px > 0
        and px <= stop_px
    ):
        return {"action": "exit", "reason": f"channel SL {stop_px:.1f}"}
    if gate["in_hold"]:
        return {"action": "hold", "peak": max(peak, px)}
    if det.get("side_action") == "EXIT_BREAK":
        return {
            "action": "exit",
            "reason": f"channel {det.get('signal')} leave — {det.get('reason')}",
        }
    if target_px > entry_px and px >= target_px:
        return {"action": "exit", "reason": f"channel target {target_px:.1f}"}
    if channel_mode == "BREAK" and det.get("signal") == "BREAK_DOWN":
        return {"action": "exit", "reason": "break trade failed — fast leave"}
    if target_px > 0 and px > entry_px:
        # Channel trail base; CE slower grind → wider giveback, PE dumps → tighter
        trail_give = float(cfg.get("atm_channel_trail_giveback_pct", 1.2))
        if cfg.get("side_pace_rule_enabled", True):
            s = (side or "").upper()
            if s == "CE":
                trail_give = max(
                    trail_give, float(cfg.get("ce_atm_trail_giveback_pct", 1.8))
                )
            elif s == "PE":
                trail_give = min(
                    trail_give, float(cfg.get("pe_atm_trail_giveback_pct", 0.9))
                )
        peak_u = max(peak, px)
        trail_stop = peak_u * (1.0 - trail_give / 100.0)
        if stop_px > 0:
            trail_stop = max(trail_stop, stop_px)
        if px <= trail_stop and peak_u > entry_px * 1.008:
            return {"action": "exit", "reason": f"channel trail {trail_stop:.1f}"}
    return {"action": "hold", "peak": max(peak, px)}


def _pivot_highs(highs: list[float], strength: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    n = len(highs)
    for i in range(strength, n - strength):
        h = highs[i]
        if all(h >= highs[i - k] for k in range(1, strength + 1)) and all(
            h >= highs[i + k] for k in range(1, strength + 1)
        ):
            out.append((i, h))
    return out


def _pivot_lows(lows: list[float], strength: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    n = len(lows)
    for i in range(strength, n - strength):
        lo = lows[i]
        if all(lo <= lows[i - k] for k in range(1, strength + 1)) and all(
            lo <= lows[i + k] for k in range(1, strength + 1)
        ):
            out.append((i, lo))
    return out


def _fit_line(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    """Return (slope, intercept) for y = slope*x + intercept. Need ≥2 points."""
    if len(points) < 2:
        return None
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    n = float(len(xs))
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return None
    slope = (n * sxy - sx * sy) / den
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _line_y(slope: float, intercept: float, x: float) -> float:
    return slope * x + intercept


def ticks_to_ohlc_1m(
    series: list[dict[str, float]] | list[tuple[float, float]],
) -> list[dict[str, float]]:
    """Resample tick {time,value} (or (t,v)) into 1-minute OHLC bars."""
    buckets: dict[int, dict[str, float]] = {}
    for row in series:
        if isinstance(row, dict):
            t = int(row.get("time") or 0)
            v = float(row.get("value") or 0)
        else:
            t = int(row[0])
            v = float(row[1])
        if t <= 0 or v <= 0:
            continue
        minute = t - (t % 60)
        bar = buckets.get(minute)
        if bar is None:
            buckets[minute] = {
                "time": float(minute),
                "open": v,
                "high": v,
                "low": v,
                "close": v,
            }
        else:
            bar["high"] = max(bar["high"], v)
            bar["low"] = min(bar["low"], v)
            bar["close"] = v
    return [buckets[k] for k in sorted(buckets)]


def detect_parallel_channel(
    candles: list[dict[str, float]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Detect ascending/descending parallel channel from 1m OHLC.
    Returns status dict always (allow=False when no quality channel).
    """
    lookback = int(cfg.get("atm_channel_lookback_bars", 90))
    strength = int(cfg.get("atm_channel_pivot_strength", 2))
    min_touches = int(cfg.get("atm_channel_min_touches", 3))
    max_slope_diff = float(cfg.get("atm_channel_max_slope_diff", 0.35))
    min_abs_slope = float(cfg.get("atm_channel_min_abs_slope", 0.015))
    min_width_pct = float(cfg.get("atm_channel_min_width_pct", 1.2))
    max_width_pct = float(cfg.get("atm_channel_max_width_pct", 18.0))
    edge_pct = float(cfg.get("atm_channel_edge_pct", 18.0))  # % of width
    break_buf_pct = float(cfg.get("atm_channel_break_buffer_pct", 0.35))

    empty = {
        "ok": False,
        "bias": None,
        "signal": None,
        "side_action": None,
        "reason": "warming",
        "upper": None,
        "lower": None,
        "width": None,
        "slope": None,
        "pos_pct": None,
        "confidence": 0.0,
    }
    if len(candles) < max(25, lookback // 3):
        return empty

    window = candles[-lookback:]
    highs = [float(c["high"]) for c in window]
    lows = [float(c["low"]) for c in window]
    closes = [float(c["close"]) for c in window]
    n = len(window)
    last_i = n - 1
    px = closes[-1]

    ph = _pivot_highs(highs, strength)
    pl = _pivot_lows(lows, strength)
    ph = ph[-8:]
    pl = pl[-8:]

    up = _fit_line(ph) if len(ph) >= 2 else None
    lo = _fit_line(pl) if len(pl) >= 2 else None

    # Fallback: regression envelope on rolling highs/lows (works when pivots sparse)
    if up is None or lo is None:
        hi_pts = [(i, highs[i]) for i in range(0, n, max(1, n // 12))]
        lo_pts = [(i, lows[i]) for i in range(0, n, max(1, n // 12))]
        # Also include window extremes
        hi_pts.append((max(range(n), key=lambda i: highs[i]), max(highs)))
        lo_pts.append((min(range(n), key=lambda i: lows[i]), min(lows)))
        if up is None:
            up = _fit_line(hi_pts)
        if lo is None:
            lo = _fit_line(lo_pts)

    if up is None or lo is None:
        return {**empty, "reason": "need pivots"}

    us, ui = up
    ls, li = lo
    # Force parallel rails (shared slope) when fits diverge — matches drawn channels
    if abs(us - ls) > max_slope_diff * max(abs(us), abs(ls), 0.01) or us * ls < 0:
        slope_shared = (us + ls) / 2.0
        ui = sum(highs[i] - slope_shared * i for i in range(n)) / n
        li = sum(lows[i] - slope_shared * i for i in range(n)) / n
        # Keep a positive width: upper intercept above lower
        if ui < li:
            ui, li = li, ui
        us = ls = slope_shared

    slope = (us + ls) / 2.0
    if abs(slope) < min_abs_slope:
        return {**empty, "reason": "flat channel"}

    bias = "ASC" if slope > 0 else "DESC"
    upper_now = _line_y(us, ui, last_i)
    lower_now = _line_y(ls, li, last_i)
    if upper_now <= lower_now:
        upper_now, lower_now = max(upper_now, lower_now), min(upper_now, lower_now)

    width = upper_now - lower_now
    mid = (upper_now + lower_now) / 2.0
    if mid <= 0 or width <= 0:
        return {**empty, "reason": "bad width"}
    width_pct = 100.0 * width / mid
    if width_pct < min_width_pct or width_pct > max_width_pct:
        return {**empty, "reason": f"width {width_pct:.1f}%"}

    tol = max(width * 0.25, px * 0.004)

    def _near(y: float, line_y: float) -> bool:
        return abs(y - line_y) <= tol

    up_touches = sum(1 for i, h in ph if _near(h, _line_y(us, ui, float(i)))) if ph else 0
    lo_touches = sum(1 for i, lv in pl if _near(lv, _line_y(ls, li, float(i)))) if pl else 0
    # Envelope fallback counts bars near rails
    if up_touches + lo_touches < 2:
        up_touches = sum(
            1 for i in range(n) if _near(highs[i], _line_y(us, ui, float(i)))
        )
        lo_touches = sum(
            1 for i in range(n) if _near(lows[i], _line_y(ls, li, float(i)))
        )
        # Scale down to "touch-like" counts
        up_touches = min(8, max(2, up_touches // max(1, n // 10)))
        lo_touches = min(8, max(2, lo_touches // max(1, n // 10)))

    min_each = max(2, min_touches - 1)
    if up_touches < min_each or lo_touches < min_each:
        return {
            **empty,
            "reason": f"touches U{up_touches}/L{lo_touches}",
            "bias": bias,
            "slope": round(slope, 4),
            "upper": round(upper_now, 2),
            "lower": round(lower_now, 2),
            "pos_pct": round(
                100.0 * (px - lower_now) / width if width else 50.0, 1
            ),
            "price": round(px, 2),
        }

    # Position in channel 0=bottom 100=top
    pos_pct = 100.0 * (px - lower_now) / width if width else 50.0
    break_buf = width * (break_buf_pct / 100.0) + px * 0.001
    edge_band = max(edge_pct, 8.0)

    signal = None
    side_action = None  # BUY_EDGE | BUY_BREAK | EXIT_BREAK | HOLD_TRAIL
    reason = f"{bias} channel"

    if px > upper_now + break_buf:
        signal = "BREAK_UP"
        if bias == "ASC":
            side_action = "BUY_BREAK"
            reason = "ASC break ↑ — huge/fast long"
        else:
            side_action = "EXIT_BREAK"
            reason = "DESC false break ↑ — leave / skip"
    elif px < lower_now - break_buf:
        signal = "BREAK_DOWN"
        if bias == "DESC":
            side_action = "EXIT_BREAK"
            reason = "DESC break ↓ — huge/fast leave"
        else:
            side_action = "EXIT_BREAK"
            reason = "ASC break ↓ — leave long"
    elif pos_pct <= edge_band:
        signal = "EDGE_BOTTOM"
        side_action = "BUY_EDGE"
        reason = f"{bias} bottom edge → trail to top"
    elif pos_pct >= (100.0 - edge_band):
        signal = "EDGE_TOP"
        if bias == "DESC":
            # Short fade: we don't sell options — mark watch; trail target = bottom
            side_action = "WATCH_SHORT"
            reason = "DESC top edge — short fade watch"
        else:
            side_action = "HOLD_TRAIL"
            reason = "ASC top edge — take / trail"
    else:
        signal = "IN_CHANNEL"
        side_action = "HOLD_TRAIL" if bias == "ASC" else "WATCH"
        reason = f"{bias} mid channel"

    # Confidence from touches + parallelism + slope clarity
    conf = 0.45
    conf += min(0.25, 0.05 * (up_touches + lo_touches))
    conf += min(0.15, abs(slope) * 2)
    if abs(us - ls) < 0.05:
        conf += 0.1
    conf = max(0.0, min(0.95, conf))

    return {
        "ok": True,
        "bias": bias,
        "signal": signal,
        "side_action": side_action,
        "reason": reason,
        "upper": round(upper_now, 2),
        "lower": round(lower_now, 2),
        "width": round(width, 2),
        "width_pct": round(width_pct, 2),
        "slope": round(slope, 4),
        "pos_pct": round(pos_pct, 1),
        "confidence": round(conf, 3),
        "up_touches": up_touches,
        "lo_touches": lo_touches,
        "price": round(px, 2),
        "target_px": round(
            _target_for_entry(
                px, upper_now, width, is_break=(side_action == "BUY_BREAK")
            )
            if side_action in ("BUY_EDGE", "BUY_BREAK", "HOLD_TRAIL")
            else lower_now,
            2,
        ),
        "stop_px": round(
            lower_now - break_buf
            if side_action in ("BUY_EDGE", "BUY_BREAK")
            else upper_now + break_buf,
            2,
        ),
    }


def pick_atm_legs(
    candidates: list[dict[str, Any]],
    spot: float | None,
    cfg: dict[str, Any],
    *,
    pair: dict[str, Any] | None = None,
    atm_hint: float | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Pick nearest ATM CE and PE from scanner candidates (+ optional pair)."""
    step = float(cfg.get("atm_channel_strike_step", 50))
    atm = atm_hint
    if atm is None and spot:
        atm = round(float(spot) / step) * step
    best: dict[str, dict[str, Any] | None] = {"CE": None, "PE": None}
    best_dist = {"CE": 1e18, "PE": 1e18}
    pool = list(candidates or [])
    if pair:
        for key, side in (("ce", "CE"), ("pe", "PE")):
            leg = pair.get(key)
            if isinstance(leg, dict) and leg.get("tradingsymbol"):
                pool.append({**leg, "instrument_type": side})
    for c in pool:
        side = str(c.get("instrument_type") or "").upper()
        if side not in ("CE", "PE"):
            continue
        strike = float(c.get("strike") or 0)
        dist = abs(strike - atm) if atm else 0.0
        score = dist
        if score < best_dist[side]:
            best_dist[side] = score
            best[side] = c
    return best


def fetch_true_atm_legs(spot: float, cfg: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Resolve exact ATM CE/PE for current trade week (Nifty NFO).

    Uses cached instruments dump — safe to call from a background thread only.
    """
    from foxtrade.scanner import (
        _as_date,
        expiry_allowed_for_entry,
        trade_expiries_for_scan,
    )
    from kite_client import get_authenticated_kite

    out: dict[str, dict[str, Any] | None] = {"CE": None, "PE": None}
    try:
        step = float(cfg.get("atm_channel_strike_step", 50))
        atm = round(float(spot) / step) * step
        today = datetime.now(IST).date()
        nifty = _get_nifty_opts_cached()
        if not nifty:
            return out
        _cur, _nxt, trade_exps, _exp_day = trade_expiries_for_scan(nifty, today)
        if not trade_exps:
            return out
        exp = min(trade_exps)
        subset = [
            i
            for i in nifty
            if _as_date(i["expiry"]) == exp and float(i["strike"]) == float(atm)
        ]
        keys = [f"NFO:{i['tradingsymbol']}" for i in subset]
        quotes: dict[str, Any] = {}
        if keys:
            kite = get_authenticated_kite()
            quotes = kite.ltp(keys)
        for i in subset:
            side = str(i["instrument_type"]).upper()
            if side not in ("CE", "PE"):
                continue
            sym = str(i["tradingsymbol"])
            exp_d = _as_date(i["expiry"])
            if not expiry_allowed_for_entry(exp_d, today, symbol=sym):
                continue
            lp = quotes.get(f"NFO:{sym}", {}).get("last_price")
            out[side] = {
                "tradingsymbol": sym,
                "instrument_token": int(i["instrument_token"]),
                "instrument_type": side,
                "strike": float(i["strike"]),
                "expiry": str(exp_d),
                "ltp": float(lp) if lp is not None else None,
                "exchange": "NFO",
                "in_core_band": True,
            }
    except Exception:
        return out
    return out


def seed_minute_ohlc(token: int) -> list[dict[str, float]]:
    """Today's 1m OHLC for an option token (channel warm-start). Background only."""
    from kite_client import get_authenticated_kite

    try:
        kite = get_authenticated_kite()
        now = datetime.now(IST)
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now < start:
            return []
        rows = kite.historical_data(int(token), start, now, "minute", continuous=False, oi=False)
        out: list[dict[str, float]] = []
        for row in rows or []:
            dt = row["date"]
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            else:
                dt = dt.replace(tzinfo=IST)
            out.append(
                {
                    "time": float(int(dt.timestamp())),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        return out
    except Exception:
        return []


@dataclass
class AtmChannelState:
    last_message: str = "idle"
    phase: str = "SCAN"
    legs: dict[str, Any] = field(default_factory=dict)
    last_signals: dict[str, Any] = field(default_factory=dict)
    open_trade_id: str | None = None
    open_side: str | None = None
    mode: str | None = None  # EDGE | BREAK


class AtmChannelBook:
    """Parallel ATM CE/PE channel scan + optional entries/exits.

    Heavy Kite calls (instruments dump, historical seed) run on daemon threads
    so the engine heartbeat never stalls.
    """

    def __init__(self) -> None:
        self.state = AtmChannelState()
        self._candles: dict[str, list[dict[str, float]]] = {}
        self._candles_lock = threading.Lock()
        self._seeded: set[str] = set()
        self._seed_inflight: set[str] = set()
        self._last_atm_fetch: float = 0.0
        self._atm_cache: dict[str, dict[str, Any] | None] = {"CE": None, "PE": None}
        self._atm_lock = threading.Lock()
        self._fetch_inflight = False
        self._bg_lock = threading.Lock()
        self._reentry_until: dict[str, float] = {}
        # side -> {action, since, count} — wait for stable setup before entry
        self._signal_persist: dict[str, dict[str, Any]] = {}

    def status_line(self) -> str:
        bits = []
        for side in ("CE", "PE"):
            d = (self.state.last_signals or {}).get(side) or {}
            if not d:
                bits.append(f"{side}—")
                continue
            if not d.get("ok"):
                bias = d.get("bias") or ""
                bits.append(f"{side} {bias} {d.get('reason') or '—'}".strip())
                continue
            bits.append(
                f"{side} {d.get('bias')} {d.get('signal')} "
                f"pos{d.get('pos_pct')}% {d.get('side_action')}"
            )
        return " · ".join(bits) or self.state.last_message

    def instruments_to_watch(self) -> list[dict[str, Any]]:
        """ATM CE/PE for market hub LTP watch."""
        out: list[dict[str, Any]] = []
        for leg in (self._atm_cache or {}).values():
            if isinstance(leg, dict) and leg.get("tradingsymbol"):
                out.append(dict(leg))
        return out

    def _bg_fetch_atm(self, spot: float, cfg: dict[str, Any]) -> None:
        try:
            fetched = fetch_true_atm_legs(float(spot), cfg)
            with self._atm_lock:
                for s in ("CE", "PE"):
                    if fetched.get(s):
                        self._atm_cache[s] = fetched[s]
                self._last_atm_fetch = time.time()
        except Exception:
            pass
        finally:
            with self._bg_lock:
                self._fetch_inflight = False

    def _bg_seed_sym(self, sym: str, token: int) -> None:
        try:
            seeded = seed_minute_ohlc(int(token))
            if seeded:
                with self._candles_lock:
                    existing = list(self._candles.get(sym) or [])
                    by_t = {int(c["time"]): c for c in seeded}
                    for c in existing:
                        by_t[int(c["time"])] = c
                    self._candles[sym] = [by_t[k] for k in sorted(by_t)][-400:]
            self._seeded.add(sym)
        except Exception:
            self._seeded.add(sym)  # don't retry forever on hard fail
        finally:
            self._seed_inflight.discard(sym)

    def _ensure_atm_legs(
        self,
        *,
        cfg: dict[str, Any],
        candidates: list[dict[str, Any]],
        spot: float | None,
        pair: dict[str, Any] | None,
        atm_hint: float | None,
    ) -> dict[str, dict[str, Any] | None]:
        legs = pick_atm_legs(
            candidates, spot, cfg, pair=pair, atm_hint=atm_hint
        )
        with self._atm_lock:
            for s in ("CE", "PE"):
                if legs.get(s):
                    self._atm_cache[s] = legs[s]
                elif self._atm_cache.get(s):
                    legs[s] = self._atm_cache[s]
            last_fetch = self._last_atm_fetch

        need_fetch = any(legs.get(s) is None for s in ("CE", "PE"))
        now = time.time()
        refresh_due = now - last_fetch > 180
        if spot and (need_fetch or refresh_due) and now - last_fetch > 30:
            with self._bg_lock:
                if not self._fetch_inflight:
                    self._fetch_inflight = True
                    threading.Thread(
                        target=self._bg_fetch_atm,
                        args=(float(spot), dict(cfg)),
                        name="atm-ch-fetch",
                        daemon=True,
                    ).start()
        return legs

    def _candles_for(
        self,
        *,
        sym: str,
        token: int,
        quote: float | None,
        series: list[dict[str, float]],
        cfg: dict[str, Any],
    ) -> list[dict[str, float]]:
        with self._candles_lock:
            candles = list(self._candles.get(sym) or [])

        # Kick history seed in background — never block the engine tick
        if (
            cfg.get("atm_channel_seed_history", True)
            and sym not in self._seeded
            and sym not in self._seed_inflight
            and token > 0
            and len(candles) < 40
        ):
            self._seed_inflight.add(sym)
            threading.Thread(
                target=self._bg_seed_sym,
                args=(sym, int(token)),
                name=f"atm-ch-seed-{sym[-6:]}",
                daemon=True,
            ).start()

        live = ticks_to_ohlc_1m(series)
        if live:
            by_t = {int(c["time"]): c for c in candles}
            for c in live:
                by_t[int(c["time"])] = c
            candles = [by_t[k] for k in sorted(by_t)]
        if quote is not None:
            now_m = int(datetime.now(IST).timestamp())
            now_m = now_m - (now_m % 60)
            if candles and int(candles[-1]["time"]) == now_m:
                candles[-1]["close"] = float(quote)
                candles[-1]["high"] = max(float(candles[-1]["high"]), float(quote))
                candles[-1]["low"] = min(float(candles[-1]["low"]), float(quote))
            else:
                candles.append(
                    {
                        "time": float(now_m),
                        "open": float(quote),
                        "high": float(quote),
                        "low": float(quote),
                        "close": float(quote),
                    }
                )
        if len(candles) > 400:
            candles = candles[-400:]
        with self._candles_lock:
            self._candles[sym] = candles
        return candles

    def on_tick(
        self,
        *,
        cfg: dict[str, Any],
        quotes: dict[str, float],
        candidates: list[dict[str, Any]],
        spot: float | None,
        option_series_fn: Callable[[str], list[dict[str, float]]],
        mode: str,
        notifications: list[dict[str, Any]] | None = None,
        pair: dict[str, Any] | None = None,
        atm_hint: float | None = None,
    ) -> None:
        if not channel_enabled(cfg):
            self.state.phase = "OFF"
            self.state.last_message = "atm_channel disabled"
            return

        try:
            from foxtrade.learner import apply_learned_weights

            cfg = apply_learned_weights(cfg)
        except Exception:
            pass

        legs = self._ensure_atm_legs(
            cfg=cfg,
            candidates=candidates,
            spot=spot,
            pair=pair,
            atm_hint=atm_hint,
        )
        self.state.legs = {
            k: {
                "symbol": (v or {}).get("tradingsymbol"),
                "strike": (v or {}).get("strike"),
                "ltp": (v or {}).get("ltp"),
                "token": (v or {}).get("instrument_token"),
                "expiry": (v or {}).get("expiry"),
            }
            for k, v in legs.items()
        }

        signals: dict[str, Any] = {}
        for side, leg in legs.items():
            if not leg or not leg.get("tradingsymbol"):
                signals[side] = {"ok": False, "reason": "no ATM leg"}
                continue
            sym = str(leg["tradingsymbol"])
            series = option_series_fn(sym) or []
            q = quotes.get(sym)
            if q is None and leg.get("ltp") is not None:
                q = float(leg["ltp"])
            candles = self._candles_for(
                sym=sym,
                token=int(leg.get("instrument_token") or 0),
                quote=float(q) if q is not None else None,
                series=series,
                cfg=cfg,
            )
            det = detect_parallel_channel(candles, cfg)
            det["symbol"] = sym
            det["bars"] = len(candles)
            signals[side] = det

        # Cross-leg: descending break on one side → huge/fast long on the other (buy-only)
        ce_s = signals.get("CE") or {}
        pe_s = signals.get("PE") or {}
        if (
            ce_s.get("ok")
            and ce_s.get("bias") == "DESC"
            and ce_s.get("signal") == "BREAK_DOWN"
            and legs.get("PE")
            and pe_s.get("side_action") not in ("BUY_BREAK", "BUY_EDGE")
        ):
            signals["PE"] = {
                **pe_s,
                "ok": True,
                "bias": pe_s.get("bias") or "ASC",
                "side_action": "BUY_BREAK",
                "signal": "OPP_CE_BREAK_DOWN",
                "reason": "CE DESC break ↓ → PE huge/fast long",
                "confidence": max(float(pe_s.get("confidence") or 0), 0.72),
                "symbol": (legs["PE"] or {}).get("tradingsymbol"),
                "upper": pe_s.get("upper") or float((legs["PE"] or {}).get("ltp") or 0) * 1.05,
                "lower": pe_s.get("lower") or float((legs["PE"] or {}).get("ltp") or 0) * 0.97,
                "stop_px": pe_s.get("stop_px")
                or float((legs["PE"] or {}).get("ltp") or 0) * 0.978,
                "target_px": pe_s.get("target_px")
                or float((legs["PE"] or {}).get("ltp") or 0) * 1.05,
            }
        if (
            pe_s.get("ok")
            and pe_s.get("bias") == "DESC"
            and pe_s.get("signal") == "BREAK_DOWN"
            and legs.get("CE")
            and ce_s.get("side_action") not in ("BUY_BREAK", "BUY_EDGE")
        ):
            signals["CE"] = {
                **ce_s,
                "ok": True,
                "bias": ce_s.get("bias") or "ASC",
                "side_action": "BUY_BREAK",
                "signal": "OPP_PE_BREAK_DOWN",
                "reason": "PE DESC break ↓ → CE huge/fast long",
                "confidence": max(float(ce_s.get("confidence") or 0), 0.72),
                "symbol": (legs["CE"] or {}).get("tradingsymbol"),
                "upper": ce_s.get("upper") or float((legs["CE"] or {}).get("ltp") or 0) * 1.05,
                "lower": ce_s.get("lower") or float((legs["CE"] or {}).get("ltp") or 0) * 0.97,
                "stop_px": ce_s.get("stop_px")
                or float((legs["CE"] or {}).get("ltp") or 0) * 0.978,
                "target_px": ce_s.get("target_px")
                or float((legs["CE"] or {}).get("ltp") or 0) * 1.05,
            }

        self.state.last_signals = signals
        self.state.last_message = self.status_line()
        self.state.phase = "SCAN"

        # Manage open channel trades first (huge/fast leave on adverse break)
        self._manage_open(cfg=cfg, quotes=quotes, signals=signals, notifications=notifications)

        # Entries: prefer BREAK over EDGE when both fire; scan CE+PE
        if not bool(cfg.get("atm_channel_entries", True)):
            return
        ok, why = can_open_new(ALGO_ID, cfg=cfg)
        if not ok:
            self.state.last_message = f"{self.status_line()} · blocked:{why[:48]}"
            return

        # Daily spam cap — stop digging after N channel scratches
        max_day = int(cfg.get("atm_channel_max_trades_per_day", 4))
        if max_day > 0:
            today = datetime.now(IST).strftime("%Y-%m-%d")
            day_n = sum(
                1
                for t in trade_store.list_trades()
                if str(t.get("algo") or "") == ALGO_ID
                and str(t.get("opened_at") or "").startswith(today)
                and str(t.get("ledger_kind") or "") != "live_attempt"
            )
            if day_n >= max_day:
                self.state.last_message = (
                    f"{self.status_line()} · day cap {day_n}/{max_day}"
                )
                return

        prefer_break = bool(cfg.get("atm_channel_prefer_break", True))
        allow_edge = bool(cfg.get("atm_channel_allow_edge", False))
        # EDGE must prove itself; BREAK still needs a short confirm
        edge_need = max(1, int(cfg.get("atm_channel_edge_persist_ticks", 3)))
        break_need = max(1, int(cfg.get("atm_channel_break_persist_ticks", 2)))
        ranked: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
        now_ts = time.time()
        for side, det in signals.items():
            if not det.get("ok"):
                self._signal_persist.pop(side, None)
                continue
            action = det.get("side_action")
            leg = legs.get(side)
            if not leg:
                continue
            if action not in ("BUY_BREAK", "BUY_EDGE"):
                self._signal_persist.pop(side, None)
                continue
            if action == "BUY_EDGE" and not allow_edge:
                self._signal_persist.pop(side, None)
                continue
            # Reject EDGE while break-leave is the live signal family (chop at floor)
            if action == "BUY_EDGE" and det.get("bias") == "ASC":
                pos = float(det.get("pos_pct") or 50)
                # Too deep under edge band with no bounce → wait
                if pos < 2.0:
                    self._signal_persist.pop(side, None)
                    continue
            need = break_need if action == "BUY_BREAK" else edge_need
            prev = self._signal_persist.get(side) or {}
            if prev.get("action") == action:
                ticks = int(prev.get("ticks") or 0) + 1
                since = float(prev.get("since") or now_ts)
            else:
                ticks = 1
                since = now_ts
            self._signal_persist[side] = {
                "action": action,
                "ticks": ticks,
                "since": since,
            }
            if ticks < need:
                continue
            if action == "BUY_BREAK":
                ranked.append((0 if prefer_break else 1, side, det, leg))
            else:
                ranked.append((1 if prefer_break else 0, side, det, leg))
        ranked.sort(key=lambda x: (x[0], -float(x[2].get("confidence") or 0)))
        if not ranked:
            waiting = {
                s: p
                for s, p in self._signal_persist.items()
                if p.get("action") in ("BUY_BREAK", "BUY_EDGE")
            }
            if waiting:
                bits = [
                    f"{s} {p.get('action')} {p.get('ticks')}t"
                    for s, p in waiting.items()
                ]
                self.state.last_message = (
                    f"{self.status_line()} · waiting confirm {' · '.join(bits)}"
                )
            return

        _prio, side, det, leg = ranked[0]
        sym_cd = str(leg.get("tradingsymbol") or "")
        if sym_cd and (
            time.time() < self._reentry_until.get(sym_cd, 0)
            or _reentry_blocked(sym_cd)
        ):
            self.state.last_message = f"{self.status_line()} · reentry cooldown"
            return
        # Consume persist so we don't instantly re-fire after a fill/reject
        self._signal_persist.pop(side, None)
        self._enter(
            cfg=cfg,
            mode=mode,
            side=side,
            det=det,
            leg=leg,
            quotes=quotes,
            notifications=notifications,
        )

    def _manage_open(
        self,
        *,
        cfg: dict[str, Any],
        quotes: dict[str, float],
        signals: dict[str, Any],
        notifications: list[dict[str, Any]] | None,
    ) -> None:
        opens = open_for_algo(ALGO_ID)
        if not opens:
            self.state.open_trade_id = None
            return
        lot_size = int(cfg.get("lot_size", 75))
        for trade in opens:
            tid = str(trade.get("id") or "")
            sym = str(trade.get("symbol") or "")
            side = str(trade.get("side") or "")
            px = float(quotes.get(sym) or trade.get("live_price") or trade.get("entry_price") or 0)
            if px <= 0:
                continue
            # Keep live mark
            try:
                trade_store.update_trade(
                    tid,
                    {
                        "live_price": px,
                        "live_pnl_rupees": round(
                            (px - float(trade["entry_price"])) * int(trade.get("qty") or 0),
                            2,
                        ),
                    },
                )
            except Exception:
                pass

            feats = trade.get("entry_features") or {}
            target = float(feats.get("channel_target") or trade.get("target_px") or 0)
            stop = float(feats.get("channel_stop") or trade.get("stop_px") or 0)
            entry_px = float(trade.get("entry_price") or 0)
            entry_mode = str(feats.get("channel_mode") or "EDGE")
            det = signals.get(side) or {}
            # Unknown open time → treat as just filled so min-hold still applies.
            # (Old default 999 skipped the hold gate and caused 1s scratch loops.)
            held_min = 0.0
            opened = trade.get("opened_at")
            if opened:
                try:
                    opened_dt = datetime.fromisoformat(str(opened))
                    if opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=IST)
                    held_min = max(
                        0.0,
                        (datetime.now(IST) - opened_dt).total_seconds() / 60.0,
                    )
                except Exception:
                    held_min = 0.0
            try:
                conf = float(feats.get("confidence")) if feats.get("confidence") is not None else None
            except (TypeError, ValueError):
                conf = None
            ch_ex = atm_channel_should_exit(
                px=px,
                entry_px=entry_px,
                peak=float(trade.get("peak_px") or px),
                stop_px=stop,
                target_px=target,
                channel_mode=entry_mode,
                side_det=det,
                cfg=cfg,
                held_minutes=held_min,
                confidence=conf,
                side=side,
            )
            # Keep peak updated while holding
            if ch_ex.get("peak") is not None:
                try:
                    trade_store.update_trade(tid, {"peak_px": float(ch_ex["peak"])})
                except Exception:
                    pass
            reason = (
                str(ch_ex.get("reason") or "")
                if ch_ex.get("action") == "exit"
                else None
            )

            if reason:
                closed = broker.close_long(tid, px, reason, lot_size)
                # Dedicated ATM cooldown (persisted) — global entry_cooldown alone
                # was memory-only and vanished on soft-restart → instant re-spam.
                cd_min = float(
                    cfg.get("atm_channel_reentry_minutes")
                    or cfg.get("entry_cooldown_minutes")
                    or 15
                )
                # Scratches / break-leaves get a longer ban
                if "leave" in reason.lower() or "SL" in reason:
                    cd_min = max(cd_min, float(cfg.get("atm_channel_scratch_cooldown_minutes", 20)))
                cd_sec = max(60.0, cd_min * 60.0)
                if sym:
                    self._reentry_until[sym] = time.time() + cd_sec
                    _set_reentry_cooldown(sym, cd_sec)
                if closed and notifications is not None:
                    notifications.append(
                        {
                            "ts": datetime.now(IST).isoformat(),
                            "level": "info",
                            "message": f"ATM channel EXIT {sym}: {reason}",
                        }
                    )
                self.state.open_trade_id = None
                self.state.phase = "FLAT"
                self.state.last_message = f"exited {sym}: {reason}"

    def _enter(
        self,
        *,
        cfg: dict[str, Any],
        mode: str,
        side: str,
        det: dict[str, Any],
        leg: dict[str, Any],
        quotes: dict[str, float],
        notifications: list[dict[str, Any]] | None,
    ) -> None:
        sym = str(leg.get("tradingsymbol") or "")
        px = float(quotes.get(sym) or leg.get("ltp") or 0)
        if not sym or px <= 0:
            return
        is_break = det.get("side_action") == "BUY_BREAK"
        base_lots = int(cfg.get("algo1_lots") or cfg.get("atm_channel_lots") or 1)
        if is_break:
            lots = max(
                base_lots,
                int(cfg.get("atm_channel_break_lots") or max(base_lots * 2, 2)),
            )
            lots = min(lots, int(cfg.get("atm_channel_break_max_lots", 8)))
            ch_mode = "BREAK"
        else:
            lots = max(1, int(cfg.get("atm_channel_edge_lots") or base_lots))
            lots = min(lots, int(cfg.get("atm_channel_edge_max_lots", 4)))
            ch_mode = "EDGE"

        sl_pct = float(
            cfg.get(
                "atm_channel_break_sl_pct" if is_break else "atm_channel_edge_sl_pct",
                2.0 if is_break else 1.5,
            )
        )
        # Floor: edge needs real room so noise cannot scratch in 1s
        min_dist = float(cfg.get("atm_channel_min_stop_dist_pct", 1.5))
        sl_pct = max(sl_pct, min_dist)
        stop = _long_stop(px, float(det.get("stop_px") or 0), sl_pct)
        target = _target_for_entry(
            px,
            float(det.get("upper") or 0),
            float(det.get("width") or 0),
            is_break=is_break,
        )

        lot_size = int(cfg.get("lot_size", 75))
        trade = broker.open_long(
            mode=mode,
            algo=ALGO_ID,
            strategy="AtmChannel",
            symbol=sym,
            side=side,
            entry_price=px,
            lots=lots,
            lot_size=lot_size,
            cfg=cfg,
            allow_add=False,
            extra={
                "expiry": leg.get("expiry"),
                "strike": leg.get("strike"),
                "instrument_token": int(leg.get("instrument_token") or 0),
                "underlying": "NIFTY",
                "note": det.get("reason"),
                "leg_role": "primary",
                "stop_px": stop,
                "target_px": target,
                "peak_px": px,
                "entry_features": {
                    "tags": ["atm_channel", det.get("bias"), det.get("signal"), ch_mode],
                    "channel_mode": ch_mode,
                    "channel_bias": det.get("bias"),
                    "channel_signal": det.get("signal"),
                    "channel_target": target,
                    "channel_stop": stop,
                    "channel_upper": det.get("upper"),
                    "channel_lower": det.get("lower"),
                    "confidence": det.get("confidence"),
                },
            },
        )
        if trade.get("status") != "OPEN":
            self.state.last_message = (
                f"channel entry rejected: {trade.get('message') or trade.get('note')}"
            )
            return
        self.state.open_trade_id = trade.get("id")
        self.state.open_side = side
        self.state.mode = ch_mode
        self.state.phase = "IN_TRADE"
        self.state.last_message = (
            f"ENTER {ch_mode} {side} {sym} @ {px:.1f} "
            f"SL {stop:.1f} TG {target:.1f} · {det.get('reason')}"
        )
        if notifications is not None:
            notifications.append(
                {
                    "ts": datetime.now(IST).isoformat(),
                    "level": "info",
                    "message": self.state.last_message,
                }
            )

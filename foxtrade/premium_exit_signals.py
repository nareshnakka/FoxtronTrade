"""
Breakout exit tells (practical, not rare 2.5×):

1. Premium band 165–175 on the winner
2. Fallen-leg reversal (hammer / reverse-hammer) on 30s or 1m micro-candles,
   then breakout of that candle's high/low → book the winner
"""

from __future__ import annotations

from typing import Any


def build_ohlc_bars(
    ticks: list[dict[str, Any]],
    *,
    period_sec: float = 30.0,
    price_key: str = "px",
) -> list[dict[str, Any]]:
    """Bucket timed ticks into OHLC bars."""
    if not ticks or period_sec <= 0:
        return []
    bars: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    bucket_start: float | None = None
    for t in ticks:
        ts = float(t.get("t") or 0)
        px = t.get(price_key)
        if px is None or ts <= 0:
            continue
        px = float(px)
        if bucket_start is None:
            bucket_start = ts
        if ts - bucket_start >= period_sec and bucket:
            bars.append(_ohlc(bucket, bucket_start, period_sec))
            bucket = []
            bucket_start = ts
        bucket.append({"t": ts, "px": px})
    # do not emit incomplete last bar for pattern (need closed candle)
    return bars


def _ohlc(bucket: list[dict[str, Any]], start: float, period: float) -> dict[str, Any]:
    xs = [float(b["px"]) for b in bucket]
    return {
        "t0": start,
        "t1": start + period,
        "o": xs[0],
        "h": max(xs),
        "l": min(xs),
        "c": xs[-1],
        "n": len(xs),
    }


def is_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Bullish hammer: long lower wick, small body near top."""
    body = abs(c - o)
    full = h - l
    if full <= 0.5:
        return False
    lower = min(o, c) - l
    upper = h - max(o, c)
    return lower >= body * 2.0 and lower >= full * 0.55 and upper <= body * 1.2


def is_inverted_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Reverse hammer at lows: long upper wick, small body near bottom."""
    body = abs(c - o)
    full = h - l
    if full <= 0.5:
        return False
    lower = min(o, c) - l
    upper = h - max(o, c)
    return upper >= body * 2.0 and upper >= full * 0.55 and lower <= body * 1.2


def is_shooting_star(o: float, h: float, l: float, c: float) -> bool:
    """Bearish at highs: long upper wick, close weak."""
    body = abs(c - o)
    full = h - l
    if full <= 0.5:
        return False
    lower = min(o, c) - l
    upper = h - max(o, c)
    return upper >= body * 2.0 and upper >= full * 0.55 and lower <= body * 1.2 and c <= o


def detect_fallen_reversal_candle(
    bars: list[dict[str, Any]],
    *,
    fallen_side: str,
) -> dict[str, Any] | None:
    """
    On fallen PE (crushing): hammer / inverted hammer → arm break above candle high.
    On fallen CE: shooting star → arm break below candle low.
    """
    if len(bars) < 1:
        return None
    b = bars[-1]
    o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
    if fallen_side == "PE":
        if is_hammer(o, h, l, c) or is_inverted_hammer(o, h, l, c):
            kind = "hammer" if is_hammer(o, h, l, c) else "inverted_hammer"
            return {
                "ok": True,
                "kind": kind,
                "fallen_side": "PE",
                "break_dir": "up",
                "level": round(h, 2),
                "candle": {"o": o, "h": h, "l": l, "c": c},
                "t1": b.get("t1"),
            }
    else:
        if is_shooting_star(o, h, l, c) or (
            is_inverted_hammer(o, h, l, c) and c < o
        ):
            kind = "shooting_star" if is_shooting_star(o, h, l, c) else "bearish_rev_hammer"
            return {
                "ok": True,
                "kind": kind,
                "fallen_side": "CE",
                "break_dir": "down",
                "level": round(l, 2),
                "candle": {"o": o, "h": h, "l": l, "c": c},
                "t1": b.get("t1"),
            }
    return None


def fallen_reversal_broken(
    arm: dict[str, Any] | None,
    *,
    fallen_px: float,
) -> bool:
    if not arm or not arm.get("ok"):
        return False
    lvl = float(arm["level"])
    if arm.get("break_dir") == "up":
        return fallen_px > lvl
    if arm.get("break_dir") == "down":
        return fallen_px < lvl
    return False


def premium_target_hit(
    px: float,
    *,
    target_low: float = 168.0,
    target_high: float = 175.0,
    fallen_tl_broke: bool | None = None,
    require_fallen_tl: bool = False,
) -> str | None:
    """
    Book band (default 168–175).
    - Always book at/above target_high.
    - Soft band (target_low..target_high): if require_fallen_tl, only when
      fallen_tl_broke is True (falling leg broke its channel TL).
    """
    if px >= target_high:
        return f"premium target high {target_high:g}"
    if px >= target_low:
        if require_fallen_tl and not fallen_tl_broke:
            return None
        if require_fallen_tl and fallen_tl_broke:
            return (
                f"premium {target_low:g}–{target_high:g} + fallen TL break"
            )
        return f"premium target {target_low:g}–{target_high:g}"
    return None


def dual_book_winner(
    ce_px: float,
    pe_px: float,
    ce_entry: float,
    pe_entry: float,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Simple dual playbook:
      wait convergence → hold both → when ONE leg hits the top (165–175),
      book that winner; hold the opposite for retrace exit.
    Returns {side, reason, ce_move, pe_move} or None.
    """
    cfg = cfg or {}
    if not cfg.get("breakout_premium_target_enabled", True):
        return None
    t_lo = float(cfg.get("breakout_premium_target_low", 165))
    t_hi = float(cfg.get("breakout_premium_target_high", 175))
    ce_m = float(ce_px) - float(ce_entry)
    pe_m = float(pe_px) - float(pe_entry)
    ce_hit = premium_target_hit(float(ce_px), target_low=t_lo, target_high=t_hi)
    pe_hit = premium_target_hit(float(pe_px), target_low=t_lo, target_high=t_hi)

    def _pack(side: str, reason: str) -> dict[str, Any]:
        return {
            "side": side,
            "reason": reason,
            "ce_move": round(ce_m, 1),
            "pe_move": round(pe_m, 1),
        }

    # Both at top → book the stronger leg
    if ce_hit and pe_hit:
        if float(ce_px) >= float(pe_px):
            return _pack("CE", ce_hit)
        return _pack("PE", pe_hit)
    if ce_hit and float(ce_px) >= float(pe_px):
        return _pack("CE", ce_hit)
    if pe_hit and float(pe_px) >= float(ce_px):
        return _pack("PE", pe_hit)
    return None

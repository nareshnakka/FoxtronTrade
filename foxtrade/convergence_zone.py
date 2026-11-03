"""
Detect the real CE/PE convergence range from history.

Screening band (config premium_low/high) only picks which strikes to watch.
The convergence range is where CE and PE actually overlap / trade tightly
together (the yellow box on the chart) — envelope of both premiums while
|CE−PE| stays small for a sustained stretch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def detect_convergence_zone(
    ce_hist: list[float],
    pe_hist: list[float],
    *,
    gap_tol: float = 12.0,
    min_samples: int = 15,
) -> dict[str, Any] | None:
    """
    Find best sustained window where CE≈PE, return price envelope of both legs.

    Returns e.g. {low, high, mid, samples, gap_avg, active, source: "detected"}
    """
    n = min(len(ce_hist), len(pe_hist))
    if n < min_samples:
        return None

    best: dict[str, Any] | None = None
    best_score = -1.0
    i = 0
    while i < n:
        if abs(ce_hist[i] - pe_hist[i]) > gap_tol:
            i += 1
            continue
        j = i
        while j < n and abs(ce_hist[j] - pe_hist[j]) <= gap_tol:
            j += 1
        length = j - i
        if length >= min_samples:
            ce_seg = ce_hist[i:j]
            pe_seg = pe_hist[i:j]
            vals = ce_seg + pe_seg
            gaps = [abs(ce_seg[k] - pe_seg[k]) for k in range(length)]
            # Prefer longer + tighter + more recent windows
            score = length * 1.0 - (sum(gaps) / length) + (j / n) * 15
            if score > best_score:
                best_score = score
                best = {
                    "low": round(min(vals), 1),
                    "high": round(max(vals), 1),
                    "mid": round(sum(vals) / len(vals), 1),
                    "samples": length,
                    "gap_avg": round(sum(gaps) / length, 2),
                    "start_i": i,
                    "end_i": j - 1,
                    "gap_tol": gap_tol,
                    "source": "detected",
                }
        i = max(j, i + 1)

    if not best:
        # Fallback: use recent samples where gap was relatively tight (best 20%)
        pairs = [(ce_hist[k], pe_hist[k], abs(ce_hist[k] - pe_hist[k])) for k in range(n)]
        pairs.sort(key=lambda x: x[2])
        take = max(min_samples, n // 5)
        tight = pairs[:take]
        if not tight:
            return None
        vals = [p for a, b, _ in tight for p in (a, b)]
        gap_avg = sum(g for _, _, g in tight) / len(tight)
        if gap_avg > gap_tol * 2:
            return None
        best = {
            "low": round(min(vals), 1),
            "high": round(max(vals), 1),
            "mid": round(sum(vals) / len(vals), 1),
            "samples": len(tight),
            "gap_avg": round(gap_avg, 2),
            "start_i": 0,
            "end_i": n - 1,
            "gap_tol": gap_tol,
            "source": "detected_tightest",
        }

    last_gap = abs(ce_hist[-1] - pe_hist[-1])
    last_mid = (ce_hist[-1] + pe_hist[-1]) / 2
    best["active"] = last_gap <= gap_tol and best["low"] - 2 <= last_mid <= best["high"] + 2
    best["last_gap"] = round(last_gap, 2)
    # Pad slightly so band reads like the drawn yellow box
    pad = max(1.0, (best["high"] - best["low"]) * 0.02)
    best["low"] = round(best["low"] - pad, 1)
    best["high"] = round(best["high"] + pad, 1)
    return best


def in_convergence_zone(
    ce_ltp: float,
    pe_ltp: float,
    zone: dict[str, Any] | None,
    *,
    entry_tol: float = 3.0,
) -> bool:
    """True when CE/PE are tight and mid sits inside detected zone."""
    if not zone:
        return False
    mid = (ce_ltp + pe_ltp) / 2
    gap_ok = abs(ce_ltp - pe_ltp) <= float(entry_tol)
    in_box = float(zone["low"]) <= mid <= float(zone["high"])
    # Also allow either leg still inside the box (overlap region)
    either_in = (
        float(zone["low"]) <= ce_ltp <= float(zone["high"])
        or float(zone["low"]) <= pe_ltp <= float(zone["high"])
    )
    return gap_ok and (in_box or either_in)


def _minute_closes(token: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    from kite_client import get_authenticated_kite

    kite = get_authenticated_kite()
    rows = kite.historical_data(int(token), start, end, "minute")
    out: list[tuple[datetime, float]] = []
    for row in rows:
        dt = row["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        out.append((dt, float(row["close"])))
    return out


def detect_zone_from_day_history(
    ce_token: int,
    pe_token: int,
    *,
    gap_tol: float = 12.0,
    min_samples: int = 15,
    live_ce: list[float] | None = None,
    live_pe: list[float] | None = None,
) -> dict[str, Any] | None:
    """
    Prefer live series; if already broken out, dig 09:15→now minute bars
    so the morning overlap box is still recoverable (as in your yellow range).
    """
    z = detect_convergence_zone(
        live_ce or [], live_pe or [], gap_tol=gap_tol, min_samples=min_samples
    )
    if z and z.get("source") == "detected":
        return z

    now = datetime.now(IST)
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < start:
        return z
    try:
        ce_rows = _minute_closes(ce_token, start, now)
        pe_rows = _minute_closes(pe_token, start, now)
    except Exception:
        return z

    pe_map = {dt.replace(second=0, microsecond=0): px for dt, px in pe_rows}
    ce_h: list[float] = []
    pe_h: list[float] = []
    for dt, px in ce_rows:
        key = dt.replace(second=0, microsecond=0)
        if key in pe_map:
            ce_h.append(px)
            pe_h.append(pe_map[key])

    dug = detect_convergence_zone(ce_h, pe_h, gap_tol=gap_tol, min_samples=min_samples)
    if dug:
        dug["source"] = "detected_day"
        dug["day_samples"] = len(ce_h)
        return dug
    return z or dug

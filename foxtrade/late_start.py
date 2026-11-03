"""
Late login catch-up (09:20 / 09:25 / 09:30+).

Dig today's minute history from 09:15, find which CE/PE pairs
converged or were closest, which decayed, and recommend the active set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite
from foxtrade.scanner import is_converged

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class PairDayPath:
    pair_id: str
    expiry: str
    strike: float | None
    ce: dict[str, Any]
    pe: dict[str, Any]
    min_gap: float
    converged: bool
    first_converge_ts: str | None = None
    last_gap: float = 0.0
    last_mid: float = 0.0
    ce_open: float | None = None
    pe_open: float | None = None
    ce_last: float | None = None
    pe_last: float | None = None
    decayed: bool = False
    expanded_side: str | None = None  # CE | PE
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class LateStartReport:
    late: bool
    minutes_since_open: int
    session_open: str
    paths: list[PairDayPath] = field(default_factory=list)
    recommended_id: str | None = None
    message: str = ""


def minutes_since_open(now: datetime | None = None) -> int:
    now = now or datetime.now(IST)
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now.date() != open_t.date() or now < open_t:
        return 0
    return int((now - open_t).total_seconds() // 60)


def is_late_start(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    # weekend / pre-open: not late trading start
    if now.weekday() >= 5:
        return False
    threshold = int(cfg.get("late_start_after_minutes", 5))
    return minutes_since_open(now) >= threshold


def _session_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(IST)
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now
    return start, end


def _fetch_minute_closes(token: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    kite = get_authenticated_kite()
    rows = kite.historical_data(token, start, end, "minute", continuous=False, oi=False)
    out: list[tuple[datetime, float]] = []
    for row in rows:
        dt = row["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        else:
            dt = dt.replace(tzinfo=IST)
        out.append((dt, float(row["close"])))
    return out


def dig_day_history(
    instruments: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> LateStartReport:
    """
    Replay today's 1-minute closes for watched legs and score CE/PE pairs.
    """
    now = datetime.now(IST)
    mins = minutes_since_open(now)
    open_t, end_t = _session_bounds(now)
    report = LateStartReport(
        late=mins >= int(cfg.get("late_start_after_minutes", 5)),
        minutes_since_open=mins,
        session_open=open_t.isoformat(),
    )
    if mins < 1:
        report.message = "at/near open — live tracking only"
        return report

    low = float(cfg["premium_low"])
    high = float(cfg["premium_high"])
    tol = float(cfg.get("convergence_tol", 2.0))

    # Fetch history per token (cap to avoid rate limits)
    max_legs = int(cfg.get("late_start_max_legs", 40))
    legs = instruments[:max_legs]
    hist: dict[str, list[tuple[datetime, float]]] = {}
    for inst in legs:
        try:
            hist[inst["tradingsymbol"]] = _fetch_minute_closes(
                int(inst["instrument_token"]), open_t, end_t
            )
        except Exception:
            continue

    # Build same-expiry same-strike pairs from instruments that have history
    by_key: dict[tuple[str, float], dict[str, dict[str, Any]]] = {}
    for inst in legs:
        if inst["tradingsymbol"] not in hist or not hist[inst["tradingsymbol"]]:
            continue
        key = (str(inst["expiry"]), float(inst["strike"]))
        by_key.setdefault(key, {})[inst["instrument_type"]] = inst

    paths: list[PairDayPath] = []
    for (exp, strike), sides in by_key.items():
        if "CE" not in sides or "PE" not in sides:
            continue
        ce, pe = sides["CE"], sides["PE"]
        ce_h = hist.get(ce["tradingsymbol"]) or []
        pe_h = hist.get(pe["tradingsymbol"]) or []
        if not ce_h or not pe_h:
            continue

        # Align by timestamp minute
        pe_map = {dt.replace(second=0, microsecond=0): px for dt, px in pe_h}
        min_gap = 1e9
        converged = False
        first_conv: str | None = None
        last_gap = 0.0
        last_mid = 0.0
        samples = 0
        in_band_samples = 0
        for dt, ce_px in ce_h:
            key_dt = dt.replace(second=0, microsecond=0)
            pe_px = pe_map.get(key_dt)
            if pe_px is None:
                continue
            samples += 1
            gap = abs(ce_px - pe_px)
            mid = (ce_px + pe_px) / 2
            last_gap, last_mid = gap, mid
            if low - 5 <= mid <= high + 5:
                in_band_samples += 1
            if gap < min_gap:
                min_gap = gap
            if is_converged(ce_px, pe_px, tol, low, high) and not converged:
                converged = True
                first_conv = key_dt.isoformat()

        if samples < 3:
            continue

        ce_open, ce_last = ce_h[0][1], ce_h[-1][1]
        pe_open, pe_last = pe_h[0][1], pe_h[-1][1]
        ce_ch = ce_last - ce_open
        pe_ch = pe_last - pe_open

        decayed = ce_ch <= -8 and pe_ch <= -8
        expanded = None
        if ce_ch >= 15 and ce_ch > pe_ch + 5:
            expanded = "CE"
        elif pe_ch >= 15 and pe_ch > ce_ch + 5:
            expanded = "PE"

        pid = f"{ce['tradingsymbol']}|{pe['tradingsymbol']}"
        path = PairDayPath(
            pair_id=pid,
            expiry=exp,
            strike=strike,
            ce=ce,
            pe=pe,
            min_gap=round(min_gap if min_gap < 1e8 else last_gap, 2),
            converged=converged,
            first_converge_ts=first_conv,
            last_gap=round(last_gap, 2),
            last_mid=round(last_mid, 2),
            ce_open=ce_open,
            pe_open=pe_open,
            ce_last=ce_last,
            pe_last=pe_last,
            decayed=decayed,
            expanded_side=expanded,
        )

        # Score: prefer converged, not fully decayed, still actionable
        score = 0.0
        reasons: list[str] = []
        if converged:
            score += 40
            reasons.append(f"converged @ {first_conv}")
        score += max(0, 20 - path.min_gap)  # tighter min gap better
        if expanded and not decayed:
            score += 15
            reasons.append(f"expanded {expanded}")
        if decayed:
            score -= 25
            reasons.append("both decayed since open")
        # still near band mid? Prefer higher merge mid inside band
        if low <= path.last_mid <= high:
            band = max(high - low, 1.0)
            score += 10 + 15 * ((path.last_mid - low) / band)
            reasons.append(f"mid in band @{path.last_mid:.0f}")
        elif path.last_mid < low - 15:
            score -= 10
            reasons.append("mid left band low")
        # late but still room to target?
        if expanded == "CE" and ce_last and ce_last < float(cfg.get("target_low", 169)):
            score += 5
        if expanded == "PE" and pe_last and pe_last < float(cfg.get("target_low", 169)):
            score += 5
        # already ran to 2x — may be late for fresh entry
        if ce_open and ce_last and ce_last >= ce_open * 2:
            score -= 12
            reasons.append("CE already ~2x — late chase risk")
        if pe_open and pe_last and pe_last >= pe_open * 2:
            score -= 12
            reasons.append("PE already ~2x — late chase risk")

        path.score = score
        path.reasons = reasons
        paths.append(path)

    paths.sort(key=lambda p: (-p.score, p.min_gap, p.last_gap))
    report.paths = paths
    if paths:
        best = paths[0]
        report.recommended_id = best.pair_id
        report.message = (
            f"late +{mins}m dig: best K{best.strike} score={best.score:.0f} "
            f"minGap={best.min_gap} ({'; '.join(best.reasons) or 'ok'})"
        )
    else:
        report.message = f"late +{mins}m dig: no pair paths from history"
    return report


def seed_series_from_history(
    market: Any,
    instruments: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> int:
    """Pre-fill MarketHub option_series with today's minute closes for charts."""
    now = datetime.now(IST)
    if minutes_since_open(now) < 1:
        return 0
    open_t, end_t = _session_bounds(now)
    seeded = 0
    max_legs = int(cfg.get("late_start_max_legs", 40))
    for inst in instruments[:max_legs]:
        tok = int(inst.get("instrument_token") or 0)
        if tok <= 0:
            continue
        try:
            rows = _fetch_minute_closes(tok, open_t, end_t)
        except Exception:
            continue
        sym = inst["tradingsymbol"]
        with market._lock:
            series = market.option_series[sym]
            series.clear()
            for dt, px in rows:
                if hasattr(market, "_append_point"):
                    market._append_point(series, dt.timestamp(), float(px))
                else:
                    series.append((float(int(dt.timestamp())), float(px)))
            if rows:
                market.last_quotes[sym] = rows[-1][1]
                seeded += 1
    return seeded

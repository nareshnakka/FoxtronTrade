"""
Lab scanner — multi-day falling-channel breakouts on current-week options.

Method
------
1) Identify strikes that fell for 3–4 sessions (lower highs + lower closes).
2) Build a descending parallel channel from those daily highs/lows.
3) On the next session, detect breakout above the projected upper rail.
4) Simulate SL=-12 / TP=+30/+40/+50 and score confirmation filters
   across premium bands 30–40 … 120–130.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CHAIN_1M = ROOT / "data" / "option_chain_1m"
LIVE_DB = ROOT / "data" / "option_chain_live" / "chain.db"
OUT_DIR = ROOT / "data" / "lab"
OUT_JSON = OUT_DIR / "fallen_channel_breakout_analysis.json"
LAST_RUN = OUT_DIR / "lab_fallen_last.json"
IST = ZoneInfo("Asia/Kolkata")

BANDS = [(lo, lo + 10) for lo in range(30, 130, 10)]
FALL_DAYS_MIN = 3
FALL_DAYS_MAX = 4
SL_PTS = 12.0
TP_LEVELS = (30.0, 40.0, 50.0)
BREAK_BUF = 1.5
MAX_HOLD_BARS = 180
SESSION_START = "09:20"
SESSION_END = "15:15"

FILTERS = [
    "bare",
    "persist3",
    "strong_body",
    "spot_aligned",
    "mom10",
    "width_ok",
    "steep",
    "clean_break",
    "time_ok",
    "near_channel",
    "persist3_spot",
    "persist3_mom",
    "clean_spot",
    "clean_near",
    "quality",
    "elite",
    "elite_mom",
]


@dataclass
class Bar:
    ts: str
    day: str
    o: float
    h: float
    l: float
    c: float
    spot: float
    oi: float = 0.0
    vol: float = 0.0


@dataclass
class Trade:
    expiry: str
    side: str
    strike: int
    band: str
    entry_ts: str
    entry_px: float
    channel_slope_per_day: float
    channel_width: float
    fall_days: int
    fall_drop: float
    fall_drop_pct: float
    break_dist: float
    filters: dict[str, bool]
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _parse_day_dirs(day_filter: set[str] | None = None) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Load viewer days. If day_filter set, keep those days plus earlier days for channel context."""
    all_days = sorted(
        d.name
        for d in CHAIN_1M.iterdir()
        if d.is_dir() and (d / "meta.json").exists() and (d / "viewer.json").exists()
    )
    keep: set[str] | None = None
    if day_filter:
        # Always include history before the earliest selected day (for 3–4d fall)
        keep = set(day_filter)
        earliest = min(day_filter)
        for d in all_days:
            if d < earliest:
                keep.add(d)
            elif d in day_filter:
                keep.add(d)
    out = []
    for day in all_days:
        if keep is not None and day not in keep:
            continue
        d = CHAIN_1M / day
        out.append(
            (
                day,
                json.loads((d / "meta.json").read_text(encoding="utf-8")),
                json.loads((d / "viewer.json").read_text(encoding="utf-8")),
            )
        )
    return out


def _load_viewer_series(
    *,
    day_filter: set[str] | None = None,
    include_db: bool = True,
) -> dict[tuple[str, str, int], list[Bar]]:
    series: dict[tuple[str, str, int], list[Bar]] = defaultdict(list)
    for day, meta, view in _parse_day_dirs(day_filter):
        expiry = str(meta.get("expiry") or "")[:10]
        if not expiry:
            continue
        for fr in view.get("frames") or []:
            dt = str(fr.get("datetime") or "")
            if len(dt) < 16:
                continue
            hhmm = dt[11:16]
            if hhmm < SESSION_START or hhmm > SESSION_END:
                continue
            spot = float(fr.get("spot") or 0)
            for key, leg in (fr.get("legs") or {}).items():
                try:
                    side, strike_s = str(key).split(":")
                    strike = int(strike_s)
                except Exception:
                    continue
                if side not in ("CE", "PE"):
                    continue
                try:
                    o = float(leg.get("o") or leg.get("c") or 0)
                    h = float(leg.get("h") or o)
                    l = float(leg.get("l") or o)
                    c = float(leg.get("c") or 0)
                except (TypeError, ValueError):
                    continue
                if c <= 0:
                    continue
                series[(expiry, side, strike)].append(
                    Bar(
                        ts=dt.replace("T", " ")[:19],
                        day=day,
                        o=o,
                        h=h,
                        l=l,
                        c=c,
                        spot=spot,
                        oi=float(leg.get("oi") or 0),
                        vol=float(leg.get("v") or 0),
                    )
                )

    if include_db and LIVE_DB.exists():
        con = sqlite3.connect(LIVE_DB)
        rows = con.execute(
            "SELECT ts, expiry, strike, side, ltp FROM ltp_legs WHERE week='current' ORDER BY ts"
        ).fetchall()
        spot_rows = {
            str(r[0])[:16]: float(r[1] or 0)
            for r in con.execute("SELECT ts, spot FROM ltp_ticks ORDER BY ts")
        }
        con.close()
        buckets: dict[tuple[str, str, int, str], list[float]] = defaultdict(list)
        meta_spot: dict[tuple[str, str, int, str], float] = {}
        for ts, expiry, strike, side, ltp in rows:
            if ltp is None:
                continue
            ts_s = str(ts).replace("T", " ")
            day = ts_s[:10]
            if day_filter is not None:
                earliest = min(day_filter)
                # selected days + any prior history for the falling window
                if day not in day_filter and day >= earliest:
                    continue
            hhmm = ts_s[11:16] if len(ts_s) >= 16 else ""
            if hhmm < SESSION_START or hhmm > SESSION_END:
                continue
            key = (str(expiry)[:10], str(side).upper(), int(strike), ts_s[:16])
            buckets[key].append(float(ltp))
            meta_spot[key] = float(spot_rows.get(ts_s[:16], 0) or 0)
        for (expiry, side, strike, minute), px in buckets.items():
            if side not in ("CE", "PE") or not px:
                continue
            c = px[-1]
            o, h, l = px[0], max(px), min(px)
            series[(expiry, side, strike)].append(
                Bar(
                    ts=minute + ":00",
                    day=minute[:10],
                    o=o,
                    h=h,
                    l=l,
                    c=c,
                    spot=meta_spot.get((expiry, side, strike, minute), 0.0),
                )
            )

    for k, rows in list(series.items()):
        rows.sort(key=lambda b: b.ts)
        dedup: list[Bar] = []
        seen: set[str] = set()
        for b in rows:
            if b.ts in seen:
                continue
            seen.add(b.ts)
            dedup.append(b)
        series[k] = dedup
    return series


def _day_stats(bars: list[Bar]) -> list[dict[str, Any]]:
    by_day: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        by_day[b.day].append(b)
    stats = []
    for day in sorted(by_day):
        rows = by_day[day]
        if len(rows) < 20:
            continue
        first_n = rows[: min(30, len(rows))]
        stats.append(
            {
                "day": day,
                "n": len(rows),
                "open": float(rows[0].c),
                "close": float(rows[-1].c),
                "high": float(max(r.h for r in rows)),
                "low": float(min(r.l for r in rows)),
                "early_high": float(max(r.h for r in first_n)),
                "early_low": float(min(r.l for r in first_n)),
                "vwap_proxy": float(sum(r.c for r in rows) / len(rows)),
            }
        )
    return stats


def _is_falling_multi_day(day_stats: list[dict[str, Any]], n_days: int) -> dict[str, Any] | None:
    if len(day_stats) < n_days:
        return None
    win = day_stats[-n_days:]
    closes = [float(d["close"]) for d in win]
    highs = [float(d["high"]) for d in win]
    drop = closes[0] - closes[-1]
    if drop < 6.0:
        return None
    drop_pct = 100.0 * drop / max(closes[0], 1.0)
    lower_closes = sum(1 for i in range(1, len(closes)) if closes[i] <= closes[i - 1] + 2.0)
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] <= highs[i - 1] + 3.0)
    high_drop = highs[0] - highs[-1]
    red = sum(1 for d in win if float(d["close"]) <= float(d["open"]) + 1.5)
    strict = (
        lower_closes >= n_days - 1
        and lower_highs >= n_days - 1
        and high_drop >= 4.0
        and red >= max(2, n_days - 1)
    )
    soft = (
        drop_pct >= 25.0
        and high_drop >= 8.0
        and lower_highs >= n_days - 2
        and closes[-1] <= min(closes[:-1]) + 3.0
    )
    if not (strict or soft):
        return None
    return {
        "n_days": n_days,
        "days": [d["day"] for d in win],
        "closes": closes,
        "highs": highs,
        "lows": [float(d["low"]) for d in win],
        "drop": drop,
        "drop_pct": drop_pct,
        "high_drop": high_drop,
        "mode": "strict" if strict else "soft",
        "win": win,
    }


def _daily_falling_channel(fall: dict[str, Any]) -> dict[str, Any] | None:
    win = fall["win"]
    n = len(win)
    if n < 3:
        return None
    highs = [float(d["high"]) for d in win]
    early = [float(d["early_high"]) for d in win]
    span = max(1, n - 1)
    y0, y1 = highs[0], highs[-1]
    slope = (y1 - y0) / span
    if slope > -0.5:
        y0, y1 = early[0], early[-1]
        slope = (y1 - y0) / span
    if slope > -0.3:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(highs) / n
    den = sum((x - mean_x) ** 2 for x in xs) or 1.0
    ls_slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, highs)) / den
    if ls_slope < slope:
        slope = ls_slope
        y0 = mean_y - slope * mean_x
    else:
        y0 = float(y0)

    ranges = sorted(max(1.0, float(d["high"]) - float(d["low"])) for d in win)
    mid = ranges[len(ranges) // 2]
    width = max(mid * 1.15, 4.0)
    last_px = float(win[-1]["close"])
    width = min(width, max(18.0, last_px * 0.45))
    if width < 3.0:
        return None
    a_hi = y0
    return {
        "slope_per_day": slope,
        "a_hi": a_hi,
        "width": width,
        "n_days": n,
        "last_day": win[-1]["day"],
        "proj_upper_next": a_hi + slope * n,
        "proj_lower_next": a_hi + slope * n - width,
    }


def _band_for(px: float) -> str | None:
    for lo, hi in BANDS:
        if lo <= px < hi:
            return f"{lo}-{hi}"
    return None


def _simulate(bars: list[Bar], entry_i: int, entry_px: float, tp: float) -> dict[str, Any]:
    sl = entry_px - SL_PTS
    target = entry_px + tp
    end = min(len(bars) - 1, entry_i + MAX_HOLD_BARS)
    entry_day = bars[entry_i].day
    for j in range(entry_i + 1, end + 1):
        b = bars[j]
        hit_sl = b.l <= sl
        hit_tp = b.h >= target
        if hit_sl and hit_tp:
            return {
                "exit": "SL_TP_same",
                "exit_px": sl,
                "pnl": round(sl - entry_px, 2),
                "bars": j - entry_i,
                "same_day": b.day == entry_day,
            }
        if hit_sl:
            return {
                "exit": "SL",
                "exit_px": sl,
                "pnl": round(sl - entry_px, 2),
                "bars": j - entry_i,
                "same_day": b.day == entry_day,
            }
        if hit_tp:
            return {
                "exit": "TP",
                "exit_px": target,
                "pnl": round(target - entry_px, 2),
                "bars": j - entry_i,
                "same_day": b.day == entry_day,
            }
    b = bars[end]
    return {
        "exit": "TIME",
        "exit_px": b.c,
        "pnl": round(b.c - entry_px, 2),
        "bars": end - entry_i,
        "same_day": b.day == entry_day,
    }


def _filters(
    bars: list[Bar],
    i: int,
    side: str,
    upper: float,
    ch: dict[str, Any],
) -> dict[str, bool]:
    b = bars[i]
    persist3 = True
    for k in range(max(0, i - 2), i + 1):
        if bars[k].c < upper + 0.5:
            persist3 = False
            break
    rng = max(0.05, b.h - b.l)
    strong_body = ((b.c - b.l) / rng >= 0.65) and (b.c >= b.o)
    spot_chg = b.spot - bars[i - 15].spot if i >= 15 else 0.0
    spot_aligned = (side == "CE" and spot_chg >= 8.0) or (
        side == "PE" and spot_chg <= -8.0
    )
    mom10 = b.c - bars[i - 10].c if i >= 10 else 0.0
    mom_ok = mom10 >= 2.5
    width = float(ch["width"])
    width_ok = 3.0 <= width <= 28.0
    steep = float(ch["slope_per_day"]) <= -2.0
    break_dist = b.c - upper
    clean_break = break_dist >= 2.5
    hhmm = b.ts[11:16]
    time_ok = "09:45" <= hhmm <= "14:30"
    near_channel = break_dist <= 12.0
    return {
        "bare": True,
        "persist3": persist3,
        "strong_body": strong_body,
        "spot_aligned": spot_aligned,
        "mom10": mom_ok,
        "width_ok": width_ok,
        "steep": steep,
        "clean_break": clean_break,
        "time_ok": time_ok,
        "near_channel": near_channel,
        "persist3_spot": persist3 and spot_aligned,
        "persist3_mom": persist3 and mom_ok,
        "clean_spot": clean_break and spot_aligned,
        "clean_near": clean_break and near_channel,
        "quality": clean_break and persist3 and near_channel and time_ok,
        "elite": clean_break
        and persist3
        and spot_aligned
        and strong_body
        and near_channel
        and time_ok,
        "elite_mom": clean_break
        and persist3
        and mom_ok
        and spot_aligned
        and near_channel,
    }


def scan_instrument(
    expiry: str,
    side: str,
    strike: int,
    bars: list[Bar],
    *,
    entry_days: set[str] | None = None,
) -> list[Trade]:
    trades: list[Trade] = []
    if len(bars) < 120:
        return trades
    day_stats = _day_stats(bars)
    if len(day_stats) < FALL_DAYS_MIN:
        return trades

    days = sorted({b.day for b in bars})
    day_start: dict[str, int] = {}
    for i, b in enumerate(bars):
        if b.day not in day_start:
            day_start[b.day] = i

    taken: set[str] = set()

    for di, day in enumerate(days):
        if entry_days is not None and day not in entry_days:
            continue
        prior = [d for d in day_stats if d["day"] < day]
        fall = None
        for n in range(FALL_DAYS_MAX, FALL_DAYS_MIN - 1, -1):
            fall = _is_falling_multi_day(prior, n)
            if fall:
                break
        if not fall:
            continue
        ch = _daily_falling_channel(fall)
        if not ch:
            continue

        start_i = day_start[day]
        end_i = len(bars) - 1
        if di + 1 < len(days):
            end_i = day_start[days[di + 1]] - 1

        upper = float(ch["proj_upper_next"])
        pending_break_i: int | None = None
        for i in range(start_i, end_i + 1):
            if day in taken:
                break
            b = bars[i]
            hhmm = b.ts[11:16]
            if hhmm < "09:30" or hhmm > "14:45":
                continue
            if b.c < upper + BREAK_BUF:
                pending_break_i = None
                continue
            if pending_break_i is None:
                pending_break_i = i
            if i - pending_break_i < 1:
                continue
            if bars[i - 1].c < upper + BREAK_BUF * 0.5:
                pending_break_i = i
                continue

            if not (25 <= b.c < 140):
                continue
            band = _band_for(b.c) or f"x{int(b.c // 10) * 10}"
            dist = b.c - upper
            if dist > 18.0:
                continue

            flags = _filters(bars, i, side, upper, ch)
            tr = Trade(
                expiry=expiry,
                side=side,
                strike=strike,
                band=band,
                entry_ts=b.ts,
                entry_px=round(b.c, 2),
                channel_slope_per_day=round(float(ch["slope_per_day"]), 3),
                channel_width=round(float(ch["width"]), 2),
                fall_days=int(fall["n_days"]),
                fall_drop=round(float(fall["drop"]), 2),
                fall_drop_pct=round(float(fall["drop_pct"]), 2),
                break_dist=round(dist, 2),
                filters=flags,
            )
            for tp in TP_LEVELS:
                tr.outcomes[f"tp{int(tp)}"] = _simulate(bars, i, b.c, tp)
            trades.append(tr)
            taken.add(day)
            break
    return trades


def _agg(trades: list[Trade], filt: str, tp_key: str) -> dict[str, Any]:
    rows = [t for t in trades if t.filters.get(filt)]
    if not rows:
        return {"n": 0}
    pnls = [float(t.outcomes[tp_key]["pnl"]) for t in rows if tp_key in t.outcomes]
    if not pnls:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg = sum(pnls) / len(pnls)
    return {
        "n": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 3),
        "avg_pnl": round(avg, 2),
        "sum_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy_R": round(avg / SL_PTS, 3),
        "profit_factor": round(
            (sum(wins) / abs(sum(losses))) if losses and sum(losses) else 99.0, 2
        ),
        "tp_hits": sum(1 for t in rows if t.outcomes.get(tp_key, {}).get("exit") == "TP"),
        "sl_hits": sum(1 for t in rows if t.outcomes.get(tp_key, {}).get("exit") == "SL"),
    }


def summarize(trades: list[Trade]) -> dict[str, Any]:
    band_names = {f"{a}-{b}" for a, b in BANDS}
    in_band = [t for t in trades if t.band in band_names]
    by_band: dict[str, Any] = {}
    for lo, hi in BANDS:
        band = f"{lo}-{hi}"
        bt = [t for t in trades if t.band == band]
        by_band[band] = {
            "n_signals": len(bt),
            "filters": {
                f: {str(int(tp)): _agg(bt, f, f"tp{int(tp)}") for tp in TP_LEVELS}
                for f in FILTERS
            },
        }

    overall = {
        f: {str(int(tp)): _agg(in_band, f, f"tp{int(tp)}") for tp in TP_LEVELS}
        for f in FILTERS
    }
    overall_all_px = {
        f: {str(int(tp)): _agg(trades, f, f"tp{int(tp)}") for tp in TP_LEVELS}
        for f in FILTERS
    }

    ranked = []
    for f in FILTERS:
        for band, payload in by_band.items():
            for tp in ("30", "40", "50"):
                cell = payload["filters"][f][tp]
                if cell.get("n", 0) >= 2:
                    ranked.append({"filter": f, "band": band, "tp": int(tp), **cell})
        for tp in ("30", "40", "50"):
            cell = overall[f][tp]
            if cell.get("n", 0) >= 3:
                ranked.append({"filter": f, "band": "30-130", "tp": int(tp), **cell})
    ranked.sort(
        key=lambda x: (x.get("expectancy_R", -9), x.get("win_rate", 0), x.get("n", 0)),
        reverse=True,
    )
    return {
        "n_trades_all": len(trades),
        "n_trades_band_30_130": len(in_band),
        "by_side": {
            "CE": sum(1 for t in trades if t.side == "CE"),
            "PE": sum(1 for t in trades if t.side == "PE"),
        },
        "by_expiry": {
            e: sum(1 for t in trades if t.expiry == e)
            for e in sorted({t.expiry for t in trades})
        },
        "overall_band_30_130": overall,
        "overall_all_premiums": overall_all_px,
        "by_band": by_band,
        "top_setups": ranked[:40],
    }


def load_last_fallen() -> dict[str, Any] | None:
    if not LAST_RUN.exists():
        return None
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_scan(
    *,
    days: list[str] | None = None,
    preset: str | None = None,
    include_db: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Lab entrypoint. Optional `days` limits which sessions can host the breakout
    entry (prior history is still loaded for the 3–4d falling channel).
    """
    entry_days = {str(d)[:10] for d in (days or []) if d} or None
    series = _load_viewer_series(day_filter=entry_days, include_db=include_db)

    all_trades: list[Trade] = []
    fall_n = 0
    ch_n = 0
    for (expiry, side, strike), bars in sorted(series.items()):
        ds = _day_stats(bars)
        for day in sorted({b.day for b in bars}):
            if entry_days is not None and day not in entry_days:
                continue
            prior = [d for d in ds if d["day"] < day]
            fall = None
            for n in range(FALL_DAYS_MAX, FALL_DAYS_MIN - 1, -1):
                fall = _is_falling_multi_day(prior, n)
                if fall:
                    break
            if not fall:
                continue
            fall_n += 1
            if _daily_falling_channel(fall):
                ch_n += 1
        all_trades.extend(
            scan_instrument(
                expiry, side, strike, bars, entry_days=entry_days
            )
        )

    summary = summarize(all_trades)
    data_days = sorted({b.day for rows in series.values() for b in rows})
    payload: dict[str, Any] = {
        "ok": True,
        "kind": "fallen_channel",
        "generated_at": _now_iso(),
        "preset": preset,
        "entry_days": sorted(entry_days) if entry_days else data_days,
        "params": {
            "fall_days": [FALL_DAYS_MIN, FALL_DAYS_MAX],
            "sl": SL_PTS,
            "tp": list(TP_LEVELS),
            "break_buf": BREAK_BUF,
            "bands": [f"{a}-{b}" for a, b in BANDS],
            "note": (
                "Daily descending channel from 3-4d lower-highs; "
                "breakout = 2 closes above projected upper rail; gap-through >18 skipped"
            ),
            "data_days": data_days,
            "instruments": len(series),
            "fall_windows": fall_n,
            "tradable_channels": ch_n,
            "include_db": include_db,
        },
        "summary": summary,
        "trades": [asdict(t) for t in all_trades],
        # Compact KPI for Lab header
        "result": {
            "label": "Fallen channel breakout",
            "n": summary["n_trades_band_30_130"],
            "n_all": summary["n_trades_all"],
            "fall_windows": fall_n,
            "channels": ch_n,
            "bare_tp40": (summary.get("overall_band_30_130") or {})
            .get("bare", {})
            .get("40", {}),
            "bare_tp50": (summary.get("overall_band_30_130") or {})
            .get("bare", {})
            .get("50", {}),
            "top": (summary.get("top_setups") or [None])[0],
        },
    }

    if persist:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            LAST_RUN.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass
    return payload

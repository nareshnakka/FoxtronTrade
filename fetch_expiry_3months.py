"""
Fetch every trading day for ALL Nifty weekly expiries in the last 3 months.
Includes spot OHLC, futures OI, causal S/R, pivots, and minute files.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite
from foxtrade.indicators import support_resistance

IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
OUT = Path(__file__).resolve().parent / "data" / "expiries"
ROOT = OUT / "last_3_months"


def _daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _has_session(kite, day: date) -> bool:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    bars = kite.historical_data(NIFTY_TOKEN, start, end, "day") or []
    return bool(bars)


def _prev_trading_day(kite, day: date, cache: dict[date, bool]) -> date:
    d = day
    for _ in range(10):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if d not in cache:
            cache[d] = _has_session(kite, d)
        if cache[d]:
            return d
    return day - timedelta(days=1)


def list_nifty_expiries(kite, start: date, end: date) -> list[dict]:
    """
    Nifty weekly = every Tuesday; if holiday, prior trading day.
    Monthly note when expiry is last Tuesday of month (or shifted).
    Future Tuesdays (no history yet) are skipped — missing bars ≠ holiday.
    """
    cache: dict[date, bool] = {}
    as_of = min(end, datetime.now(IST).date())
    # Candidate Tuesdays in range (and one week before start for shift)
    tuesdays = []
    d = start - timedelta(days=7)
    while d <= end + timedelta(days=7):
        if d.weekday() == 1:  # Tuesday
            tuesdays.append(d)
        d += timedelta(days=1)

    expiries: list[date] = []
    for tue in tuesdays:
        if tue > as_of:
            # Upcoming Tuesday — not yet an expiry week we can resolve.
            continue
        if tue not in cache:
            cache[tue] = _has_session(kite, tue)
        exp = tue if cache[tue] else _prev_trading_day(kite, tue, cache)
        if start <= exp <= end and exp not in expiries:
            expiries.append(exp)

    expiries.sort()
    out = []
    for exp in expiries:
        last_tue = None
        for day in range(31, 0, -1):
            try:
                cand = date(exp.year, exp.month, day)
            except ValueError:
                continue
            if cand.weekday() == 1:
                last_tue = cand
                break
        if last_tue is not None and last_tue not in cache:
            cache[last_tue] = _has_session(kite, last_tue)
        shifted_last = (
            last_tue
            if last_tue and cache.get(last_tue, False)
            else (_prev_trading_day(kite, last_tue, cache) if last_tue else None)
        )
        is_monthly = shifted_last is not None and exp == shifted_last
        out.append(
            {
                "expiry": exp.isoformat(),
                "weekday": exp.strftime("%A"),
                "type": "weekly+monthly" if is_monthly else "weekly",
                "note": (
                    "Monthly + weekly (last Tue of month / holiday-shifted)"
                    if is_monthly
                    else "Weekly Tuesday rule (holiday-adjusted)"
                ),
            }
        )
    return out


def _pick_fut(kite) -> tuple[int, str]:
    inst = kite.instruments("NFO")
    futs = [i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"]
    futs.sort(key=lambda x: x["expiry"])
    f = futs[0]
    return int(f["instrument_token"]), str(f["tradingsymbol"])


def _day_bar(kite, token: int, day: date, *, oi: bool = False) -> dict | None:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    bars = kite.historical_data(token, start, end, "day", oi=oi) or []
    if not bars:
        return None
    b = bars[0]
    out = {
        "open": float(b["open"]),
        "high": float(b["high"]),
        "low": float(b["low"]),
        "close": float(b["close"]),
        "volume": int(b.get("volume") or 0),
    }
    if oi and b.get("oi") is not None:
        out["oi"] = int(b["oi"])
    return out


def _minutes(kite, token: int, day: date, *, oi: bool = False) -> list[dict]:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    bars = kite.historical_data(token, start, end, "minute", oi=oi) or []
    out = []
    for b in bars:
        ts = b["date"]
        row = {
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": int(b.get("volume") or 0),
        }
        if oi and b.get("oi") is not None:
            row["oi"] = int(b["oi"])
        out.append(row)
    return out


def _pivot_levels(prev: dict) -> dict[str, float]:
    h, l, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    p = (h + l + c) / 3.0
    return {
        "pivot": round(p, 2),
        "r1": round(2 * p - l, 2),
        "r2": round(p + (h - l), 2),
        "r3": round(h + 2 * (p - l), 2),
        "s1": round(2 * p - h, 2),
        "s2": round(p - (h - l), 2),
        "s3": round(l - 2 * (h - p), 2),
    }


def _sr(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    sr40 = support_resistance(closes, 40)
    sr20 = support_resistance(closes, 20)
    sr10 = support_resistance(closes, 10)
    return {
        "support_10d": sr10.get("support"),
        "resistance_10d": sr10.get("resistance"),
        "support_20d": sr20.get("support"),
        "resistance_20d": sr20.get("resistance"),
        "support_40d": sr40.get("support"),
        "resistance_40d": sr40.get("resistance"),
        "swing_support_20d": round(min(lows[-20:]), 2) if len(lows) >= 5 else None,
        "swing_resistance_20d": round(max(highs[-20:]), 2) if len(highs) >= 5 else None,
    }


def main() -> None:
    kite = get_authenticated_kite()
    today = date.today()
    # Last ~3 calendar months
    start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    start = start - timedelta(days=32)
    start = start.replace(day=1)  # roughly month-3 first day
    # Cleaner: today - 92 days, align to month start of that month
    start = today - timedelta(days=92)
    start = date(start.year, start.month, 1)
    end = today

    ROOT.mkdir(parents=True, exist_ok=True)
    minutes_dir = ROOT / "minutes"
    oi_dir = ROOT / "oi_minutes"
    minutes_dir.mkdir(exist_ok=True)
    oi_dir.mkdir(exist_ok=True)

    print(f"Range {start} → {end}")
    expiries = list_nifty_expiries(kite, start, end)
    print(f"Found {len(expiries)} expiries:")
    for e in expiries:
        print(f"  {e['expiry']}  {e['type']}")

    fut_token, fut_sym = _pick_fut(kite)
    print(f"OI source: {fut_sym}")

    # Long daily history for causal S/R
    hist_from = datetime(start.year, start.month, start.day, 9, 15, tzinfo=IST) - timedelta(days=60)
    hist_to = datetime(end.year, end.month, end.day, 15, 30, tzinfo=IST)
    hist_daily = kite.historical_data(NIFTY_TOKEN, hist_from, hist_to, "day") or []
    hist_by_day: dict[str, dict] = {}
    ordered: list[str] = []
    for b in hist_daily:
        dt = b["date"]
        key = (dt.date() if hasattr(dt, "date") else dt).isoformat()[:10]
        hist_by_day[key] = {
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        }
        ordered.append(key)

    # Assign week_start = day after previous expiry (or range start)
    exp_dates = [date.fromisoformat(e["expiry"]) for e in expiries]
    for i, meta in enumerate(expiries):
        exp = exp_dates[i]
        prev = exp_dates[i - 1] if i > 0 else (start - timedelta(days=1))
        w0 = prev + timedelta(days=1)
        if w0 < start:
            w0 = start
        meta["week_start"] = w0.isoformat()

    payload = {
        "as_of": today.isoformat(),
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "symbol": "NIFTY 50",
        "oi_instrument": fut_sym,
        "oi_instrument_token": fut_token,
        "oi_note": "Nifty front-month futures OI (Kite). Spot has no OI.",
        "sr_note": "Causal S/R (bars ≤ day). Pivots from prior day OHLC.",
        "expiry_count": len(expiries),
        "source": "Kite historical · last 3 months all weekly expiries",
        "expiries": [],
        "all_days_index": [],  # flat index of every session day
    }

    seen_days: set[str] = set()
    flat_days: list[dict] = []

    for i, meta in enumerate(expiries):
        exp = date.fromisoformat(meta["expiry"])
        w0 = date.fromisoformat(meta["week_start"])
        days: list[dict] = []
        print(f"\n=== {meta['expiry']} ({meta['type']})  {w0}→{exp} ===")

        for d in _daterange(w0, exp):
            if d.weekday() >= 5:
                continue
            key = d.isoformat()
            spot = _day_bar(kite, NIFTY_TOKEN, d, oi=False)
            if spot is None:
                days.append(
                    {
                        "date": key,
                        "weekday": d.strftime("%A"),
                        "session": False,
                        "note": "holiday or no data",
                        "expiry_week": meta["expiry"],
                    }
                )
                print(f"  {d} HOLIDAY")
                continue

            fut = _day_bar(kite, fut_token, d, oi=True)
            spot_mins = _minutes(kite, NIFTY_TOKEN, d, oi=False)
            fut_mins = _minutes(kite, fut_token, d, oi=True)

            closes, highs, lows = [], [], []
            prev_bar = None
            for dk in ordered:
                if dk > key:
                    break
                row = hist_by_day[dk]
                if dk < key:
                    prev_bar = row
                closes.append(row["close"])
                highs.append(row["high"])
                lows.append(row["low"])
            if key not in hist_by_day:
                closes.append(spot["close"])
                highs.append(spot["high"])
                lows.append(spot["low"])

            sr = _sr(closes, highs, lows)
            oi_vals = [m["oi"] for m in fut_mins if m.get("oi") is not None]
            oi_intraday = None
            if oi_vals:
                oi_intraday = {
                    "oi_open": oi_vals[0],
                    "oi_close": oi_vals[-1],
                    "oi_high": max(oi_vals),
                    "oi_low": min(oi_vals),
                    "oi_change_intraday": oi_vals[-1] - oi_vals[0],
                }

            day_row = {
                "date": key,
                "weekday": d.strftime("%A"),
                "session": True,
                "is_expiry_day": d == exp,
                "expiry_week": meta["expiry"],
                "spot": {
                    "open": spot["open"],
                    "high": spot["high"],
                    "low": spot["low"],
                    "close": spot["close"],
                    "volume": spot["volume"],
                    "range": round(spot["high"] - spot["low"], 2),
                    "change": round(spot["close"] - spot["open"], 2),
                    "change_pct": round(
                        (spot["close"] - spot["open"]) / spot["open"] * 100, 3
                    ),
                },
                "open_interest": {
                    "instrument": fut_sym,
                    "day_oi": int(fut["oi"]) if fut and fut.get("oi") is not None else None,
                    "fut_open": fut["open"] if fut else None,
                    "fut_high": fut["high"] if fut else None,
                    "fut_low": fut["low"] if fut else None,
                    "fut_close": fut["close"] if fut else None,
                    "fut_volume": fut["volume"] if fut else None,
                    **(oi_intraday or {}),
                },
                "support_resistance": {
                    **sr,
                    "session_low_as_support": spot["low"],
                    "session_high_as_resistance": spot["high"],
                },
                "pivots": _pivot_levels(prev_bar) if prev_bar else None,
                "minutes_count": len(spot_mins),
                "minutes_file": f"minutes/nifty_{key}.json",
                "oi_minutes_file": f"oi_minutes/nifty_fut_{key}.json",
            }
            days.append(day_row)

            # Write minute files once per calendar day
            if key not in seen_days:
                (minutes_dir / f"nifty_{key}.json").write_text(
                    json.dumps(spot_mins, indent=2), encoding="utf-8"
                )
                (oi_dir / f"nifty_fut_{key}.json").write_text(
                    json.dumps(fut_mins, indent=2), encoding="utf-8"
                )
                seen_days.add(key)
                flat_days.append(day_row)

            oi = day_row["open_interest"].get("day_oi")
            print(
                f"  {d} {d.strftime('%a')}  "
                f"{spot['open']:.0f}→{spot['close']:.0f}  "
                f"S20={sr.get('support_20d')} R20={sr.get('resistance_20d')}  "
                f"OI={oi}"
                f"{'  EXPIRY' if d == exp else ''}"
            )

        prev_oi = None
        for row in days:
            if not row.get("session"):
                continue
            oi = (row.get("open_interest") or {}).get("day_oi")
            if oi is not None and prev_oi is not None and prev_oi:
                row["open_interest"]["oi_change_vs_prev_day"] = oi - prev_oi
                row["open_interest"]["oi_change_pct_vs_prev_day"] = round(
                    (oi - prev_oi) / prev_oi * 100, 3
                )
            if oi is not None:
                prev_oi = oi

        session_days = [x for x in days if x.get("session")]
        w_open = session_days[0]["spot"]["open"] if session_days else None
        w_close = session_days[-1]["spot"]["close"] if session_days else None
        payload["expiries"].append(
            {
                **meta,
                "week_end": meta["expiry"],
                "trading_days": len(session_days),
                "week_open": w_open,
                "week_close": w_close,
                "week_change": (
                    round(w_close - w_open, 2)
                    if w_open is not None and w_close is not None
                    else None
                ),
                "days": days,
            }
        )

    # Sort flat index and fill OI day change across full timeline
    flat_days.sort(key=lambda x: x["date"])
    prev_oi = None
    for row in flat_days:
        oi = (row.get("open_interest") or {}).get("day_oi")
        if oi is not None and prev_oi is not None and prev_oi:
            row["open_interest"]["oi_change_vs_prev_day"] = oi - prev_oi
            row["open_interest"]["oi_change_pct_vs_prev_day"] = round(
                (oi - prev_oi) / prev_oi * 100, 3
            )
        if oi is not None:
            prev_oi = oi
    payload["all_days_index"] = flat_days
    payload["session_day_count"] = len(flat_days)

    out = ROOT / "all_expiries_every_day.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Also compact summary without embedding full day arrays twice — already fine
    summary = {
        "as_of": payload["as_of"],
        "range_start": payload["range_start"],
        "range_end": payload["range_end"],
        "expiry_count": payload["expiry_count"],
        "session_day_count": payload["session_day_count"],
        "oi_instrument": fut_sym,
        "expiries": [
            {
                "expiry": e["expiry"],
                "type": e["type"],
                "week_start": e.get("week_start"),
                "trading_days": e["trading_days"],
                "week_open": e["week_open"],
                "week_close": e["week_close"],
                "week_change": e["week_change"],
            }
            for e in payload["expiries"]
        ],
    }
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")
    print(f"Summary {ROOT / 'summary.json'}")
    print(f"Days={len(flat_days)}  Expiries={len(expiries)}")


if __name__ == "__main__":
    main()

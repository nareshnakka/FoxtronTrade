"""
Fetch Nifty spot + OI + support/resistance for every trading day
in the last 3 completed expiry weeks. Saves combined JSON + minute files.
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

EXPIRIES = [
    {
        "expiry": "2026-10-27",
        "weekday": "Tuesday",
        "type": "weekly+monthly",
        "note": "Last Tue of Oct — weekly and monthly",
        "week_start": "2026-10-20",
    },
    {
        "expiry": "2026-10-19",
        "weekday": "Monday",
        "type": "weekly",
        "note": "Moved from Tue 20 Oct (Dussehra)",
        "week_start": "2026-10-14",
    },
    {
        "expiry": "2026-10-13",
        "weekday": "Tuesday",
        "type": "weekly",
        "note": "Regular Tuesday weekly",
        "week_start": "2026-10-07",
    },
]


def _daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _pick_nifty_fut_token(kite) -> tuple[int, str]:
    inst = kite.instruments("NFO")
    futs = [
        i
        for i in inst
        if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"
    ]
    futs.sort(key=lambda x: x["expiry"])
    if not futs:
        raise RuntimeError("No NIFTY futures in NFO")
    # Prefer nearest fut that has history spanning Oct (usually front month)
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
    """Classic floor pivots from previous day OHLC."""
    h, l, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    p = (h + l + c) / 3.0
    r1 = 2 * p - l
    s1 = 2 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = h + 2 * (p - l)
    s3 = l - 2 * (h - p)
    return {
        "pivot": round(p, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "r3": round(r3, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "s3": round(s3, 2),
    }


def _sr_from_history(
    daily_closes: list[float],
    daily_highs: list[float],
    daily_lows: list[float],
) -> dict[str, float | None]:
    """Causal S/R using only bars available up to this day (inclusive)."""
    sr40 = support_resistance(daily_closes, lookback=40)
    sr20 = support_resistance(daily_closes, lookback=20)
    sr10 = support_resistance(daily_closes, lookback=10)
    # Swing S/R from highs/lows
    swing_res = max(daily_highs[-20:]) if len(daily_highs) >= 5 else None
    swing_sup = min(daily_lows[-20:]) if len(daily_lows) >= 5 else None
    return {
        "support_10d": sr10.get("support"),
        "resistance_10d": sr10.get("resistance"),
        "support_20d": sr20.get("support"),
        "resistance_20d": sr20.get("resistance"),
        "support_40d": sr40.get("support"),
        "resistance_40d": sr40.get("resistance"),
        "swing_support_20d": round(swing_sup, 2) if swing_sup is not None else None,
        "swing_resistance_20d": round(swing_res, 2) if swing_res is not None else None,
    }


def main() -> None:
    kite = get_authenticated_kite()
    OUT.mkdir(parents=True, exist_ok=True)
    minutes_dir = OUT / "minutes"
    oi_dir = OUT / "oi_minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)
    oi_dir.mkdir(parents=True, exist_ok=True)

    fut_token, fut_sym = _pick_nifty_fut_token(kite)
    print(f"OI source: {fut_sym} token={fut_token}")

    # Prefetch long daily spot history for causal S/R
    hist_start = datetime(2026, 8, 1, 9, 15, tzinfo=IST)
    hist_end = datetime(2026, 10, 29, 15, 30, tzinfo=IST)
    hist_daily = kite.historical_data(NIFTY_TOKEN, hist_start, hist_end, "day") or []
    # index by date
    hist_by_day: dict[str, dict] = {}
    ordered_days: list[str] = []
    for b in hist_daily:
        dt = b["date"]
        key = (dt.date() if hasattr(dt, "date") else dt).isoformat()[:10]
        hist_by_day[key] = {
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        }
        ordered_days.append(key)

    payload = {
        "as_of": date.today().isoformat(),
        "symbol": "NIFTY 50",
        "oi_instrument": fut_sym,
        "oi_instrument_token": fut_token,
        "oi_note": "Nifty futures open interest (Kite historical oi=True). Spot index has no OI.",
        "sr_note": "Support/resistance are causal: only bars ≤ that day. Pivots use prior day OHLC.",
        "next_live_expiry": "2026-11-03",
        "source": "Kite historical · spot + fut OI + S/R",
        "expiries": [],
    }

    for meta in EXPIRIES:
        exp = date.fromisoformat(meta["expiry"])
        w0 = date.fromisoformat(meta["week_start"])
        days: list[dict] = []
        print(f"\n=== Expiry {exp}  week {w0}→{exp} ===")
        for d in _daterange(w0, exp):
            if d.weekday() >= 5:
                continue
            key = d.isoformat()
            spot = _day_bar(kite, NIFTY_TOKEN, d, oi=False)
            if spot is None:
                print(f"  {d}  HOLIDAY/no bars")
                days.append(
                    {
                        "date": key,
                        "weekday": d.strftime("%A"),
                        "session": False,
                        "note": "holiday or no data",
                    }
                )
                continue

            fut = _day_bar(kite, fut_token, d, oi=True)
            spot_mins = _minutes(kite, NIFTY_TOKEN, d, oi=False)
            fut_mins = _minutes(kite, fut_token, d, oi=True)

            # Causal S/R from history up to and including this day
            closes: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            prev_bar = None
            for dk in ordered_days:
                if dk > key:
                    break
                row = hist_by_day[dk]
                if dk < key:
                    prev_bar = row
                closes.append(row["close"])
                highs.append(row["high"])
                lows.append(row["low"])
            # ensure today included
            if not closes or ordered_days[len(closes) - 1] != key if closes else True:
                if key in hist_by_day:
                    pass  # already in loop
                else:
                    closes.append(spot["close"])
                    highs.append(spot["high"])
                    lows.append(spot["low"])

            sr = _sr_from_history(closes, highs, lows)
            pivots = _pivot_levels(prev_bar) if prev_bar else None

            # OI change vs prior fut session
            oi_today = int(fut["oi"]) if fut and fut.get("oi") is not None else None
            oi_change = None
            oi_change_pct = None
            if oi_today is not None and prev_bar is not None:
                # find previous session fut OI from stored days in this run is hard;
                # use minute first/last OI delta within day instead + day oi
                pass
            oi_intraday = None
            if fut_mins:
                oi_vals = [m["oi"] for m in fut_mins if m.get("oi") is not None]
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
                    "day_oi": oi_today,
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
                    "near_support_40d": (
                        bool(
                            sr.get("support_40d")
                            and spot["low"] <= float(sr["support_40d"]) * 1.002
                        )
                        if sr.get("support_40d")
                        else None
                    ),
                    "near_resistance_40d": (
                        bool(
                            sr.get("resistance_40d")
                            and spot["high"] >= float(sr["resistance_40d"]) * 0.998
                        )
                        if sr.get("resistance_40d")
                        else None
                    ),
                },
                "pivots": pivots,
                "minutes_count": len(spot_mins),
                "minutes_file": f"minutes/nifty_{key}.json",
                "oi_minutes_file": f"oi_minutes/nifty_fut_{key}.json",
            }
            days.append(day_row)

            (minutes_dir / f"nifty_{key}.json").write_text(
                json.dumps(spot_mins, indent=2), encoding="utf-8"
            )
            (oi_dir / f"nifty_fut_{key}.json").write_text(
                json.dumps(fut_mins, indent=2), encoding="utf-8"
            )

            oi_tag = f"OI={oi_today}" if oi_today is not None else "OI=?"
            print(
                f"  {d} {d.strftime('%a')}  "
                f"{spot['open']:.1f}→{spot['close']:.1f}  "
                f"S={sr.get('support_20d')} R={sr.get('resistance_20d')}  "
                f"{oi_tag}"
                f"{'  EXPIRY' if d == exp else ''}"
            )

        # Fill day-over-day OI change now that we have sequence
        prev_oi = None
        for row in days:
            if not row.get("session"):
                continue
            oi = (row.get("open_interest") or {}).get("day_oi")
            if oi is not None and prev_oi is not None:
                row["open_interest"]["oi_change_vs_prev_day"] = oi - prev_oi
                row["open_interest"]["oi_change_pct_vs_prev_day"] = round(
                    (oi - prev_oi) / prev_oi * 100, 3
                ) if prev_oi else None
            if oi is not None:
                prev_oi = oi

        session_days = [x for x in days if x.get("session")]
        week_open = session_days[0]["spot"]["open"] if session_days else None
        week_close = session_days[-1]["spot"]["close"] if session_days else None
        payload["expiries"].append(
            {
                **meta,
                "week_start": w0.isoformat(),
                "week_end": exp.isoformat(),
                "trading_days": len(session_days),
                "week_open": week_open,
                "week_close": week_close,
                "week_change": (
                    round(week_close - week_open, 2)
                    if week_open is not None and week_close is not None
                    else None
                ),
                "days": days,
            }
        )

    out = OUT / "last_3_expiries_every_day.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")
    print(f"Spot minutes: {minutes_dir}")
    print(f"Fut OI minutes: {oi_dir}")


if __name__ == "__main__":
    main()

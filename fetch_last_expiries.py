"""Fetch Nifty spot data for the last 3 completed weekly expiries."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
OUT = Path(__file__).resolve().parent / "data" / "expiries"

# As of 2026-10-29: next live week is 2026-11-03.
# Prior completed Nifty weekly expiries (NSE Tue rule + holiday shifts):
LAST_3 = [
    {
        "expiry": "2026-10-27",
        "weekday": "Tuesday",
        "type": "weekly+monthly",
        "note": "Last Tuesday of Oct — weekly and monthly",
    },
    {
        "expiry": "2026-10-19",
        "weekday": "Monday",
        "type": "weekly",
        "note": "Moved from Tue 20 Oct (Dussehra holiday)",
    },
    {
        "expiry": "2026-10-13",
        "weekday": "Tuesday",
        "type": "weekly",
        "note": "Regular Tuesday weekly",
    },
]


def _day_bars(kite, day: date) -> list[dict]:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    return kite.historical_data(NIFTY_TOKEN, start, end, "day") or []


def _minute_bars(kite, day: date) -> list[dict]:
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    return kite.historical_data(NIFTY_TOKEN, start, end, "minute") or []


def _week_window(expiry: date) -> tuple[date, date]:
    """Prior Wednesday after previous week → expiry day (approx trade week)."""
    start = expiry - timedelta(days=6)
    return start, expiry


def summarize_minutes(bars: list[dict]) -> dict:
    if not bars:
        return {}
    opens = float(bars[0]["open"])
    closes = float(bars[-1]["close"])
    hi = max(float(b["high"]) for b in bars)
    lo = min(float(b["low"]) for b in bars)
    return {
        "minutes": len(bars),
        "open": opens,
        "high": hi,
        "low": lo,
        "close": closes,
        "range": round(hi - lo, 2),
        "change": round(closes - opens, 2),
        "change_pct": round((closes - opens) / opens * 100, 3) if opens else None,
    }


def main() -> None:
    kite = get_authenticated_kite()
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "as_of": date.today().isoformat(),
        "next_live_expiry": "2026-11-03",
        "source": "Kite historical Nifty 50 · NSE weekly Tuesday rule",
        "expiries": [],
    }

    for meta in LAST_3:
        exp = date.fromisoformat(meta["expiry"])
        w0, w1 = _week_window(exp)
        day_bar = _day_bars(kite, exp)
        mins = _minute_bars(kite, exp)

        # Daily bars across the week window
        week_start = datetime(w0.year, w0.month, w0.day, 9, 15, tzinfo=IST)
        week_end = datetime(w1.year, w1.month, w1.day, 15, 30, tzinfo=IST)
        week_days = kite.historical_data(NIFTY_TOKEN, week_start, week_end, "day") or []
        week_rows = [
            {
                "date": str(b["date"].date() if hasattr(b["date"], "date") else b["date"])[:10],
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": int(b.get("volume") or 0),
            }
            for b in week_days
        ]

        entry = {
            **meta,
            "week_start": w0.isoformat(),
            "week_end": w1.isoformat(),
            "expiry_day": summarize_minutes(mins),
            "expiry_day_bar": (
                {
                    "open": float(day_bar[0]["open"]),
                    "high": float(day_bar[0]["high"]),
                    "low": float(day_bar[0]["low"]),
                    "close": float(day_bar[0]["close"]),
                }
                if day_bar
                else None
            ),
            "week_daily": week_rows,
        }
        report["expiries"].append(entry)

        # Save raw minutes for the expiry day
        raw_path = OUT / f"nifty_minutes_{exp.isoformat()}.json"
        raw_path.write_text(
            json.dumps(
                [
                    {
                        "ts": b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"]),
                        "open": float(b["open"]),
                        "high": float(b["high"]),
                        "low": float(b["low"]),
                        "close": float(b["close"]),
                        "volume": int(b.get("volume") or 0),
                    }
                    for b in mins
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  {exp}  minutes={len(mins)}  saved {raw_path.name}")

    out = OUT / "last_3_expiries.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")

    print("\n=== Last 3 Nifty expiries ===")
    for e in report["expiries"]:
        d = e["expiry_day"] or {}
        print(
            f"  {e['expiry']} ({e['weekday']}, {e['type']})  "
            f"spot {d.get('open')}→{d.get('close')}  "
            f"Δ{d.get('change')}  range {d.get('range')}  | {e['note']}"
        )


if __name__ == "__main__":
    main()

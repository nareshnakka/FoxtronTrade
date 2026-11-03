"""Fetch Nifty 1-minute bars for a date range into data/expiries/last_3_months/minutes."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")
OUT = Path("data/expiries/last_3_months/minutes")
NIFTY = 256265


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    kite = get_authenticated_kite()

    y1, m1, d1 = map(int, args.date_from.split("-"))
    y2, m2, d2 = map(int, args.date_to.split("-"))
    days = (
        kite.historical_data(
            NIFTY,
            datetime(y1, m1, d1, tzinfo=IST),
            datetime(y2, m2, d2, 23, 59, tzinfo=IST),
            "day",
        )
        or []
    )
    print(f"Trading days {args.date_from}→{args.date_to}: {len(days)}", flush=True)
    ok = skip = fail = 0
    for i, d in enumerate(days, 1):
        dt = d["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        else:
            dt = dt.replace(tzinfo=IST)
        day = dt.date().isoformat()
        path = OUT / f"nifty_{day}.json"
        if path.exists() and path.stat().st_size > 2000:
            skip += 1
            continue
        start = datetime(dt.year, dt.month, dt.day, 9, 15, tzinfo=IST)
        end = datetime(dt.year, dt.month, dt.day, 15, 30, tzinfo=IST)
        try:
            rows = kite.historical_data(NIFTY, start, end, "minute") or []
            out = []
            for r in rows:
                t = r["date"]
                if getattr(t, "tzinfo", None):
                    t = t.astimezone(IST)
                else:
                    t = t.replace(tzinfo=IST)
                out.append(
                    {
                        "ts": t.isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": int(r.get("volume") or 0),
                    }
                )
            if len(out) < 50:
                print(f"  WARN {day} only {len(out)} bars", flush=True)
            path.write_text(json.dumps(out, indent=2), encoding="utf-8")
            ok += 1
            if ok % 10 == 0 or i == len(days):
                print(
                    f"  progress fetched={ok} skip={skip} fail={fail} "
                    f"last={day} bars={len(out)}",
                    flush=True,
                )
            time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {day}: {exc}", flush=True)
            time.sleep(1.0)
    n = len(list(OUT.glob("nifty_*.json")))
    print(f"DONE fetched={ok} skipped={skip} fail={fail} minute_files={n}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

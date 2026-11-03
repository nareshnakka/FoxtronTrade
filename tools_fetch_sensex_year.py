"""
Fetch ~1y Sensex data:
  • Index 1-minute bars → data/sensex/minutes/sensex_YYYY-MM-DD.json
  • Near FUT (+OI when Kite allows) → data/sensex/fut_oi/sensex_fut_YYYY-MM-DD.json
  • Near-week ATM CE/PE minutes (live contracts only) → data/sensex/options/

Expired option chains are usually unavailable on Kite; spot + synth premium
powers FastTrack-style backtests the same way as Nifty.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path("data/sensex")
MINUTES = ROOT / "minutes"
FUT_OI = ROOT / "fut_oi"
OPTIONS = ROOT / "options"
META = ROOT / "meta.json"
SENSEX_TOKEN = 265  # BSE INDICES SENSEX


def _iso_day(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo:
            dt = dt.astimezone(IST)
        return dt.date().isoformat()
    return dt.isoformat()


def _bars_to_rows(rows: list) -> list[dict]:
    out = []
    for r in rows:
        t = r["date"]
        if getattr(t, "tzinfo", None):
            t = t.astimezone(IST)
        else:
            t = t.replace(tzinfo=IST)
        row = {
            "ts": t.isoformat(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r.get("volume") or 0),
        }
        if r.get("oi") is not None:
            row["oi"] = int(r.get("oi") or 0)
        out.append(row)
    return out


def _trading_days(kite, d0: date, d1: date) -> list[date]:
    rows = (
        kite.historical_data(
            SENSEX_TOKEN,
            datetime(d0.year, d0.month, d0.day, tzinfo=IST),
            datetime(d1.year, d1.month, d1.day, 23, 59, tzinfo=IST),
            "day",
        )
        or []
    )
    days: list[date] = []
    for r in rows:
        dt = r["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        days.append(dt.date() if hasattr(dt, "date") else dt)
    return days


def fetch_spot(kite, days: list[date], sleep: float) -> dict[str, int]:
    MINUTES.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    for i, day in enumerate(days, 1):
        path = MINUTES / f"sensex_{day.isoformat()}.json"
        if path.exists() and path.stat().st_size > 2000:
            skip += 1
            continue
        start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
        try:
            rows = kite.historical_data(SENSEX_TOKEN, start, end, "minute") or []
            out = _bars_to_rows(rows)
            if len(out) < 50:
                print(f"  WARN spot {day} only {len(out)} bars", flush=True)
            path.write_text(json.dumps(out), encoding="utf-8")
            ok += 1
            if ok % 15 == 0 or i == len(days):
                print(
                    f"  spot progress {i}/{len(days)} fetched={ok} skip={skip} "
                    f"fail={fail} last={day} bars={len(out)}",
                    flush=True,
                )
            time.sleep(sleep)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL spot {day}: {exc}", flush=True)
            time.sleep(max(1.0, sleep * 4))
    return {"ok": ok, "skip": skip, "fail": fail}


def _near_sensex_fut(kite) -> dict | None:
    bfo = kite.instruments("BFO")
    futs = [
        i
        for i in bfo
        if i.get("name") == "SENSEX" and i.get("instrument_type") == "FUT"
    ]
    today = datetime.now(IST).date()
    futs = [
        i
        for i in futs
        if _as_date(i.get("expiry")) and _as_date(i.get("expiry")) >= today
    ]
    futs.sort(key=lambda x: _as_date(x.get("expiry")) or date.max)
    return futs[0] if futs else None


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def fetch_fut_oi(kite, days: list[date], sleep: float, lookback_days: int) -> dict:
    """OI history usually only for the *current* FUT contract — recent window."""
    FUT_OI.mkdir(parents=True, exist_ok=True)
    fut = _near_sensex_fut(kite)
    if not fut:
        print("  No live SENSEX FUT — skip OI", flush=True)
        return {"ok": 0, "skip": 0, "fail": 0, "symbol": None}
    tok = int(fut["instrument_token"])
    sym = str(fut["tradingsymbol"])
    print(f"  FUT {sym} expiry={fut.get('expiry')} token={tok}", flush=True)
    cutoff = datetime.now(IST).date() - timedelta(days=lookback_days)
    use = [d for d in days if d >= cutoff]
    ok = skip = fail = 0
    for i, day in enumerate(use, 1):
        path = FUT_OI / f"sensex_fut_{day.isoformat()}.json"
        if path.exists() and path.stat().st_size > 500:
            skip += 1
            continue
        start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
        try:
            rows = kite.historical_data(tok, start, end, "minute", oi=True) or []
            out = _bars_to_rows(rows)
            path.write_text(
                json.dumps(
                    {"symbol": sym, "token": tok, "expiry": str(fut.get("expiry")), "bars": out},
                    default=str,
                ),
                encoding="utf-8",
            )
            ok += 1
            if ok % 10 == 0 or i == len(use):
                print(
                    f"  fut progress {i}/{len(use)} fetched={ok} bars={len(out)} last={day}",
                    flush=True,
                )
            time.sleep(sleep)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL fut {day}: {exc}", flush=True)
            time.sleep(max(1.0, sleep * 4))
    return {"ok": ok, "skip": skip, "fail": fail, "symbol": sym}


def fetch_near_atm_options(kite, sleep: float) -> dict:
    """Best-effort: current week ATM CE/PE minutes (live contracts only)."""
    OPTIONS.mkdir(parents=True, exist_ok=True)
    bfo = kite.instruments("BFO")
    today = datetime.now(IST).date()
    opts = [
        i
        for i in bfo
        if i.get("name") == "SENSEX"
        and i.get("instrument_type") in ("CE", "PE")
        and _as_date(i.get("expiry"))
        and _as_date(i.get("expiry")) >= today
    ]
    if not opts:
        return {"ok": 0, "reason": "no opts"}
    # spot for ATM
    q = kite.ltp("BSE:SENSEX") or {}
    spot = float(next(iter(q.values())).get("last_price") or 0)
    if spot <= 0:
        return {"ok": 0, "reason": "no spot"}
    # nearest expiry
    exps = sorted({_as_date(i["expiry"]) for i in opts if _as_date(i.get("expiry"))})
    exp = exps[0]
    week = [i for i in opts if _as_date(i.get("expiry")) == exp]
    # round strike to 100
    atm = int(round(spot / 100.0) * 100)
    picked = []
    for side in ("CE", "PE"):
        cands = [i for i in week if i.get("instrument_type") == side]
        cands.sort(key=lambda x: abs(float(x.get("strike") or 0) - atm))
        if cands:
            picked.append(cands[0])
    ok = 0
    # last 10 sessions only — older may 404 for weekly options
    for leg in picked:
        tok = int(leg["instrument_token"])
        sym = str(leg["tradingsymbol"])
        for back in range(0, 10):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            path = OPTIONS / f"{sym}_{day.isoformat()}.json"
            if path.exists() and path.stat().st_size > 200:
                continue
            start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
            end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
            try:
                rows = kite.historical_data(tok, start, end, "minute") or []
                out = _bars_to_rows(rows)
                if len(out) < 20:
                    continue
                path.write_text(
                    json.dumps(
                        {
                            "symbol": sym,
                            "strike": leg.get("strike"),
                            "side": leg.get("instrument_type"),
                            "expiry": str(leg.get("expiry")),
                            "lot_size": leg.get("lot_size"),
                            "bars": out,
                        },
                        default=str,
                    ),
                    encoding="utf-8",
                )
                ok += 1
                print(f"  opt {sym} {day} bars={len(out)}", flush=True)
                time.sleep(sleep)
            except Exception as exc:  # noqa: BLE001
                print(f"  opt skip {sym} {day}: {exc}", flush=True)
                time.sleep(sleep)
    return {"ok": ok, "atm": atm, "expiry": str(exp), "spot": spot}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=1.0)
    ap.add_argument("--sleep", type=float, default=0.28)
    ap.add_argument("--fut-lookback", type=int, default=60, help="calendar days of FUT+OI")
    ap.add_argument("--skip-spot", action="store_true")
    ap.add_argument("--skip-fut", action="store_true")
    ap.add_argument("--skip-opts", action="store_true")
    args = ap.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    kite = get_authenticated_kite()
    end = datetime.now(IST).date()
    start = end - timedelta(days=int(365 * args.years) + 5)
    print(f"Sensex fetch {start}→{end}", flush=True)
    days = _trading_days(kite, start, end)
    print(f"Trading days listed: {len(days)}", flush=True)

    spot_stats = {"ok": 0, "skip": 0, "fail": 0}
    fut_stats: dict = {}
    opt_stats: dict = {}
    if not args.skip_spot:
        print("=== SPOT minutes ===", flush=True)
        spot_stats = fetch_spot(kite, days, args.sleep)
    if not args.skip_fut:
        print("=== FUT + OI (recent) ===", flush=True)
        fut_stats = fetch_fut_oi(kite, days, args.sleep, args.fut_lookback)
    if not args.skip_opts:
        print("=== Near ATM options (live week) ===", flush=True)
        opt_stats = fetch_near_atm_options(kite, args.sleep)

    meta = {
        "generated_at": datetime.now(IST).isoformat(),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "trading_days": len(days),
        "spot": spot_stats,
        "fut_oi": fut_stats,
        "options": opt_stats,
        "spot_files": len(list(MINUTES.glob("sensex_*.json"))),
        "fut_files": len(list(FUT_OI.glob("sensex_fut_*.json"))),
        "opt_files": len(list(OPTIONS.glob("*.json"))),
        "index_token": SENSEX_TOKEN,
        "note": (
            "Full 1y option chain history is not available on Kite for expired "
            "weeklies; FastTrack Sensex backtests use spot + synthetic ATM premium."
        ),
    }
    META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("META", META, flush=True)
    print(json.dumps(meta, indent=2), flush=True)
    return 0 if spot_stats.get("fail", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

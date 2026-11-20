"""
Export a captured live SQLite day → option_chain_1m/<day>/viewer.json
so playback_sim / backtest can replay it bar-by-bar.
"""
from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "option_chain_live" / "chain.db"
OUT_ROOT = ROOT / "data" / "option_chain_1m"
STEP = 50
WING = 10


def _db_path(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    raw = cfg.get("option_chain_db_path") or str(DEFAULT_DB)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def export_live_day_to_1m(
    day: str,
    *,
    week: str | None = None,
    cfg: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Build option_chain_1m/<day>/ from chain.db.
    Returns {day, path, frames, week, source, skipped?}.
    """
    out = OUT_ROOT / day
    viewer_path = out / "viewer.json"
    if viewer_path.exists() and not force:
        return {
            "day": day,
            "path": str(out),
            "skipped": True,
            "reason": "viewer.json already exists",
            "source": "option_chain_1m",
        }

    db = _db_path(cfg)
    if not db.exists():
        raise FileNotFoundError(f"chain db missing: {db}")

    con = sqlite3.connect(str(db))
    try:
        ticks = con.execute(
            "SELECT ts, spot, atm, current_expiry, next_expiry FROM ltp_ticks "
            "WHERE day=? ORDER BY ts",
            (day,),
        ).fetchall()
        if not ticks:
            raise ValueError(f"no ticks for {day}")

        cur_exp = ticks[0][3]
        nxt_exp = ticks[0][4]
        if week is None:
            week = "next" if str(cur_exp)[:10] == day else "current"
        trade_exp = nxt_exp if week == "next" else cur_exp

        legs_raw = con.execute(
            "SELECT ts, strike, side, symbol, ltp FROM ltp_legs "
            "WHERE week=? AND substr(ts,1,10)=? ORDER BY ts",
            (week, day),
        ).fetchall()
    finally:
        con.close()

    by_min_sym: dict[tuple[str, str], list[float]] = defaultdict(list)
    sym_meta: dict[str, tuple[int, str]] = {}
    for ts, strike, side, symbol, ltp in legs_raw:
        if ltp is None:
            continue
        minute = str(ts)[:16]
        by_min_sym[(minute, symbol)].append(float(ltp))
        sym_meta[symbol] = (int(strike), str(side))

    spot_by_min: dict[str, list[float]] = defaultdict(list)
    atm_by_min: dict[str, int] = {}
    for ts, spot, atm, _, _ in ticks:
        if spot is None:
            continue
        minute = str(ts)[:16]
        spot_by_min[minute].append(float(spot))
        if atm is not None:
            atm_by_min[minute] = int(atm)

    minutes = sorted(set(spot_by_min) | {m for m, _ in by_min_sym})
    minutes = [m for m in minutes if "09:15" <= m[11:16] <= "15:30"]
    if not minutes:
        raise ValueError(f"no session minutes for {day}")

    out.mkdir(parents=True, exist_ok=True)
    spot_path = out / "NIFTY_SPOT_1m.csv"
    with spot_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["datetime", "open", "high", "low", "close", "volume"]
        )
        w.writeheader()
        for m in minutes:
            xs = spot_by_min.get(m) or []
            if not xs:
                continue
            w.writerow(
                {
                    "datetime": m.replace("T", " ") + ":00",
                    "open": xs[0],
                    "high": max(xs),
                    "low": min(xs),
                    "close": xs[-1],
                    "volume": 0,
                }
            )

    leg_ohlc: dict[str, dict[tuple[str, int], dict[str, Any]]] = defaultdict(dict)
    all_strikes: set[int] = set()
    for m in minutes:
        for sym, (strike, side) in sym_meta.items():
            xs = by_min_sym.get((m, sym))
            if not xs:
                continue
            all_strikes.add(strike)
            leg_ohlc[m][(side, strike)] = {
                "o": xs[0],
                "h": max(xs),
                "l": min(xs),
                "c": xs[-1],
                "v": 0,
                "oi": 0,
                "sym": sym,
            }

    frames: list[dict[str, Any]] = []
    atm_seen: set[int] = set()
    for m in minutes:
        xs = spot_by_min.get(m) or []
        if not xs:
            continue
        spot_c = xs[-1]
        atm = atm_by_min.get(m) or int(round(spot_c / STEP) * STEP)
        atm_seen.add(atm)
        wing_strikes = [atm + STEP * i for i in range(WING, -WING - 1, -1)]
        legs: dict[str, Any] = {}
        for k in wing_strikes:
            for side in ("CE", "PE"):
                r = leg_ohlc[m].get((side, k))
                if not r:
                    continue
                if k == atm:
                    role = "ATM"
                elif side == "CE":
                    role = (
                        f"ITM+{(atm - k) // STEP}"
                        if k < atm
                        else f"OTM+{(k - atm) // STEP}"
                    )
                else:
                    role = (
                        f"ITM+{(k - atm) // STEP}"
                        if k > atm
                        else f"OTM+{(atm - k) // STEP}"
                    )
                legs[f"{side}:{k}"] = {**r, "role": role}
        frames.append(
            {
                "t": m[11:16],
                "datetime": m.replace("T", " ") + ":00",
                "spot": spot_c,
                "atm": atm,
                "strikes": wing_strikes,
                "legs": legs,
            }
        )

    import json

    meta = {
        "day": day,
        "expiry": str(trade_exp),
        "source": "option_chain_live",
        "week": week,
        "atm_open": frames[0]["atm"] if frames else None,
        "atm_dynamic": True,
        "atm_seen": sorted(atm_seen),
        "wing": WING,
        "window": "09:15-15:30 IST",
        "spot_open": frames[0]["spot"] if frames else None,
        "strike_universe": sorted(all_strikes, reverse=True),
        "current_expiry_scan": str(cur_exp),
        "next_expiry_scan": str(nxt_exp),
    }
    viewer = {"meta": meta, "strikes": sorted(all_strikes, reverse=True), "frames": frames}
    viewer_path.write_text(json.dumps(viewer, separators=(",", ":")), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {
        "day": day,
        "path": str(out),
        "frames": len(frames),
        "week": week,
        "source": "option_chain_live",
        "skipped": False,
    }

"""
Backtest trained day-type adaptive exits on last 5 days (causal).

Simulates first merge, then each bar updates DayTypeTracker and exits:
  LOCK_145 / FALLEN_WATCH → book both at 1.45× (or EOD)
  TRAIL → arm at trail_arm, exit on giveback% from peak (or EOD)
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_final_daily_report as cw
from foxtrade.charges import estimate_option_charges
from foxtrade.config_store import load_config
from foxtrade.day_type import DayTypeTracker

ROOT = Path(__file__).resolve().parent
LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
OUT = ROOT / "data" / "backtests" / "bt_last5_daytype_adaptive.json"
CAPITAL = 100_000.0
DEPLOY = 0.90


def _charges(entry: float, exit_px: float, qty: int, cfg: dict) -> float:
    try:
        return float(
            estimate_option_charges(entry, exit_px, qty, cfg=cfg).get("charges") or 0
        )
    except Exception:
        return 0.0


def _leg(fr: dict, side: str, k: int) -> float | None:
    row = (fr.get("legs") or {}).get(f"{side}:{k}")
    if not row or row.get("c") is None:
        return None
    return float(row["c"])


def _hm(t: str) -> int:
    try:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def simulate_day(day: str, cfg: dict, lot: int) -> dict:
    viewer = json.loads((cw.CHAIN / day / "viewer.json").read_text(encoding="utf-8"))
    frames = list(viewer.get("frames") or [])
    cw.BAND_LO, cw.BAND_HI = 70.0, 77.0
    cw.BAND_CENTER = 73.5
    merge = cw.find_merge_after(frames, after_t="09:20", before_t=None, exclude_pairs=set())
    if not merge:
        return {"day": day, "net": 0.0, "setups": 0, "note": "no merge"}

    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    lots = max(1, int((CAPITAL * DEPLOY) // ((ce0 + pe0) * lot)))
    qty = lots * lot
    tracker = DayTypeTracker()
    # synthetic minute timestamps from HH:MM
    entry_ts = _hm(entry_t) * 60.0
    tracker.reset(ce_entry=ce0, pe_entry=pe0, ts=entry_ts)

    last_ce, last_pe = ce0, pe0
    peak_win = ce0
    armed = False
    arm_side = "CE"
    exit_t = entry_t
    exit_why = "EOD"
    day_type_at_exit = "UNKNOWN"
    mode_at_exit = "LOCK_145"

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t or t > "15:25":
            continue
        ce = _leg(fr, "CE", ce_k)
        pe = _leg(fr, "PE", pe_k)
        if ce is not None:
            last_ce = ce
        if pe is not None:
            last_pe = pe
        if ce is None and pe is None:
            continue
        ts = _hm(t) * 60.0
        snap = tracker.update(ce_ltp=last_ce, pe_ltp=last_pe, ts=ts, cfg=cfg)
        day_type_at_exit = snap.day_type
        mode_at_exit = snap.exit_mode
        ce_m, pe_m = last_ce / ce0, last_pe / pe0
        win = "CE" if ce_m >= pe_m else "PE"
        win_ltp = last_ce if win == "CE" else last_pe
        win_m = ce_m if win == "CE" else pe_m
        exit_mult = float(snap.pair_exit_mult or cfg.get("algo_cw_pair_exit_mult") or 1.45)

        if snap.exit_mode in ("LOCK_145", "FALLEN_WATCH"):
            if win_m >= exit_mult:
                exit_t, exit_why = t, f"{snap.day_type}/{snap.exit_mode} hit {win} {win_m:.2f}×"
                break
        else:  # TRAIL
            arm = float(snap.trail_arm_mult or 1.30)
            gb = float(snap.trail_giveback_pct or 10) / 100.0
            if not armed and win_m >= arm:
                armed = True
                arm_side = win
                peak_win = win_ltp
            if armed:
                if win == arm_side:
                    peak_win = max(peak_win, win_ltp)
                else:
                    # switch if other side overtakes
                    if win_m >= arm:
                        arm_side = win
                        peak_win = win_ltp
                now = last_ce if arm_side == "CE" else last_pe
                if peak_win > 0 and (peak_win - now) / peak_win >= gb:
                    exit_t = t
                    exit_why = (
                        f"{snap.day_type}/TRAIL {arm_side} peak={peak_win:.1f} "
                        f"now={now:.1f} dd={(peak_win-now)/peak_win*100:.1f}%"
                    )
                    break
                # also book if hit classic exit while trailing weakly
                if win_m >= exit_mult * 1.15 and (peak_win - now) / peak_win >= gb * 0.5:
                    exit_t, exit_why = t, f"{snap.day_type}/TRAIL soft-book {win} {win_m:.2f}×"
                    break
        exit_t = t
    else:
        exit_why = f"EOD {day_type_at_exit}/{mode_at_exit}"

    ce_pnl = (last_ce - ce0) * qty
    pe_pnl = (last_pe - pe0) * qty
    chg = _charges(ce0, last_ce, qty, cfg) + _charges(pe0, last_pe, qty, cfg)
    gross = ce_pnl + pe_pnl
    net = gross - chg
    return {
        "day": day,
        "setups": 1,
        "entry_t": entry_t,
        "exit_t": exit_t,
        "exit_why": exit_why,
        "day_type": day_type_at_exit,
        "exit_mode": mode_at_exit,
        "ce_strike": ce_k,
        "pe_strike": pe_k,
        "ce_entry": ce0,
        "pe_entry": pe0,
        "ce_exit": round(last_ce, 2),
        "pe_exit": round(last_pe, 2),
        "ce_mult": round(last_ce / ce0, 3),
        "pe_mult": round(last_pe / pe0, 3),
        "lots": lots,
        "gross": round(gross, 2),
        "charges": round(chg, 2),
        "net": round(net, 2),
        "early_tells": tracker.snap.reasons,
    }


def main() -> None:
    cfg = load_config()
    cfg = dict(cfg)
    cfg["day_type_adaptive"] = True
    lot = int(cfg.get("lot_size") or 75)
    daily = [simulate_day(d, cfg, lot) for d in LAST5 if (cw.CHAIN / d / "viewer.json").exists()]
    nets = [float(d["net"]) for d in daily]
    green = sum(1 for n in nets if n > 0)
    red = sum(1 for n in nets if n < 0)
    total = round(sum(nets), 2)

    print("=" * 88)
    print("TRAINED DAY-TYPE ADAPTIVE — last 5 days (causal)")
    print("=" * 88)
    print(f"TOTAL NET ₹{total:,.2f}  |  {green}G/{red}R  |  worst ₹{min(nets):,.2f}")
    for d in daily:
        print(
            f"  {d['day']}  {d.get('day_type')}/{d.get('exit_mode')}  "
            f"{d.get('entry_t')}→{d.get('exit_t')}  "
            f"CE{d.get('ce_mult')}× PE{d.get('pe_mult')}×  NET ₹{d['net']:,.2f}"
        )
        print(f"    {d.get('exit_why')}")
        if d.get("early_tells"):
            print(f"    tells: {'; '.join(d['early_tells'][:2])}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "strategy": "day_type_adaptive",
                "summary": {
                    "net": total,
                    "green": green,
                    "red": red,
                    "worst": min(nets) if nets else 0,
                },
                "daily": daily,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()

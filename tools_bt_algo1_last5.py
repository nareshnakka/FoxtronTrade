"""
Algo1 Convergence Dual — causal last-5-day backtest (no look-ahead).

Uses trained Algo1 rules only (Algo-CW untouched):
  70–77 merge · exit 1.45× · lock day if green (skip 2nd) · day-type adaptive
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_algo1_exit_learn as L
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config
from foxtrade.day_type import DayTypeTracker

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "backtests"
LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    frames: dict[str, list] = {}
    for d in LAST5:
        path = cw.CHAIN / d / "viewer.json"
        if not path.exists():
            continue
        frames[d] = list(json.loads(path.read_text(encoding="utf-8")).get("frames") or [])

    days = [d for d in LAST5 if d in frames]

    # Algo1 trained policy (NOT Algo-CW)
    skip_ge = float(cfg.get("algo1_skip_second_if_day_pnl_ge", 1) or 1)
    max_s = int(cfg.get("algo1_max_setups_per_day", 2) or 2)
    exit_m = float(cfg.get("algo1_pair_exit_mult", 1.45) or 1.45)
    live = L.Policy(
        "algo1_lockgreen_daytype",
        exit_m,
        "hit",
        skip_2nd_if_net_ge=skip_ge,
        max_setups=max_s,
    )

    daily, sc = L.run_universe(days, frames, policy=live, lot_size=lot, cfg=cfg)

    # Tag day-type at exit for each first setup (causal walk)
    for day in daily:
        fr = frames[day["day"]]
        setups = day.get("setups") or []
        if not setups:
            day["day_type"] = None
            continue
        s0 = setups[0]
        tr = DayTypeTracker()
        tr.reset(ce_entry=float(s0["ce_entry"]), pe_entry=float(s0["pe_entry"]), ts=_hm(s0["entry_t"]) * 60)
        last_type = "NORMAL"
        for frb in fr:
            t = frb.get("t") or ""
            if t < s0["entry_t"] or t > s0["exit_t"]:
                continue
            legs = frb.get("legs") or {}
            ce = legs.get(f"CE:{s0['ce_strike']}")
            pe = legs.get(f"PE:{s0['pe_strike']}")
            if not ce or ce.get("c") is None or not pe or pe.get("c") is None:
                continue
            snap = tr.update(
                ce_ltp=float(ce["c"]),
                pe_ltp=float(pe["c"]),
                ts=_hm(t) * 60,
                cfg={
                    **cfg,
                    "algo_cw_pair_exit_mult": exit_m,
                    "day_type_adaptive": True,
                },
            )
            last_type = snap.day_type
        day["day_type"] = last_type
        day["exit_mode"] = tr.snap.exit_mode

    lines = [
        "=" * 88,
        "ALGO1 CONVERGENCE DUAL — LAST 5 DAYS CAUSAL REPORT",
        "Rules: 70–77 · exit 1.45× · lock if green · day-type adaptive (Algo1 only)",
        f"Capital ₹{L.CAPITAL:,.0f} × {L.DEPLOY:.0%} · lot={lot} · no look-ahead · Algo-CW unchanged",
        "=" * 88,
        (
            f"TOTAL NET ₹{sc['net']:,.2f}  |  {sc['green_days']}G / {sc['red_days']}R  |  "
            f"worst ₹{sc['worst_day']:,.2f}  |  best ₹{sc['best_day']:,.2f}  |  avg ₹{sc['avg_day']:,.2f}"
        ),
        "=" * 88,
    ]
    print("\n".join(lines))

    for day in daily:
        print(
            f"\n{day['day']}  type={day.get('day_type')}/{day.get('exit_mode')}  "
            f"setups={day['setup_count']}  NET ₹{day['net']:,.2f}"
        )
        lines.append("")
        lines.append(
            f"### {day['day']}  type={day.get('day_type')}/{day.get('exit_mode')}  "
            f"setups={day['setup_count']}  NET ₹{day['net']:,.2f}"
        )
        for s in day.get("setups") or []:
            row = (
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            print(row)
            print(f"    {s.get('exit_why')}")
            lines.append(row)
            lines.append(f"    exit: {s.get('exit_why')}")
        if day["setup_count"] == 1 and float(day["net"]) > 0:
            note = "    locked green day — skipped further Algo1 setups"
            print(note)
            lines.append(note)

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo1_last5_causal.txt"
    js = OUT / "bt_algo1_last5_causal.json"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps({"strategy": "algo1_convergence_dual", "summary": sc, "daily": daily}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")
    print(f"Wrote {js}")


def _hm(t: str) -> int:
    try:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


if __name__ == "__main__":
    main()

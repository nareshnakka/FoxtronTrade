"""Last-5-days causal report for live lock-green Algo-CW/CW pair rules."""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_algo1_exit_learn as L
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
OUT = Path(__file__).resolve().parent / "data" / "backtests"


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    frames: dict[str, list] = {}
    for d in LAST5:
        v = json.loads((cw.CHAIN / d / "viewer.json").read_text(encoding="utf-8"))
        frames[d] = list(v.get("frames") or [])

    live = L.Policy(
        "live_lockgreen_1.45",
        1.45,
        "hit",
        skip_2nd_if_net_ge=float(cfg.get("algo_cw_skip_second_if_day_pnl_ge") or 1),
        max_setups=int(cfg.get("algo_cw_max_setups_per_day") or 2),
    )
    old = L.Policy("old_baseline", 1.45, "hit", skip_2nd_if_net_ge=15000, max_setups=2)

    daily, sc = L.run_universe(LAST5, frames, policy=live, lot_size=lot, cfg=cfg)
    _, sb = L.run_universe(LAST5, frames, policy=old, lot_size=lot, cfg=cfg)

    lines = [
        "=" * 88,
        "ALGO CW / Algo-CW pair — LAST 5 DAYS CAUSAL REPORT",
        "Rules: 70–77 merge · exit 1.45× · lock day if already green (skip 2nd)",
        f"Capital ₹{L.CAPITAL:,.0f} × {L.DEPLOY:.0%} · lot={lot} · no look-ahead",
        "=" * 88,
        (
            f"TOTAL NET ₹{sc['net']:,.2f}  |  {sc['green_days']}G / {sc['red_days']}R  |  "
            f"worst ₹{sc['worst_day']:,.2f}  |  best ₹{sc['best_day']:,.2f}  |  avg ₹{sc['avg_day']:,.2f}"
        ),
        (
            f"(vs old max-2 / skip@₹15k: ₹{sb['net']:,.2f} · {sb['green_days']}G/{sb['red_days']}R · "
            f"worst ₹{sb['worst_day']:,.2f})"
        ),
        "=" * 88,
    ]

    print("\n".join(lines[:8]))

    for day in daily:
        block = [
            "",
            f"### {day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}",
        ]
        print(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            row = (
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  GROSS ₹{s['gross']:,.2f}  CHG ₹{s['charges']:,.2f}  "
                f"NET ₹{s['net']:,.2f}"
            )
            why = f"    exit: {s.get('exit_why')}"
            block.extend([row, why])
            print(row)
            print(why)
        if day["setup_count"] == 1 and float(day["net"]) > 0:
            note = "    locked green day — skipped further setups"
            block.append(note)
            print(note)
        lines.extend(block)

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_last5_lockgreen_report.txt"
    js = OUT / "bt_last5_lockgreen_report.json"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "strategy": "cw_70_77_1.45_lockgreen",
                "days": LAST5,
                "causal": True,
                "summary": sc,
                "baseline_compare": sb,
                "daily": daily,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")
    print(f"Wrote {js}")


if __name__ == "__main__":
    main()

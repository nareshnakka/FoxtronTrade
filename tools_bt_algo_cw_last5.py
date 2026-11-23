"""
Algo-CW Dual — causal last-5-day backtest (no look-ahead).

Live Algo-CW rules only (Algo1 not included):
  70–77 merge · exit 1.45× · max 2/day · skip 2nd if day PnL ≥ ₹15k
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_algo1_exit_learn as L
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

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

    skip_ge = float(cfg.get("algo_cw_skip_second_if_day_pnl_ge") or 15000)
    max_s = int(cfg.get("algo_cw_max_setups_per_day") or 2)
    exit_m = float(cfg.get("algo_cw_pair_exit_mult") or 1.45)
    policy = L.Policy(
        "algo_cw_live",
        exit_m,
        "hit",
        skip_2nd_if_net_ge=skip_ge,
        max_setups=max_s,
    )

    daily, sc = L.run_universe(days, frames, policy=policy, lot_size=lot, cfg=cfg)

    lines = [
        "=" * 88,
        "ALGO-CW — LAST 5 DAYS CAUSAL REPORT",
        f"Rules: 70–77 · exit {exit_m}× · max {max_s}/day · skip 2nd if day ≥ ₹{skip_ge:,.0f}",
        f"Capital ₹{L.CAPITAL:,.0f} × {L.DEPLOY:.0%} · lot={lot} · no look-ahead · Algo1 excluded",
        "=" * 88,
        (
            f"TOTAL NET ₹{sc['net']:,.2f}  |  {sc['green_days']}G / {sc['red_days']}R  |  "
            f"worst ₹{sc['worst_day']:,.2f}  |  best ₹{sc['best_day']:,.2f}  |  avg ₹{sc['avg_day']:,.2f}"
        ),
        "=" * 88,
    ]
    print("\n".join(lines))

    for day in daily:
        print(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            print(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            print(f"    {s.get('exit_why')}")
        if day["setup_count"] == 1 and float(day["net"]) >= skip_ge:
            print(f"    skipped further setups (day ≥ ₹{skip_ge:,.0f})")

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo_cw_last5_causal.txt"
    js = OUT / "bt_algo_cw_last5_causal.json"
    txt.write_text("\n".join(lines) + "\n" + _detail(daily, skip_ge), encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "strategy": "algo_cw_70_77_1.45_skip15k",
                "days": days,
                "causal": True,
                "summary": sc,
                "daily": daily,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")
    print(f"Wrote {js}")


def _detail(daily: list, skip_ge: float) -> str:
    blocks: list[str] = []
    for day in daily:
        blocks.append(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            blocks.append(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            blocks.append(f"    {s.get('exit_why')}")
        if day["setup_count"] == 1 and float(day["net"]) >= skip_ge:
            blocks.append(f"    skipped further setups (day ≥ ₹{skip_ge:,.0f})")
    return "\n".join(blocks) + "\n"


if __name__ == "__main__":
    main()

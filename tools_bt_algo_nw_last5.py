"""
Algo-NW — causal last-5-day backtest (no look-ahead).

Live Algo-NW rules:
  next-week expiry · 46–54 merge · exit 1.70× · max 1/day · ₹1L × 90%
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_1l_dual_daily as bt
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "backtests"
LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    capital = float(cfg.get("algo_nw_capital_inr") or 100_000)
    deploy = float(cfg.get("algo_nw_deploy_pct") or 0.9)
    band_lo = float(cfg.get("algo_nw_premium_low") or 46)
    band_hi = float(cfg.get("algo_nw_premium_high") or 54)
    win_mult = float(cfg.get("algo_nw_pair_exit_mult") or 1.70)
    max_s = int(cfg.get("algo_nw_max_setups_per_day") or 1)

    old_cap, old_dep = nw.CAPITAL, nw.DEPLOY_PCT
    old_band = (bt.NW_BAND, bt.NW_MULT, bt.NW_MAX)
    nw.CAPITAL = capital
    nw.DEPLOY_PCT = deploy
    bt.NW_BAND = (band_lo, band_hi)
    bt.NW_MULT = win_mult
    bt.NW_MAX = max_s

    try:
        daily = []
        for d in LAST5:
            fr, meta = nw.load_nextweek_frames(d)
            daily.append(bt.simulate_nw_day(d, fr, meta, lot_size=lot, cfg=cfg))
    finally:
        nw.CAPITAL, nw.DEPLOY_PCT = old_cap, old_dep
        bt.NW_BAND, bt.NW_MULT, bt.NW_MAX = old_band

    nets = [float(d["net"]) for d in daily]
    green = sum(1 for n in nets if n > 0)
    red = sum(1 for n in nets if n < 0)
    flat = sum(1 for n in nets if n == 0)
    total = round(sum(nets), 2)

    lines = [
        "=" * 88,
        "ALGO-NW — LAST 5 DAYS CAUSAL REPORT (no look-ahead)",
        f"Rules: next-week · {band_lo:g}–{band_hi:g} · exit {win_mult:g}× · max {max_s}/day",
        f"Capital ₹{capital:,.0f} × {deploy:.0%} · lot={lot}",
        "=" * 88,
        (
            f"TOTAL NET ₹{total:,.2f}  |  {green}G / {red}R"
            + (f" / {flat} flat" if flat else "")
            + f"  |  worst ₹{min(nets):,.2f}  |  best ₹{max(nets):,.2f}  |  avg ₹{total/len(nets):,.2f}"
        ),
        "=" * 88,
    ]
    print("\n".join(lines))

    for day in daily:
        note = f"  ({day.get('note')})" if day.get("note") else ""
        exp = day.get("expiry") or ""
        print(
            f"\n{day['day']}  expiry={exp}  setups={day['setup_count']}  "
            f"NET ₹{day['net']:,.2f}{note}"
        )
        lines.append(
            f"\n{day['day']}  expiry={exp}  setups={day['setup_count']}  "
            f"NET ₹{day['net']:,.2f}{note}"
        )
        for s in day.get("setups") or []:
            row = (
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  GROSS ₹{s['gross']:,.2f}  CHG ₹{s['charges']:,.2f}  "
                f"NET ₹{s['net']:,.2f}"
            )
            why = f"    exit: {s.get('exit_why')}"
            print(row)
            print(why)
            lines.extend([row, why])

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo_nw_last5_causal.txt"
    js = OUT / "bt_algo_nw_last5_causal.json"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "strategy": "algo_nw_46_54_1.70x_max1",
                "days": LAST5,
                "causal": True,
                "rules": {
                    "band": [band_lo, band_hi],
                    "exit_mult": win_mult,
                    "max_setups": max_s,
                    "capital": capital,
                    "deploy": deploy,
                },
                "summary": {
                    "net": total,
                    "green": green,
                    "red": red,
                    "worst": min(nets),
                    "best": max(nets),
                    "avg": round(total / len(nets), 2),
                },
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

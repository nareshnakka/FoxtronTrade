"""
Causal (no look-ahead) dual backtest for the last 5 trading days on disk.

Uses walk-forward merge/exit from tools_bt_final_daily_report / nextweek reports.
Live-aligned profiles:
  CW / Algo-CW pair: 70–77 · exit 1.45× · max 2/day · ₹1L × 90%
  NW / Algo-NW:    46–54 · exit 1.70× · max 1/day · ₹1L × 90%
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_1l_dual_daily as bt
import tools_bt_final_daily_report as cw
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "backtests"
CAPITAL = 100_000.0
DEPLOY = 0.90
DAYS = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def _summarize(label: str, daily: list[dict], band: tuple[float, float], mult: float, mx: int) -> dict:
    all_s = [s for d in daily for s in (d.get("setups") or [])]
    net = round(sum(float(d.get("net") or 0) for d in daily), 2)
    gross = round(sum(float(d.get("gross") or 0) for d in daily), 2)
    chg = round(sum(float(d.get("charges") or 0) for d in daily), 2)
    sw = sum(1 for s in all_s if float(s.get("net") or 0) > 0)
    sl = sum(1 for s in all_s if float(s.get("net") or 0) < 0)
    print("=" * 72)
    print(f"{label}  {band[0]:g}–{band[1]:g} @{mult:g}× max{mx}/day  ₹{CAPITAL:,.0f}")
    print(f"NET ₹{net:,.2f}  GROSS ₹{gross:,.2f}  charges ₹{chg:,.2f}  setups {sw}W/{sl}L")
    print("=" * 72)
    for d in daily:
        note = d.get("note") or ""
        print(f"  {d['day']}  setups={d['setup_count']}  NET ₹{float(d['net']):,.2f}  {note}")
        for s in d.get("setups") or []:
            print(
                f"    #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            print(f"      exit: {s.get('exit_why')}")
    return {
        "label": label,
        "band": list(band),
        "win_mult": mult,
        "max_setups": mx,
        "net": net,
        "gross": gross,
        "charges": chg,
        "setup_wins": sw,
        "setup_losses": sl,
        "daily": daily,
    }


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    for mod in (cw, nw):
        mod.CAPITAL = CAPITAL
        mod.DEPLOY_PCT = DEPLOY

    # CW / live algo2 pair band
    old = (cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.WIN_MULT, cw.MAX_SETUPS)
    cw.BAND_LO, cw.BAND_HI = 70.0, 77.0
    cw.BAND_CENTER = (70.0 + 77.0) / 2.0
    cw.WIN_MULT = 1.45
    cw.MAX_SETUPS = 2
    try:
        cw_daily = [cw.simulate_day(d, lot_size=lot, cfg=cfg) for d in DAYS]
    finally:
        cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.WIN_MULT, cw.MAX_SETUPS = old

    nw_daily = []
    for d in DAYS:
        fr, meta = nw.load_nextweek_frames(d)
        nw_daily.append(bt.simulate_nw_day(d, fr, meta, lot_size=lot, cfg=cfg))

    cw_rep = _summarize("CW / Algo-CW pair", cw_daily, (70.0, 77.0), 1.45, 2)
    nw_rep = _summarize("NW / Algo-NW", nw_daily, (46.0, 54.0), 1.70, 1)
    combined = round(float(cw_rep["net"]) + float(nw_rep["net"]), 2)

    print("=" * 72)
    print(
        f"COMBINED LAST 5 DAYS NET ₹{combined:,.2f}  "
        f"(CW {cw_rep['net']:,.2f} + NW {nw_rep['net']:,.2f})"
    )
    print("Causal: merges/exits only use bars at/after decision time (no look-ahead).")
    print("=" * 72)

    OUT.mkdir(parents=True, exist_ok=True)
    rep = {
        "days": DAYS,
        "causal": True,
        "capital": CAPITAL,
        "deploy_pct": DEPLOY,
        "cw": cw_rep,
        "nw": nw_rep,
        "combined_net": combined,
    }
    path = OUT / "bt_last5_causal.json"
    path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()

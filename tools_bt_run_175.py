"""Run current-week + next-week dual BT at 1.75× with full daywise detail."""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_final_daily_report as cw
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

MULT = 1.75
ROOT = Path(__file__).resolve().parent


def _write_report(
    *,
    label: str,
    daily: list[dict],
    out_json: Path,
    out_txt: Path,
) -> None:
    traded = [d for d in daily if d.get("setups")]
    all_setups = [{**s, "day": d["day"]} for d in daily for s in (d.get("setups") or [])]
    net = round(sum(float(d.get("net") or 0) for d in daily), 2)
    gross = round(sum(float(d.get("gross") or 0) for d in daily), 2)
    charges = round(sum(float(d.get("charges") or 0) for d in daily), 2)
    sw = sum(1 for s in all_setups if float(s.get("net") or 0) > 0)
    sl = sum(1 for s in all_setups if float(s.get("net") or 0) < 0)
    dw = sum(1 for d in traded if float(d.get("net") or 0) > 0)
    dl = sum(1 for d in traded if float(d.get("net") or 0) < 0)

    report = {
        "strategy": f"{label}_dual_70_77_{MULT:g}x_walkforward",
        "win_mult": MULT,
        "summary": {
            "net": net,
            "gross": gross,
            "charges": charges,
            "setup_wins": sw,
            "setup_losses": sl,
            "day_wins": dw,
            "day_losses": dl,
            "setups": len(all_setups),
        },
        "daily": daily,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 96,
        f"{label.upper()} DAILY DETAILED REPORT — Dual 70–77 @ {MULT:g}× (walk-forward)",
        f"TOTAL NET Rs {net:,.2f}  |  Gross {gross:,.2f}  |  Charges {charges:,.2f}",
        f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L · exit both @{MULT:g}×",
        "=" * 96,
    ]
    print("=" * 72)
    print(f"{label.upper()} @ {MULT:g}×")
    print(f"NET ₹{net:,.2f}  GROSS ₹{gross:,.2f}  charges ₹{charges:,.2f}")
    print(f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L")
    print("=" * 72)

    for day in daily:
        exp = day.get("expiry")
        exp_s = f"  exp={exp}" if exp else ""
        lines.append("")
        lines.append(
            f"### {day['day']}{exp_s}  |  setups={day['setup_count']}  |  "
            f"NET Rs {float(day['net']):,.2f}  |  "
            f"Gross {float(day['gross']):,.2f}  |  Charges {float(day['charges']):,.2f}"
        )
        if day.get("note"):
            lines.append(f"    {day['note']}")
        if day.get("skipped_2nd_paid_well"):
            lines.append("    skipped 2nd — day already ≥ ₹15,000")
        print(
            f"\n{day['day']}{exp_s}  setups={day['setup_count']}  "
            f"NET ₹{float(day['net']):,.2f}  {day.get('note') or ''}"
        )
        for s in day.get("setups") or []:
            lines.append(
                f"  Setup #{s['setup_id']}  {s['entry_t']} → {s['exit_t']}  "
                f"ATM {s['atm']} mid {s['mid']} gap {s['gap']}  "
                f"win={s['win_side']} lots={s['lots']} (qty {s['qty_per_leg']}/leg)  "
                f"deploy ₹{s['deploy_used']:,.0f}  "
                f"GROSS Rs {s['gross']:,.2f}  CHG Rs {s['charges']:,.2f}  "
                f"NET Rs {s['net']:,.2f}"
            )
            lines.append(f"    exit: {s.get('exit_why')}")
            lines.append(
                f"    CE K{s['ce_strike']}  entry {s['entry_t']} @{s['ce_entry']}  "
                f"→ exit {s['exit_t']} @{s['ce_exit']} ({s['ce_mult']}×)"
            )
            lines.append(
                f"    PE K{s['pe_strike']}  entry {s['entry_t']} @{s['pe_entry']}  "
                f"→ exit {s['exit_t']} @{s['pe_exit']} ({s['pe_mult']}×)"
            )
            print(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"win={s['win_side']} lots={s['lots']}  "
                f"G {s['gross']:,.0f} CHG {s['charges']:,.0f} NET ₹{s['net']:,.2f}"
            )
            for leg in s.get("legs") or []:
                lines.append(
                    f"      {leg['side']:2} K{leg['strike']}  "
                    f"IN  {leg['entry_t']} @ {leg['entry']}  "
                    f"OUT {leg['exit_t']} @ {leg['exit']} ({leg['mult']}×)  "
                    f"qty {leg['qty']}  "
                    f"PnL {leg['pnl']:,.2f}  Chg {leg['charges']:,.2f}  "
                    f"Net {leg['net']:,.2f}  | {leg['reason']}"
                )
                print(
                    f"      {leg['side']} K{leg['strike']}  "
                    f"{leg['entry_t']}@{leg['entry']} → {leg['exit_t']}@{leg['exit']} "
                    f"({leg['mult']}×) qty={leg['qty']}  "
                    f"PnL {leg['pnl']:,.2f} Chg {leg['charges']:,.2f} Net {leg['net']:,.2f}"
                )

    lines.append("")
    lines.append("=" * 96)
    lines.append(f"END · Wrote {out_json.name} + {out_txt.name}")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 72)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_txt}")


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)

    # —— Current week ——
    old_cw = cw.WIN_MULT
    cw.WIN_MULT = MULT
    try:
        cw_daily = [
            cw.simulate_day(d, lot_size=lot_size, cfg=cfg) for d in cw.available_days()
        ]
    finally:
        cw.WIN_MULT = old_cw
    _write_report(
        label="current-week",
        daily=cw_daily,
        out_json=ROOT / "data/backtests/final_dual_daily_report_cw_175.json",
        out_txt=ROOT / "data/backtests/final_dual_daily_report_cw_175.txt",
    )

    # —— Next week ——
    old_nw = nw.WIN_MULT
    nw.WIN_MULT = MULT
    try:
        nw_daily = [
            nw.simulate_day(d, lot_size=lot_size, cfg=cfg) for d in nw.available_days()
        ]
    finally:
        nw.WIN_MULT = old_nw
    _write_report(
        label="next-week",
        daily=nw_daily,
        out_json=ROOT / "data/backtests/final_dual_daily_report_nextweek_175.json",
        out_txt=ROOT / "data/backtests/final_dual_daily_report_nextweek_175.txt",
    )


if __name__ == "__main__":
    main()

"""Next-week detailed daily report: merge band 46–54, exit 1.70×."""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_nextweek_band_exit_grid as grid
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

BAND_LO, BAND_HI = 46.0, 54.0
WIN_MULT = 1.70
OUT_JSON = Path("data/backtests/nextweek_band46_54_exit170_daily.json")
OUT_TXT = Path("data/backtests/nextweek_band46_54_exit170_daily.txt")


def simulate_day(day: str, frames, meta, *, lot_size: int, cfg: dict) -> dict:
    expiry = str(meta.get("expiry") or "")
    setups = []
    used = set()
    hunt = nw.MERGE_AFTER
    sid = 0
    day_net = 0.0
    old = nw.WIN_MULT
    nw.WIN_MULT = WIN_MULT
    try:
        while True:
            before = None if sid == 0 else nw.LAST_ENTRY_2ND
            if sid >= 1:
                if day_net >= nw.SKIP_2ND_IF_NET_GE:
                    break
                hunt = max(hunt, nw.SECOND_AFTER)
            merge = grid.find_merge_band(
                frames,
                band_lo=BAND_LO,
                band_hi=BAND_HI,
                after_t=hunt,
                before_t=before,
                exclude_pairs=used,
            )
            if not merge:
                break
            sid += 1
            lots = nw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
            res = nw.run_one_setup(
                frames,
                merge,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=sid,
                expiry=expiry,
            )
            setups.append(res)
            day_net = round(day_net + float(res["net"]), 2)
            used.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used.add((int(merge["pe_k"]), int(merge["ce_k"])))
            if sid >= nw.MAX_SETUPS:
                break
            exit_m = nw._hm_to_min(str(res.get("exit_t") or merge["t"])) + nw.COOLDOWN_MIN
            hunt_m = max(exit_m, nw._hm_to_min(nw.SECOND_AFTER))
            h, m = divmod(hunt_m, 60)
            hunt = f"{h:02d}:{m:02d}"
            if hunt >= nw.SESSION_END or hunt >= nw.LAST_ENTRY_2ND:
                break
    finally:
        nw.WIN_MULT = old

    return {
        "day": day,
        "expiry": expiry,
        "current_expiry_scan": meta.get("current_expiry_scan"),
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "note": None if setups else f"no {BAND_LO:g}–{BAND_HI:g} dual merge on next-week",
        "skipped_2nd_paid_well": sid == 1 and day_net >= nw.SKIP_2ND_IF_NET_GE,
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = nw.available_days()
    daily = []
    for d in days:
        fr, meta = nw.load_nextweek_frames(d)
        daily.append(simulate_day(d, fr, meta, lot_size=lot_size, cfg=cfg))

    all_s = [s for day in daily for s in (day.get("setups") or [])]
    net = round(sum(float(d["net"]) for d in daily), 2)
    gross = round(sum(float(d["gross"]) for d in daily), 2)
    charges = round(sum(float(d["charges"]) for d in daily), 2)
    sw = sum(1 for s in all_s if float(s["net"]) > 0)
    sl = sum(1 for s in all_s if float(s["net"]) < 0)
    traded = [d for d in daily if d["setup_count"] > 0]
    dw = sum(1 for d in traded if float(d["net"]) > 0)
    dl = sum(1 for d in traded if float(d["net"]) < 0)

    report = {
        "strategy": "nextweek_band_46_54_exit_1.70x",
        "band": [BAND_LO, BAND_HI],
        "win_mult": WIN_MULT,
        "summary": {
            "net": net,
            "gross": gross,
            "charges": charges,
            "setup_wins": sw,
            "setup_losses": sl,
            "day_wins": dw,
            "day_losses": dl,
            "setups": len(all_s),
        },
        "daily": daily,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 96,
        f"NEXT-WEEK DETAILED — merge {BAND_LO:g}–{BAND_HI:g} · exit {WIN_MULT:g}×",
        f"TOTAL NET Rs {net:,.2f}  |  Gross {gross:,.2f}  |  Charges {charges:,.2f}",
        f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L · Capital ₹200,000 × 90%",
        "=" * 96,
    ]
    print("=" * 72)
    print(f"NEXT-WEEK 46–54 @ {WIN_MULT:g}×")
    print(f"NET ₹{net:,.2f}  GROSS ₹{gross:,.2f}  charges ₹{charges:,.2f}")
    print(f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L")
    print("=" * 72)

    for day in daily:
        lines.append("")
        lines.append(
            f"### {day['day']}  |  expiry={day.get('expiry')} (next)  |  "
            f"setups={day['setup_count']}  |  "
            f"NET Rs {float(day['net']):,.2f}  |  "
            f"Gross {float(day['gross']):,.2f}  |  Charges {float(day['charges']):,.2f}"
        )
        if day.get("note"):
            lines.append(f"    {day['note']}")
        if day.get("skipped_2nd_paid_well"):
            lines.append("    skipped 2nd — day already ≥ ₹15,000")
        print(
            f"\n{day['day']}  exp={day.get('expiry')}  setups={day['setup_count']}  "
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
                    f"      {leg['side']:2} K{leg['strike']}  exp {leg.get('expiry')}  "
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
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 72)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()

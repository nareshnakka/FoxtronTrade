"""
Walk-forward dual BT at ₹1L capital for both books (all available days).

  Algo1 / Current-week Convergence: 70–77 merge · exit 1.45× · max 2/day
  Algo2 / Next-week Convergence:    46–54 merge · exit 1.70× · max 1/day

Sizing: capital × 90% / ((CE+PE) × lot). Causal — no look-ahead.
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_final_daily_report as cw
import tools_bt_nextweek_band_exit_grid as grid
import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "backtests"
CAPITAL = 100_000.0
DEPLOY_PCT = 0.90

# Live profiles
CW_BAND = (70.0, 77.0)
CW_MULT = 1.45
CW_MAX = 2

NW_BAND = (46.0, 54.0)
NW_MULT = 1.70
NW_MAX = 1


def _write(
    *,
    label: str,
    strategy: str,
    daily: list[dict],
    band: tuple[float, float],
    win_mult: float,
    max_setups: int,
    out_json: Path,
    out_txt: Path,
) -> dict:
    traded = [d for d in daily if d.get("setup_count")]
    all_s = [s for d in daily for s in (d.get("setups") or [])]
    net = round(sum(float(d.get("net") or 0) for d in daily), 2)
    gross = round(sum(float(d.get("gross") or 0) for d in daily), 2)
    charges = round(sum(float(d.get("charges") or 0) for d in daily), 2)
    sw = sum(1 for s in all_s if float(s.get("net") or 0) > 0)
    sl = sum(1 for s in all_s if float(s.get("net") or 0) < 0)
    dw = sum(1 for d in traded if float(d.get("net") or 0) > 0)
    dl = sum(1 for d in traded if float(d.get("net") or 0) < 0)

    report = {
        "strategy": strategy,
        "capital": CAPITAL,
        "deploy_pct": DEPLOY_PCT,
        "band": list(band),
        "win_mult": win_mult,
        "max_setups": max_setups,
        "summary": {
            "net": net,
            "gross": gross,
            "charges": charges,
            "setup_wins": sw,
            "setup_losses": sl,
            "day_wins": dw,
            "day_losses": dl,
            "setups": len(all_s),
            "days": len(daily),
            "days_traded": len(traded),
        },
        "daily": daily,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 96,
        f"{label.upper()} — merge {band[0]:g}–{band[1]:g} · exit {win_mult:g}× · max {max_setups}/day",
        f"Capital ₹{CAPITAL:,.0f} × {DEPLOY_PCT:.0%} deploy",
        f"TOTAL NET Rs {net:,.2f}  |  Gross {gross:,.2f}  |  Charges {charges:,.2f}",
        f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L (traded {len(traded)}/{len(daily)})",
        "=" * 96,
    ]
    print("=" * 72)
    print(f"{label.upper()}  {band[0]:g}–{band[1]:g} @{win_mult:g}×  ₹{CAPITAL:,.0f}")
    print(f"NET ₹{net:,.2f}  GROSS ₹{gross:,.2f}  charges ₹{charges:,.2f}")
    print(f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L · traded {len(traded)}/{len(daily)}")
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
                f"    CE K{s['ce_strike']}  {s['entry_t']} @{s['ce_entry']}  "
                f"→ {s['exit_t']} @{s['ce_exit']} ({s['ce_mult']}×)"
            )
            lines.append(
                f"    PE K{s['pe_strike']}  {s['entry_t']} @{s['pe_entry']}  "
                f"→ {s['exit_t']} @{s['pe_exit']} ({s['pe_mult']}×)"
            )
            print(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
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

    lines.append("")
    lines.append("=" * 96)
    lines.append(f"END · {out_json.name} + {out_txt.name}")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 72)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_txt}")
    return report


def simulate_nw_day(day: str, frames, meta, *, lot_size: int, cfg: dict) -> dict:
    expiry = str(meta.get("expiry") or "")
    setups: list = []
    used: set = set()
    hunt = nw.MERGE_AFTER
    sid = 0
    day_net = 0.0
    old_m = nw.WIN_MULT
    nw.WIN_MULT = NW_MULT
    try:
        while True:
            if sid >= NW_MAX:
                break
            before = None if sid == 0 else nw.LAST_ENTRY_2ND
            merge = grid.find_merge_band(
                frames,
                band_lo=NW_BAND[0],
                band_hi=NW_BAND[1],
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
            if sid >= NW_MAX:
                break
            exit_m = nw._hm_to_min(str(res.get("exit_t") or merge["t"])) + nw.COOLDOWN_MIN
            hunt_m = max(exit_m, nw._hm_to_min(nw.SECOND_AFTER))
            h, m = divmod(hunt_m, 60)
            hunt = f"{h:02d}:{m:02d}"
            if hunt >= nw.SESSION_END or hunt >= nw.LAST_ENTRY_2ND:
                break
    finally:
        nw.WIN_MULT = old_m

    return {
        "day": day,
        "expiry": expiry,
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "note": None if setups else f"no {NW_BAND[0]:g}–{NW_BAND[1]:g} dual merge on next-week",
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)

    # Patch capital on both modules
    for mod in (cw, nw):
        mod.CAPITAL = CAPITAL
        mod.DEPLOY_PCT = DEPLOY_PCT

    # —— Algo1 / Current week ——
    old = (cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.WIN_MULT, cw.MAX_SETUPS)
    cw.BAND_LO, cw.BAND_HI = CW_BAND
    cw.BAND_CENTER = (CW_BAND[0] + CW_BAND[1]) / 2.0
    cw.WIN_MULT = CW_MULT
    cw.MAX_SETUPS = CW_MAX
    try:
        cw_days = cw.available_days()
        print(f"CW days: {len(cw_days)} → {cw_days[0] if cw_days else '—'} … {cw_days[-1] if cw_days else '—'}")
        cw_daily = [cw.simulate_day(d, lot_size=lot_size, cfg=cfg) for d in cw_days]
    finally:
        cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.WIN_MULT, cw.MAX_SETUPS = old

    cw_rep = _write(
        label="Algo1 Current-Week",
        strategy="algo1_cw_70_77_1.45x_1L",
        daily=cw_daily,
        band=CW_BAND,
        win_mult=CW_MULT,
        max_setups=CW_MAX,
        out_json=OUT_DIR / "bt_1L_algo1_cw_daily.json",
        out_txt=OUT_DIR / "bt_1L_algo1_cw_daily.txt",
    )

    # —— Algo2 / Next week ——
    nw_days = nw.available_days()
    print(f"\nNW days: {len(nw_days)} → {nw_days[0] if nw_days else '—'} … {nw_days[-1] if nw_days else '—'}")
    nw_daily = []
    for d in nw_days:
        fr, meta = nw.load_nextweek_frames(d)
        nw_daily.append(simulate_nw_day(d, fr, meta, lot_size=lot_size, cfg=cfg))

    nw_rep = _write(
        label="Algo2 Next-Week",
        strategy="algo2_nw_46_54_1.70x_1L_max1",
        daily=nw_daily,
        band=NW_BAND,
        win_mult=NW_MULT,
        max_setups=NW_MAX,
        out_json=OUT_DIR / "bt_1L_algo2_nw_daily.json",
        out_txt=OUT_DIR / "bt_1L_algo2_nw_daily.txt",
    )

    # Combined summary
    combined = {
        "capital": CAPITAL,
        "algo1_cw": cw_rep["summary"],
        "algo2_nw": nw_rep["summary"],
        "combined_net": round(
            float(cw_rep["summary"]["net"]) + float(nw_rep["summary"]["net"]), 2
        ),
    }
    (OUT_DIR / "bt_1L_combined_summary.json").write_text(
        json.dumps(combined, indent=2), encoding="utf-8"
    )
    print("\n" + "=" * 72)
    print(
        f"COMBINED NET ₹{combined['combined_net']:,.2f}  "
        f"(CW {cw_rep['summary']['net']:,.2f} + NW {nw_rep['summary']['net']:,.2f})"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()

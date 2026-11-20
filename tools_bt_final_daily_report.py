"""
Final walk-forward dual backtest — no look-ahead.

Live rules:
  • Band 70–77 merge (gap≤8, 2-bar confirm) after 09:20
  • Buy CE+PE sized capital×90% / ((CE+PE)×lot)
  • Exit BOTH when either leg ≥ 1.45× (pair close)
  • 2nd setup only after 11:00 and before 13:00; max 2/day
  • Skip 2nd if day net from #1 already ≥ ₹15,000
  • 1m cooldown after flat before next hunt

Causal: merge scan and exits only use frames at/after the decision time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foxtrade.charges import estimate_option_charges
from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parent
CHAIN = ROOT / "data" / "option_chain_1m"
OUT_JSON = ROOT / "data" / "backtests" / "final_dual_daily_report.json"
OUT_TXT = ROOT / "data" / "backtests" / "final_dual_daily_report.txt"

BAND_LO, BAND_HI = 70.0, 77.0
BAND_CENTER = (BAND_LO + BAND_HI) / 2.0
MERGE_GAP = 8.0
MERGE_AFTER = "09:20"
WIN_MULT = 1.45
SESSION_END = "15:25"
SECOND_AFTER = "11:00"
LAST_ENTRY_2ND = "13:00"
MAX_SETUPS = 2
COOLDOWN_MIN = 1
SKIP_2ND_IF_NET_GE = 15_000.0
CAPITAL = 200_000.0
DEPLOY_PCT = 0.90


def _hm_to_min(hm: str) -> int:
    try:
        h, m = hm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _leg_c(frame: dict, side: str, strike: int) -> float | None:
    row = (frame.get("legs") or {}).get(f"{side}:{strike}")
    if not row or row.get("c") is None:
        return None
    return float(row["c"])


def _strikes(frame: dict) -> list[int]:
    out: set[int] = set()
    for k in frame.get("legs") or {}:
        try:
            _, ks = k.split(":")
            out.add(int(ks))
        except Exception:
            pass
    return sorted(out)


def _in_band(px: float) -> bool:
    return BAND_LO - 2 <= px <= BAND_HI + 2


def available_days() -> list[str]:
    if not CHAIN.exists():
        return []
    days = []
    for p in sorted(CHAIN.iterdir()):
        if p.is_dir() and (p / "viewer.json").exists():
            days.append(p.name)
    return days


def find_merge_after(
    frames: list[dict],
    *,
    after_t: str,
    before_t: str | None,
    exclude_pairs: set[tuple[int, int]],
) -> dict[str, Any] | None:
    """Next merge using only frames with after_t ≤ t < before_t (causal)."""
    streak = 0
    best: dict[str, Any] | None = None
    after_m = _hm_to_min(after_t)
    before_m = _hm_to_min(before_t) if before_t else 10_000
    for fr in frames:
        t = fr.get("t") or ""
        if not t or t < MERGE_AFTER:
            continue
        tm = _hm_to_min(t)
        if tm < after_m:
            continue
        if tm >= before_m:
            break
        if t > SESSION_END:
            break
        atm = int(fr.get("atm") or 0)
        ces: list[tuple[int, float]] = []
        pes: list[tuple[int, float]] = []
        for k in _strikes(fr):
            ce = _leg_c(fr, "CE", k)
            pe = _leg_c(fr, "PE", k)
            if ce is not None and _in_band(ce):
                ces.append((k, ce))
            if pe is not None and _in_band(pe):
                pes.append((k, pe))
        if not ces or not pes:
            streak = 0
            best = None
            continue
        pair = None
        for ck, ce in sorted(ces, key=lambda x: abs(x[1] - BAND_CENTER)):
            for pk, pe in sorted(pes, key=lambda x: abs(x[1] - BAND_CENTER)):
                if (ck, pk) in exclude_pairs:
                    continue
                gap = abs(ce - pe)
                mid = (ce + pe) / 2
                if gap > MERGE_GAP or not (BAND_LO <= mid <= BAND_HI):
                    continue
                score = (gap, abs(mid - BAND_CENTER), abs(((ck + pk) / 2) - atm))
                if pair is None or score < pair[0]:
                    pair = (score, ck, pk, ce, pe, mid, gap, atm)
        if pair is None:
            streak = 0
            best = None
            continue
        streak += 1
        _s, ck, pk, ce, pe, mid, gap, atm = pair
        best = {
            "t": t,
            "ce_k": ck,
            "pe_k": pk,
            "ce0": ce,
            "pe0": pe,
            "mid": mid,
            "gap": gap,
            "atm": atm,
        }
        # 2-bar confirm — both bars already observed by this time
        if streak >= 2:
            return best
    return best if streak >= 1 else None


def lots_for_entry(ce0: float, pe0: float, lot_size: int) -> int:
    deploy = CAPITAL * DEPLOY_PCT
    pair_cost = (float(ce0) + float(pe0)) * lot_size
    if pair_cost <= 0:
        return 1
    return max(1, int(deploy // pair_cost))


def _close_leg(
    closed: list[dict],
    leg: dict,
    t: str,
    px: float,
    reason: str,
    *,
    lots: int,
    lot_size: int,
    cfg: dict,
    setup_id: int,
) -> None:
    qty = lots * lot_size
    pnl = (px - leg["entry"]) * qty
    try:
        ch = float(
            estimate_option_charges(leg["entry"], px, qty, cfg=cfg).get("charges") or 0
        )
    except Exception:
        ch = 0.0
    closed.append(
        {
            **leg,
            "setup_id": setup_id,
            "exit_t": t,
            "exit": round(float(px), 2),
            "reason": reason,
            "pnl": round(pnl, 2),
            "charges": round(ch, 2),
            "net": round(pnl - ch, 2),
            "mult": round(px / leg["entry"], 3) if leg["entry"] else None,
            "qty": qty,
        }
    )


def run_one_setup(
    frames: list[dict],
    merge: dict[str, Any],
    *,
    lots: int,
    lot_size: int,
    cfg: dict,
    setup_id: int,
) -> dict[str, Any]:
    """Enter at merge bar; walk forward only — book both at first 1.45× or EOD."""
    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    open_ce: dict | None = {
        "side": "CE",
        "entry_t": entry_t,
        "entry": ce0,
        "strike": ce_k,
        "lots": lots,
        "lot_size": lot_size,
    }
    open_pe: dict | None = {
        "side": "PE",
        "entry_t": entry_t,
        "entry": pe0,
        "strike": pe_k,
        "lots": lots,
        "lot_size": lot_size,
    }
    closed: list[dict] = []
    last_ce, last_pe = ce0, pe0
    exit_t = entry_t
    win_side = None
    exit_why = "EOD"

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t:
            continue
        if t > SESSION_END:
            break
        ce_now = _leg_c(fr, "CE", ce_k)
        pe_now = _leg_c(fr, "PE", pe_k)
        if ce_now is not None:
            last_ce = ce_now
        if pe_now is not None:
            last_pe = pe_now
        if ce_now is None and pe_now is None:
            continue
        ce, pe = last_ce, last_pe
        exit_t = t
        ce_hit = open_ce is not None and ce >= ce0 * WIN_MULT
        pe_hit = open_pe is not None and pe >= pe0 * WIN_MULT
        if not (ce_hit or pe_hit):
            continue
        if ce_hit and pe_hit:
            ce_hit = (ce / ce0) >= (pe / pe0)
            pe_hit = not ce_hit
        if ce_hit:
            win_side = "CE"
            exit_why = f"pair exit CE ≥{WIN_MULT:g}× ({ce:.2f}, {ce/ce0:.2f}×)"
            _close_leg(
                closed,
                open_ce,
                t,
                ce,
                exit_why,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=setup_id,
            )
            open_ce = None
            if open_pe is not None:
                _close_leg(
                    closed,
                    open_pe,
                    t,
                    pe,
                    f"pair close after CE ≥{WIN_MULT:g}× ({pe:.2f}, {pe/pe0:.2f}×)",
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    setup_id=setup_id,
                )
                open_pe = None
        else:
            win_side = "PE"
            exit_why = f"pair exit PE ≥{WIN_MULT:g}× ({pe:.2f}, {pe/pe0:.2f}×)"
            _close_leg(
                closed,
                open_pe,
                t,
                pe,
                exit_why,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=setup_id,
            )
            open_pe = None
            if open_ce is not None:
                _close_leg(
                    closed,
                    open_ce,
                    t,
                    ce,
                    f"pair close after PE ≥{WIN_MULT:g}× ({ce:.2f}, {ce/ce0:.2f}×)",
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    setup_id=setup_id,
                )
                open_ce = None
        break

    if open_ce is not None or open_pe is not None:
        last = None
        for fr in reversed(frames):
            t = fr.get("t") or ""
            if t > SESSION_END or t < entry_t:
                continue
            ce = _leg_c(fr, "CE", ce_k)
            pe = _leg_c(fr, "PE", pe_k)
            if ce is not None or pe is not None:
                last = (
                    t,
                    ce if ce is not None else last_ce,
                    pe if pe is not None else last_pe,
                )
                break
        if last:
            t, ce, pe = last
            exit_t = t
            exit_why = "EOD flat (never hit 1.45×)"
            if open_ce is not None:
                _close_leg(
                    closed,
                    open_ce,
                    t,
                    ce,
                    f"EOD flat CE {ce:.2f}",
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    setup_id=setup_id,
                )
            if open_pe is not None:
                _close_leg(
                    closed,
                    open_pe,
                    t,
                    pe,
                    f"EOD flat PE {pe:.2f}",
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    setup_id=setup_id,
                )

    gross = round(sum(float(x["pnl"]) for x in closed), 2)
    charges = round(sum(float(x["charges"]) for x in closed), 2)
    net = round(gross - charges, 2)
    ce_leg = next((x for x in closed if x["side"] == "CE"), None)
    pe_leg = next((x for x in closed if x["side"] == "PE"), None)
    return {
        "setup_id": setup_id,
        "entry_t": entry_t,
        "exit_t": exit_t,
        "exit_why": exit_why,
        "ce_strike": ce_k,
        "pe_strike": pe_k,
        "atm": merge.get("atm"),
        "mid": round(float(merge["mid"]), 2),
        "gap": round(float(merge["gap"]), 2),
        "ce_entry": ce0,
        "pe_entry": pe0,
        "ce_exit": ce_leg["exit"] if ce_leg else None,
        "pe_exit": pe_leg["exit"] if pe_leg else None,
        "ce_mult": ce_leg.get("mult") if ce_leg else None,
        "pe_mult": pe_leg.get("mult") if pe_leg else None,
        "win_side": win_side if win_side else "EOD",
        "lots": lots,
        "lot_size": lot_size,
        "qty_per_leg": lots * lot_size,
        "deploy_used": round(lots * (ce0 + pe0) * lot_size, 2),
        "gross": gross,
        "charges": charges,
        "net": net,
        "legs": closed,
    }


def simulate_day(day: str, *, lot_size: int, cfg: dict) -> dict[str, Any]:
    viewer_path = CHAIN / day / "viewer.json"
    viewer = json.loads(viewer_path.read_text(encoding="utf-8"))
    frames = list(viewer.get("frames") or [])
    if not frames:
        return {
            "day": day,
            "setups": [],
            "setup_count": 0,
            "net": 0,
            "gross": 0,
            "charges": 0,
            "note": "no frames",
        }

    setups: list[dict] = []
    used_pairs: set[tuple[int, int]] = set()
    hunt_after = MERGE_AFTER
    setup_id = 0
    day_net = 0.0

    while True:
        before = None if setup_id == 0 else LAST_ENTRY_2ND
        if setup_id >= 1:
            if day_net >= SKIP_2ND_IF_NET_GE:
                break
            hunt_after = max(hunt_after, SECOND_AFTER)
        merge = find_merge_after(
            frames, after_t=hunt_after, before_t=before, exclude_pairs=used_pairs
        )
        if not merge:
            break
        setup_id += 1
        lots = lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
        res = run_one_setup(
            frames, merge, lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id
        )
        setups.append(res)
        day_net = round(day_net + float(res["net"]), 2)
        used_pairs.add((int(merge["ce_k"]), int(merge["pe_k"])))
        used_pairs.add((int(merge["pe_k"]), int(merge["ce_k"])))
        if setup_id >= MAX_SETUPS:
            break
        exit_m = _hm_to_min(str(res.get("exit_t") or merge["t"])) + COOLDOWN_MIN
        hunt_m = max(exit_m, _hm_to_min(SECOND_AFTER))
        h, m = divmod(hunt_m, 60)
        hunt_after = f"{h:02d}:{m:02d}"
        if hunt_after >= SESSION_END or hunt_after >= LAST_ENTRY_2ND:
            break

    return {
        "day": day,
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "note": None if setups else "no 70-77 dual merge",
        "skipped_2nd_paid_well": setup_id == 1 and day_net >= SKIP_2ND_IF_NET_GE,
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = available_days()
    daily: list[dict] = []

    for day in days:
        daily.append(simulate_day(day, lot_size=lot_size, cfg=cfg))

    traded = [d for d in daily if d.get("setups")]
    all_setups = []
    for d in daily:
        for s in d.get("setups") or []:
            all_setups.append({**s, "day": d["day"]})

    net = round(sum(float(d.get("net") or 0) for d in daily), 2)
    gross = round(sum(float(d.get("gross") or 0) for d in daily), 2)
    charges = round(sum(float(d.get("charges") or 0) for d in daily), 2)
    sw = sum(1 for s in all_setups if float(s.get("net") or 0) > 0)
    sl = sum(1 for s in all_setups if float(s.get("net") or 0) < 0)
    dw = sum(1 for d in traded if float(d.get("net") or 0) > 0)
    dl = sum(1 for d in traded if float(d.get("net") or 0) < 0)

    report = {
        "strategy": "final_dual_70_77_1.45x_2nd_after_11_walkforward",
        "rules": [
            "70–77 merge, gap≤8, 2-bar, after 09:20",
            "buy both @ merge LTP; size capital×90%",
            f"exit both when either ≥{WIN_MULT:g}× (walk-forward, no peek)",
            f"2nd only ≥{SECOND_AFTER} and <{LAST_ENTRY_2ND}; max {MAX_SETUPS}/day",
            f"skip 2nd if day net ≥ ₹{SKIP_2ND_IF_NET_GE:,.0f}",
        ],
        "capital": CAPITAL,
        "deploy_pct": DEPLOY_PCT,
        "summary": {
            "net": net,
            "gross": gross,
            "charges": charges,
            "days": len(days),
            "days_traded": len(traded),
            "setups": len(all_setups),
            "setup_wins": sw,
            "setup_losses": sl,
            "day_wins": dw,
            "day_losses": dl,
        },
        "daily": daily,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("=" * 96)
    lines.append("FINAL DAILY DETAILED REPORT — Dual 70–77 @ 1.45× (walk-forward, no look-ahead)")
    lines.append(
        f"TOTAL NET Rs {net:,.2f}  |  Gross {gross:,.2f}  |  Charges {charges:,.2f}"
    )
    lines.append(
        f"Setups {sw}W/{sl}L · Days traded {dw}W/{dl}L · Capital ₹{CAPITAL:,.0f} × {DEPLOY_PCT:.0%}"
    )
    lines.append(
        f"Rules: 1st ≥{MERGE_AFTER} · 2nd [{SECOND_AFTER},{LAST_ENTRY_2ND}) · "
        f"skip 2nd if day≥₹{SKIP_2ND_IF_NET_GE:,.0f} · exit both @{WIN_MULT:g}×"
    )
    lines.append("=" * 96)

    print("=" * 72)
    print("FINAL dual walk-forward · book @ 1.45×")
    print(f"NET ₹{net:,.2f}  GROSS ₹{gross:,.2f}  charges ₹{charges:,.2f}")
    print(f"Setups {sw}W/{sl}L · Days {dw}W/{dl}L")
    print("=" * 72)

    for day in daily:
        lines.append("")
        lines.append(
            f"### {day['day']}  |  setups={day['setup_count']}  |  "
            f"NET Rs {float(day['net']):,.2f}  |  "
            f"Gross {float(day['gross']):,.2f}  |  Charges {float(day['charges']):,.2f}"
        )
        if day.get("note"):
            lines.append(f"    {day['note']}")
        if day.get("skipped_2nd_paid_well"):
            lines.append(f"    skipped 2nd — day already ≥ ₹{SKIP_2ND_IF_NET_GE:,.0f}")
        print(
            f"\n{day['day']}  setups={day['setup_count']}  "
            f"NET ₹{float(day['net']):,.2f}  {day.get('note') or ''}"
        )
        for s in day.get("setups") or []:
            lines.append(
                f"  Setup #{s['setup_id']}  {s['entry_t']} → {s['exit_t']}  "
                f"ATM {s['atm']}  mid {s['mid']}  gap {s['gap']}  "
                f"win={s['win_side']}  lots={s['lots']} (qty {s['qty_per_leg']}/leg)  "
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
    lines.append(f"END · Wrote {OUT_JSON.name} + {OUT_TXT.name}")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 72)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()

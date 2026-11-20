"""
Next-week dual @ 1.90× winner book, then hold FALLEN for 5–50% bounce from low.

Walk-forward, no look-ahead:
  • Same 70–77 merge / 2nd-entry rules as next-week BT
  • When either leg ≥ 1.90× → close WINNER immediately
  • Keep FALLEN; track its low; sell when bounce from low is 5–50%
    (exit at ≥5%, force by 50%); else EOD
  • Also report baseline: square BOTH at 1.90× (no fallen hold)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

WIN_MULT = 1.90
BOUNCE_MIN_PCT = 5.0
BOUNCE_MAX_PCT = 50.0

OUT_JSON = Path("data/backtests/nextweek_190_fallen_retrace_5_50.json")
OUT_TXT = Path("data/backtests/nextweek_190_fallen_retrace_5_50.txt")


def _close(
    closed: list,
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
    nw._close_leg(
        closed, leg, t, px, reason,
        lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
    )


def run_setup_fallen_retrace(
    frames: list[dict],
    merge: dict,
    *,
    lots: int,
    lot_size: int,
    cfg: dict,
    setup_id: int,
    expiry: str,
) -> dict[str, Any]:
    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    open_ce: dict | None = {
        "side": "CE", "entry_t": entry_t, "entry": ce0,
        "strike": ce_k, "lots": lots, "lot_size": lot_size, "expiry": expiry,
    }
    open_pe: dict | None = {
        "side": "PE", "entry_t": entry_t, "entry": pe0,
        "strike": pe_k, "lots": lots, "lot_size": lot_size, "expiry": expiry,
    }
    closed: list[dict] = []
    last_ce, last_pe = ce0, pe0
    exit_t = entry_t
    win_side: str | None = None
    win_exit_t: str | None = None
    fallen_side: str | None = None
    fallen_low: float | None = None
    phase = "DUAL"  # DUAL → FALLEN_HOLD → DONE
    exit_why = "EOD"

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t:
            continue
        if t > nw.SESSION_END:
            break
        ce_now = nw._leg_c(fr, "CE", ce_k)
        pe_now = nw._leg_c(fr, "PE", pe_k)
        if ce_now is not None:
            last_ce = ce_now
        if pe_now is not None:
            last_pe = pe_now
        if ce_now is None and pe_now is None:
            continue
        ce, pe = last_ce, last_pe
        exit_t = t

        if phase == "DUAL":
            ce_hit = open_ce is not None and ce >= ce0 * WIN_MULT
            pe_hit = open_pe is not None and pe >= pe0 * WIN_MULT
            if not (ce_hit or pe_hit):
                continue
            if ce_hit and pe_hit:
                win_side = "CE" if (ce / ce0) >= (pe / pe0) else "PE"
            elif ce_hit:
                win_side = "CE"
            else:
                win_side = "PE"

            if win_side == "CE":
                _close(
                    closed, open_ce, t, ce,
                    f"winner CE ≥{WIN_MULT:g}× ({ce:.2f}, {ce/ce0:.2f}×)",
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
                open_ce = None
                fallen_side = "PE"
                fallen_low = pe
                win_exit_t = t
                # start fallen hold
                phase = "FALLEN_HOLD"
            else:
                _close(
                    closed, open_pe, t, pe,
                    f"winner PE ≥{WIN_MULT:g}× ({pe:.2f}, {pe/pe0:.2f}×)",
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
                open_pe = None
                fallen_side = "CE"
                fallen_low = ce
                win_exit_t = t
                phase = "FALLEN_HOLD"
            # fall through same bar to check bounce

        if phase == "FALLEN_HOLD":
            f_px = pe if fallen_side == "PE" else ce
            f_open = open_pe if fallen_side == "PE" else open_ce
            if f_open is None:
                phase = "DONE"
                break
            if fallen_low is None or f_px < fallen_low:
                fallen_low = f_px
            bounce_pct = (
                (f_px - float(fallen_low)) / float(fallen_low) * 100.0
                if fallen_low and fallen_low > 1e-9
                else 0.0
            )
            if bounce_pct >= BOUNCE_MAX_PCT - 1e-9:
                exit_why = (
                    f"fallen {fallen_side} bounce {bounce_pct:.1f}%≥max "
                    f"{BOUNCE_MAX_PCT:g}% from low {fallen_low:.1f} "
                    f"(winner booked {win_exit_t})"
                )
                _close(
                    closed, f_open, t, f_px, exit_why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
                if fallen_side == "PE":
                    open_pe = None
                else:
                    open_ce = None
                phase = "DONE"
                break
            if bounce_pct >= BOUNCE_MIN_PCT - 1e-9:
                exit_why = (
                    f"fallen {fallen_side} bounce {bounce_pct:.1f}% "
                    f"({BOUNCE_MIN_PCT:g}–{BOUNCE_MAX_PCT:g}%) from low "
                    f"{fallen_low:.1f} (winner booked {win_exit_t})"
                )
                _close(
                    closed, f_open, t, f_px, exit_why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
                if fallen_side == "PE":
                    open_pe = None
                else:
                    open_ce = None
                phase = "DONE"
                break

    # EOD flatten anything still open
    if open_ce is not None or open_pe is not None:
        last = None
        for fr in reversed(frames):
            t = fr.get("t") or ""
            if t > nw.SESSION_END or t < entry_t:
                continue
            ce = nw._leg_c(fr, "CE", ce_k)
            pe = nw._leg_c(fr, "PE", pe_k)
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
            if open_ce is not None:
                why = (
                    f"EOD fallen CE {ce:.2f} (winner {win_exit_t})"
                    if win_side == "PE"
                    else f"EOD flat CE {ce:.2f}"
                )
                _close(
                    closed, open_ce, t, ce, why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
            if open_pe is not None:
                why = (
                    f"EOD fallen PE {pe:.2f} (winner {win_exit_t})"
                    if win_side == "CE"
                    else f"EOD flat PE {pe:.2f}"
                )
                _close(
                    closed, open_pe, t, pe, why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
            if phase == "FALLEN_HOLD":
                exit_why = f"EOD fallen hold (winner {win_exit_t}, no 5–50% bounce)"
            elif phase == "DUAL":
                exit_why = f"EOD flat (never hit {WIN_MULT:g}×)"

    gross = round(sum(float(x["pnl"]) for x in closed), 2)
    charges = round(sum(float(x["charges"]) for x in closed), 2)
    net = round(gross - charges, 2)
    ce_leg = next((x for x in closed if x["side"] == "CE"), None)
    pe_leg = next((x for x in closed if x["side"] == "PE"), None)
    return {
        "setup_id": setup_id,
        "expiry": expiry,
        "entry_t": entry_t,
        "exit_t": exit_t,
        "win_exit_t": win_exit_t,
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
        "ce_exit_t": ce_leg["exit_t"] if ce_leg else None,
        "pe_exit_t": pe_leg["exit_t"] if pe_leg else None,
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


def simulate_day(day: str, *, lot_size: int, cfg: dict, mode: str) -> dict[str, Any]:
    frames, meta = nw.load_nextweek_frames(day)
    expiry = str(meta.get("expiry") or "")
    if not frames:
        return {
            "day": day, "expiry": expiry, "setups": [], "setup_count": 0,
            "net": 0, "gross": 0, "charges": 0, "note": "no frames",
        }

    setups: list[dict] = []
    used: set[tuple[int, int]] = set()
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
            merge = nw.find_merge_after(
                frames, after_t=hunt, before_t=before, exclude_pairs=used
            )
            if not merge:
                break
            sid += 1
            lots = nw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
            if mode == "fallen_retrace":
                res = run_setup_fallen_retrace(
                    frames, merge, lots=lots, lot_size=lot_size,
                    cfg=cfg, setup_id=sid, expiry=expiry,
                )
            else:
                res = nw.run_one_setup(
                    frames, merge, lots=lots, lot_size=lot_size,
                    cfg=cfg, setup_id=sid, expiry=expiry,
                )
            setups.append(res)
            day_net = round(day_net + float(res["net"]), 2)
            used.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used.add((int(merge["pe_k"]), int(merge["ce_k"])))
            if sid >= nw.MAX_SETUPS:
                break
            # cooldown from last flat (fallen may exit later)
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
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "note": None if setups else "no 70-77 dual merge on next-week",
        "skipped_2nd_paid_well": sid == 1 and day_net >= nw.SKIP_2ND_IF_NET_GE,
    }


def _summarize(daily: list[dict]) -> dict:
    all_s = [s for d in daily for s in (d.get("setups") or [])]
    return {
        "net": round(sum(float(d.get("net") or 0) for d in daily), 2),
        "gross": round(sum(float(d.get("gross") or 0) for d in daily), 2),
        "charges": round(sum(float(d.get("charges") or 0) for d in daily), 2),
        "setup_wins": sum(1 for s in all_s if float(s.get("net") or 0) > 0),
        "setup_losses": sum(1 for s in all_s if float(s.get("net") or 0) < 0),
        "setups": len(all_s),
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = nw.available_days()

    baseline = [
        simulate_day(d, lot_size=lot_size, cfg=cfg, mode="both_at_190")
        for d in days
    ]
    fallen = [
        simulate_day(d, lot_size=lot_size, cfg=cfg, mode="fallen_retrace")
        for d in days
    ]
    sb = _summarize(baseline)
    sf = _summarize(fallen)

    report = {
        "strategy": "nextweek_1.90_winner_fallen_bounce_5_50",
        "win_mult": WIN_MULT,
        "fallen_bounce_pct": [BOUNCE_MIN_PCT, BOUNCE_MAX_PCT],
        "baseline_both_at_190": {"summary": sb, "daily": baseline},
        "fallen_retrace": {"summary": sf, "daily": fallen},
        "delta_net": round(sf["net"] - sb["net"], 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 96,
        f"NEXT-WEEK @ {WIN_MULT:g}× winner + FALLEN bounce {BOUNCE_MIN_PCT:g}–{BOUNCE_MAX_PCT:g}% from low",
        f"FALLEN-HOLD NET ₹{sf['net']:,.2f}  |  baseline both@{WIN_MULT:g}× ₹{sb['net']:,.2f}  |  "
        f"delta ₹{sf['net'] - sb['net']:+,.2f}",
        "=" * 96,
    ]
    print("=" * 72)
    print(f"NEXT-WEEK 1.90× + fallen 5–50% bounce")
    print(f"Fallen-hold NET ₹{sf['net']:,.2f}")
    print(f"Baseline both@1.90× NET ₹{sb['net']:,.2f}")
    print(f"Delta ₹{sf['net'] - sb['net']:+,.2f}")
    print("=" * 72)

    for day in fallen:
        lines.append("")
        base = next(d for d in baseline if d["day"] == day["day"])
        lines.append(
            f"### {day['day']}  exp={day.get('expiry')}  "
            f"setups={day['setup_count']}  "
            f"FALLEN-HOLD NET ₹{float(day['net']):,.2f}  "
            f"(baseline ₹{float(base['net']):,.2f})"
        )
        if day.get("note"):
            lines.append(f"    {day['note']}")
        print(
            f"\n{day['day']}  setups={day['setup_count']}  "
            f"fallen-hold ₹{float(day['net']):,.2f}  "
            f"baseline ₹{float(base['net']):,.2f}  {day.get('note') or ''}"
        )
        for s in day.get("setups") or []:
            lines.append(
                f"  #{s['setup_id']} entry {s['entry_t']}  "
                f"win_out {s.get('win_exit_t')}  flat {s['exit_t']}  "
                f"win={s['win_side']} lots={s['lots']}  "
                f"NET ₹{s['net']:,.2f}"
            )
            lines.append(f"    exit: {s.get('exit_why')}")
            lines.append(
                f"    CE K{s['ce_strike']}  {s.get('ce_exit_t') or s['entry_t']} "
                f"{s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)"
            )
            lines.append(
                f"    PE K{s['pe_strike']}  {s.get('pe_exit_t') or s['entry_t']} "
                f"{s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)"
            )
            print(
                f"  #{s['setup_id']} in {s['entry_t']} win@{s.get('win_exit_t')} "
                f"flat@{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"NET ₹{s['net']:,.2f}"
            )
            print(f"      {s.get('exit_why')}")
            for leg in s.get("legs") or []:
                lines.append(
                    f"      {leg['side']} K{leg['strike']}  "
                    f"{leg['entry_t']}@{leg['entry']} → {leg['exit_t']}@{leg['exit']} "
                    f"({leg['mult']}×) qty={leg['qty']}  "
                    f"PnL {leg['pnl']:,.2f} Chg {leg['charges']:,.2f} "
                    f"Net {leg['net']:,.2f}  | {leg['reason']}"
                )
                print(
                    f"      {leg['side']} K{leg['strike']}  "
                    f"{leg['entry_t']}@{leg['entry']} → {leg['exit_t']}@{leg['exit']} "
                    f"({leg['mult']}×)  Net {leg['net']:,.2f}"
                )

    lines.append("")
    lines.append("=" * 96)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-" * 72)
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()

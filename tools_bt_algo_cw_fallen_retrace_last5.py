"""
Algo-CW CW — book breakout winner @ 1.45×, hold fallen for 10–50% bounce from low.
No second entry. Causal last-5 days (no look-ahead).

Also reports baseline: square BOTH at 1.45×, max 1/day.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
WIN_MULT = 1.45
BOUNCE_MIN_PCT = 10.0
BOUNCE_MAX_PCT = 50.0
MAX_SETUPS = 1
OUT = Path(__file__).resolve().parent / "data" / "backtests"


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
    cw._close_leg(
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
) -> dict[str, Any]:
    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    open_ce: dict | None = {
        "side": "CE", "entry_t": entry_t, "entry": ce0,
        "strike": ce_k, "lots": lots, "lot_size": lot_size,
    }
    open_pe: dict | None = {
        "side": "PE", "entry_t": entry_t, "entry": pe0,
        "strike": pe_k, "lots": lots, "lot_size": lot_size,
    }
    closed: list[dict] = []
    last_ce, last_pe = ce0, pe0
    exit_t = entry_t
    win_side: str | None = None
    win_exit_t: str | None = None
    fallen_side: str | None = None
    fallen_low: float | None = None
    phase = "DUAL"
    exit_why = "EOD"

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t:
            continue
        if t > cw.SESSION_END:
            break
        ce_now = cw._leg_c(fr, "CE", ce_k)
        pe_now = cw._leg_c(fr, "PE", pe_k)
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
                    f"(winner @{win_exit_t})"
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
                    f"{fallen_low:.1f} (winner @{win_exit_t})"
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

    if open_ce is not None or open_pe is not None:
        last = None
        for fr in reversed(frames):
            t = fr.get("t") or ""
            if t > cw.SESSION_END or t < entry_t:
                continue
            ce = cw._leg_c(fr, "CE", ce_k)
            pe = cw._leg_c(fr, "PE", pe_k)
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
                    f"EOD fallen CE {ce:.2f} (winner @{win_exit_t})"
                    if win_side == "PE"
                    else f"EOD flat CE {ce:.2f}"
                )
                _close(
                    closed, open_ce, t, ce, why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
            if open_pe is not None:
                why = (
                    f"EOD fallen PE {pe:.2f} (winner @{win_exit_t})"
                    if win_side == "CE"
                    else f"EOD flat PE {pe:.2f}"
                )
                _close(
                    closed, open_pe, t, pe, why,
                    lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id,
                )
            if phase == "FALLEN_HOLD":
                exit_why = (
                    f"EOD fallen hold (winner @{win_exit_t}, "
                    f"no {BOUNCE_MIN_PCT:g}–{BOUNCE_MAX_PCT:g}% bounce)"
                )
            elif phase == "DUAL":
                exit_why = f"EOD flat (never hit {WIN_MULT:g}×)"

    gross = round(sum(float(x["pnl"]) for x in closed), 2)
    charges = round(sum(float(x["charges"]) for x in closed), 2)
    net = round(gross - charges, 2)
    ce_leg = next((x for x in closed if x["side"] == "CE"), None)
    pe_leg = next((x for x in closed if x["side"] == "PE"), None)
    return {
        "setup_id": setup_id,
        "entry_t": entry_t,
        "exit_t": exit_t,
        "win_exit_t": win_exit_t,
        "exit_why": exit_why,
        "ce_strike": ce_k,
        "pe_strike": pe_k,
        "ce_entry": ce0,
        "pe_entry": pe0,
        "ce_exit": ce_leg["exit"] if ce_leg else None,
        "pe_exit": pe_leg["exit"] if pe_leg else None,
        "ce_mult": ce_leg.get("mult") if ce_leg else None,
        "pe_mult": pe_leg.get("mult") if pe_leg else None,
        "win_side": win_side if win_side else "EOD",
        "lots": lots,
        "gross": gross,
        "charges": charges,
        "net": net,
        "legs": closed,
    }


def simulate_day(day: str, frames: list[dict], *, lot_size: int, cfg: dict, mode: str) -> dict:
    setups: list[dict] = []
    used: set[tuple[int, int]] = set()
    hunt = cw.MERGE_AFTER
    sid = 0
    old_max = cw.MAX_SETUPS
    cw.MAX_SETUPS = MAX_SETUPS
    try:
        while sid < MAX_SETUPS:
            merge = cw.find_merge_after(
                frames, after_t=hunt, before_t=None, exclude_pairs=used
            )
            if not merge:
                break
            sid += 1
            lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
            if mode == "fallen_retrace":
                res = run_setup_fallen_retrace(
                    frames, merge, lots=lots, lot_size=lot_size,
                    cfg=cfg, setup_id=sid,
                )
            else:
                res = cw.run_one_setup(
                    frames, merge, lots=lots, lot_size=lot_size,
                    cfg=cfg, setup_id=sid,
                )
            setups.append(res)
            used.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used.add((int(merge["pe_k"]), int(merge["ce_k"])))
            break  # hard: no second entry
    finally:
        cw.MAX_SETUPS = old_max

    return {
        "day": day,
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
    }


def _score(daily: list[dict]) -> dict:
    nets = [float(d["net"]) for d in daily]
    return {
        "net": round(sum(nets), 2),
        "green_days": sum(1 for n in nets if n > 0),
        "red_days": sum(1 for n in nets if n < 0),
        "worst_day": round(min(nets), 2) if nets else 0,
        "best_day": round(max(nets), 2) if nets else 0,
        "avg_day": round(sum(nets) / len(nets), 2) if nets else 0,
    }


def _print_block(title: str, daily: list[dict], sc: dict) -> list[str]:
    lines = [
        "=" * 88,
        title,
        (
            f"TOTAL NET ₹{sc['net']:,.2f}  |  {sc['green_days']}G / {sc['red_days']}R  |  "
            f"worst ₹{sc['worst_day']:,.2f}  |  best ₹{sc['best_day']:,.2f}  |  avg ₹{sc['avg_day']:,.2f}"
        ),
        "=" * 88,
    ]
    print("\n".join(lines))
    for day in daily:
        print(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        lines.append(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            row = (
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s.get('ce_exit')} ({s.get('ce_mult')}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s.get('pe_exit')} ({s.get('pe_mult')}×)  "
                f"lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )
            why = f"    {s.get('exit_why')}"
            if s.get("win_exit_t"):
                why += f"  win_booked={s['win_exit_t']} side={s.get('win_side')}"
            print(row)
            print(why)
            lines.extend([row, why])
    return lines


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    # Match live Algo-CW / prior last5 reports: ₹1L × 90%
    old_cap = cw.CAPITAL
    cw.CAPITAL = float(cfg.get("algo_cw_capital_inr") or 100_000)
    frames: dict[str, list] = {}
    for d in LAST5:
        path = cw.CHAIN / d / "viewer.json"
        if not path.exists():
            continue
        frames[d] = list(json.loads(path.read_text(encoding="utf-8")).get("frames") or [])
    days = [d for d in LAST5 if d in frames]

    try:
        fallen_daily = [
            simulate_day(d, frames[d], lot_size=lot, cfg=cfg, mode="fallen_retrace")
            for d in days
        ]
        base_daily = [
            simulate_day(d, frames[d], lot_size=lot, cfg=cfg, mode="hit_both")
            for d in days
        ]
    finally:
        cw.CAPITAL = old_cap

    sc_f = _score(fallen_daily)
    sc_b = _score(base_daily)

    header = [
        "ALGO2 — LAST 5 CAUSAL",
        f"Book winner @ {WIN_MULT:g}× breakout → hold fallen {BOUNCE_MIN_PCT:g}–{BOUNCE_MAX_PCT:g}% bounce from low",
        f"Max {MAX_SETUPS} setup/day (NO second entry) · 70–77 · ₹{float(cfg.get('algo_cw_capital_inr') or 100_000):,.0f} × 90% · lot={lot}",
        "",
    ]
    print("\n".join(header))

    lines = header[:]
    lines.extend(
        _print_block(
            f"FALLEN RETRACE 10–50% (winner book @ {WIN_MULT:g}×, no 2nd)",
            fallen_daily,
            sc_f,
        )
    )
    print()
    lines.append("")
    lines.extend(
        _print_block(
            f"BASELINE square-both @ {WIN_MULT:g}× (no 2nd) — for compare",
            base_daily,
            sc_b,
        )
    )

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo_cw_fallen_retrace_last5.txt"
    js = OUT / "bt_algo_cw_fallen_retrace_last5.json"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "strategy": "algo_cw_145_winner_fallen_bounce_10_50_max1",
                "days": days,
                "causal": True,
                "fallen_retrace": {"summary": sc_f, "daily": fallen_daily},
                "baseline_hit_both_max1": {"summary": sc_b, "daily": base_daily},
                "rules": {
                    "win_mult": WIN_MULT,
                    "bounce_min_pct": BOUNCE_MIN_PCT,
                    "bounce_max_pct": BOUNCE_MAX_PCT,
                    "max_setups": MAX_SETUPS,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")
    print(f"Wrote {js}")


if __name__ == "__main__":
    main()

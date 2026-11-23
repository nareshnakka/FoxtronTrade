"""
Algo-CW pre-wireup backtest — last 5 causal.

A) hit 1.45× · max2 · skip 2nd if day ≥ ₹3,000
B) book winner@1.45× + fallen bounce@12% · max1
C) B then allow 2nd after flat · skip 2nd if day ≥ ₹3,000
vs LIVE-ish: hit 1.45× · max2 · skip@₹15,000
"""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_algo1_exit_learn as L
import tools_bt_algo_cw_fallen_retrace_last5 as F
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
OUT = Path(__file__).resolve().parent / "data" / "backtests"
WIN = 1.45
FALLEN_PCT = 12.0
SKIP_A = 3_000.0
SKIP_LIVE = 15_000.0


def _score(daily: list[dict]) -> dict:
    nets = [float(d["net"]) for d in daily]
    return {
        "net": round(sum(nets), 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "worst": round(min(nets), 2) if nets else 0,
        "best": round(max(nets), 2) if nets else 0,
        "avg": round(sum(nets) / len(nets), 2) if nets else 0,
    }


def _run_hit(days, frames, *, lot, cfg, max_setups: int, skip_ge: float) -> tuple[list, dict]:
    pol = L.Policy(
        "hit",
        WIN,
        "hit",
        max_setups=max_setups,
        skip_2nd_if_net_ge=skip_ge,
    )
    daily, _ = L.run_universe(days, frames, policy=pol, lot_size=lot, cfg=cfg)
    return daily, _score(daily)


def _fallen_setup(frames, merge, *, lot, cfg, sid: int) -> dict:
    old = (F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT)
    F.BOUNCE_MIN_PCT = F.BOUNCE_MAX_PCT = FALLEN_PCT
    F.WIN_MULT = WIN
    try:
        lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot)
        return F.run_setup_fallen_retrace(
            frames, merge, lots=lots, lot_size=lot, cfg=cfg, setup_id=sid
        )
    finally:
        F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT = old


def _hm(hm: str) -> int:
    try:
        h, m = hm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _run_fallen_max1(days, frames, *, lot, cfg) -> tuple[list, dict]:
    old_cap = cw.CAPITAL
    cw.CAPITAL = float(cfg.get("algo_cw_capital_inr") or 100_000)
    daily = []
    try:
        for d in days:
            merge = cw.find_merge_after(
                frames[d], after_t=cw.MERGE_AFTER, before_t=None, exclude_pairs=set()
            )
            if not merge:
                daily.append({"day": d, "setups": [], "setup_count": 0, "net": 0.0, "gross": 0.0, "charges": 0.0})
                continue
            res = _fallen_setup(frames[d], merge, lot=lot, cfg=cfg, sid=1)
            daily.append(
                {
                    "day": d,
                    "setups": [res],
                    "setup_count": 1,
                    "gross": float(res["gross"]),
                    "charges": float(res["charges"]),
                    "net": float(res["net"]),
                }
            )
    finally:
        cw.CAPITAL = old_cap
    return daily, _score(daily)


def _run_fallen_then_2nd(days, frames, *, lot, cfg, skip_ge: float) -> tuple[list, dict]:
    """C: first setup = winner book + fallen@12%; 2nd = square-both hit if day net < skip_ge."""
    old_cap = cw.CAPITAL
    cw.CAPITAL = float(cfg.get("algo_cw_capital_inr") or 100_000)
    daily = []
    try:
        for d in days:
            fr = frames[d]
            used: set = set()
            setups = []
            day_net = 0.0
            # setup 1 — fallen path
            merge1 = cw.find_merge_after(
                fr, after_t=cw.MERGE_AFTER, before_t=None, exclude_pairs=used
            )
            if merge1:
                s1 = _fallen_setup(fr, merge1, lot=lot, cfg=cfg, sid=1)
                setups.append(s1)
                day_net = float(s1["net"])
                used.add((int(merge1["ce_k"]), int(merge1["pe_k"])))
                used.add((int(merge1["pe_k"]), int(merge1["ce_k"])))
                # setup 2?
                if day_net < skip_ge:
                    exit_m = _hm(str(s1.get("exit_t") or merge1["t"])) + 1
                    hunt_m = max(exit_m, _hm("11:00"))
                    h, m = divmod(hunt_m, 60)
                    hunt = f"{h:02d}:{m:02d}"
                    if hunt < "13:00" and hunt < cw.SESSION_END:
                        merge2 = cw.find_merge_after(
                            fr, after_t=hunt, before_t="13:00", exclude_pairs=used
                        )
                        if merge2:
                            lots = cw.lots_for_entry(merge2["ce0"], merge2["pe0"], lot)
                            # square-both hit for 2nd
                            pol = L.Policy("hit2", WIN, "hit", max_setups=1, skip_2nd_if_net_ge=1e18)
                            s2 = L.run_setup(
                                fr, merge2, policy=pol, lots=lots, lot_size=lot, cfg=cfg, setup_id=2
                            )
                            setups.append(s2)
                            day_net = round(day_net + float(s2["net"]), 2)
            daily.append(
                {
                    "day": d,
                    "setups": setups,
                    "setup_count": len(setups),
                    "gross": round(sum(float(s["gross"]) for s in setups), 2),
                    "charges": round(sum(float(s["charges"]) for s in setups), 2),
                    "net": round(sum(float(s["net"]) for s in setups), 2),
                }
            )
    finally:
        cw.CAPITAL = old_cap
    return daily, _score(daily)


def _print_block(title: str, daily: list, sc: dict) -> list[str]:
    lines = [
        "",
        "=" * 88,
        title,
        (
            f"TOTAL NET ₹{sc['net']:,.2f}  |  {sc['green']}G / {sc['red']}R  |  "
            f"worst ₹{sc['worst']:,.2f}  |  best ₹{sc['best']:,.2f}  |  avg ₹{sc['avg']:,.2f}"
        ),
        "=" * 88,
    ]
    print("\n".join(lines))
    for day in daily:
        print(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        lines.append(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            row = (
                f"  #{s.get('setup_id')} {s.get('entry_t')}→{s.get('exit_t')}  "
                f"CE{s.get('ce_strike')} {s.get('ce_entry')}→{s.get('ce_exit')} ({s.get('ce_mult')}×)  "
                f"PE{s.get('pe_strike')} {s.get('pe_entry')}→{s.get('pe_exit')} ({s.get('pe_mult')}×)  "
                f"lots={s.get('lots')}  NET ₹{float(s.get('net') or 0):,.2f}"
            )
            why = f"    {s.get('exit_why')}"
            if s.get("win_exit_t"):
                why += f"  win@{s['win_exit_t']} {s.get('win_side')}"
            print(row)
            print(why)
            lines.extend([row, why])
    return lines


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    frames = {}
    for d in LAST5:
        path = cw.CHAIN / d / "viewer.json"
        if path.exists():
            frames[d] = list(json.loads(path.read_text(encoding="utf-8")).get("frames") or [])
    days = [d for d in LAST5 if d in frames]

    header = [
        "ALGO-CW PRE-WIREUP BACKTEST — LAST 5 CAUSAL (no look-ahead)",
        "Band 70–77 · ₹1L × 90% · lot=75 · live config NOT changed",
        "",
    ]
    print("\n".join(header))
    all_lines = header[:]

    live_d, live_s = _run_hit(days, frames, lot=lot, cfg=cfg, max_setups=2, skip_ge=SKIP_LIVE)
    a_d, a_s = _run_hit(days, frames, lot=lot, cfg=cfg, max_setups=2, skip_ge=SKIP_A)
    b_d, b_s = _run_fallen_max1(days, frames, lot=lot, cfg=cfg)
    c_d, c_s = _run_fallen_then_2nd(days, frames, lot=lot, cfg=cfg, skip_ge=SKIP_A)

    all_lines += _print_block(
        f"LIVE-ish: hit {WIN:g}× · max2 · skip 2nd if day ≥ ₹{SKIP_LIVE:,.0f}", live_d, live_s
    )
    all_lines += _print_block(
        f"A: hit {WIN:g}× · max2 · skip 2nd if day ≥ ₹{SKIP_A:,.0f}", a_d, a_s
    )
    all_lines += _print_block(
        f"B: winner@{WIN:g}× + fallen bounce@{FALLEN_PCT:g}% · max1", b_d, b_s
    )
    all_lines += _print_block(
        f"C: fallen@{FALLEN_PCT:g}% then 2nd hit · skip 2nd if day ≥ ₹{SKIP_A:,.0f}", c_d, c_s
    )

    # summary table
    rows = [
        ("LIVE", live_s),
        ("A skip@3k", a_s),
        ("B fallen@12%", b_s),
        ("C fallen+2nd@3k", c_s),
    ]
    summary = [
        "",
        "=" * 88,
        "SUMMARY",
        f"{'candidate':<22} {'net':>12} {'G/R':>7} {'worst':>12} {'best':>12} {'vs LIVE':>12}",
    ]
    for name, sc in rows:
        delta = sc["net"] - live_s["net"]
        summary.append(
            f"{name:<22} ₹{sc['net']:>10,.2f}  {sc['green']}G/{sc['red']}R  "
            f"₹{sc['worst']:>10,.2f}  ₹{sc['best']:>10,.2f}  ₹{delta:>+10,.2f}"
        )
    summary.append("=" * 88)
    print("\n".join(summary))
    all_lines.extend(summary)

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo_cw_wire_candidates.txt"
    js = OUT / "bt_algo_cw_wire_candidates.json"
    txt.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "days": days,
                "causal": True,
                "live": {"summary": live_s, "daily": live_d},
                "A_skip_3k": {"summary": a_s, "daily": a_d},
                "B_fallen_12": {"summary": b_s, "daily": b_d},
                "C_fallen_then_2nd": {"summary": c_s, "daily": c_d},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")


if __name__ == "__main__":
    main()

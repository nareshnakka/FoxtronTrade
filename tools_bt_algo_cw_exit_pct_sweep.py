"""
Algo-CW last-5 causal sweep: find best exit % (no look-ahead, no 2nd entry).

A) Fallen bounce % after winner book @ 1.45×
B) Winner giveback % from peak (retrace) after arming @ 1.45× — square both
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_algo1_exit_learn as L
import tools_bt_algo_cw_fallen_retrace_last5 as F
import tools_bt_final_daily_report as cw
from foxtrade.config_store import load_config

LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
OUT = Path(__file__).resolve().parent / "data" / "backtests"
WIN_MULT = 1.45
MAX_SETUPS = 1


def run_setup_fallen(
    frames, merge, *, lots, lot_size, cfg, setup_id, bmin: float, bmax: float
) -> dict[str, Any]:
    # Temporarily patch module constants used inside the function body
    old = (F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT)
    F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT = bmin, bmax, WIN_MULT
    try:
        return F.run_setup_fallen_retrace(
            frames, merge, lots=lots, lot_size=lot_size, cfg=cfg, setup_id=setup_id
        )
    finally:
        F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT = old


def simulate_fallen_day(
    day: str, frames: list, *, lot_size: int, cfg: dict, bmin: float, bmax: float
) -> dict:
    merge = cw.find_merge_after(frames, after_t=cw.MERGE_AFTER, before_t=None, exclude_pairs=set())
    if not merge:
        return {"day": day, "setups": [], "setup_count": 0, "net": 0.0, "gross": 0.0, "charges": 0.0}
    lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot_size)
    res = run_setup_fallen(
        frames, merge, lots=lots, lot_size=lot_size, cfg=cfg, setup_id=1, bmin=bmin, bmax=bmax
    )
    return {
        "day": day,
        "setups": [res],
        "setup_count": 1,
        "gross": float(res["gross"]),
        "charges": float(res["charges"]),
        "net": float(res["net"]),
    }


def score(daily: list[dict]) -> dict:
    nets = [float(d["net"]) for d in daily]
    return {
        "net": round(sum(nets), 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "worst": round(min(nets), 2) if nets else 0,
        "best": round(max(nets), 2) if nets else 0,
        "avg": round(sum(nets) / len(nets), 2) if nets else 0,
        "daily_nets": [round(n, 2) for n in nets],
    }


def rank_key(sc: dict) -> tuple:
    # Prefer: more green, higher worst-day, then total net
    return (sc["green"], sc["worst"], sc["net"])


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
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
        # —— A) Fallen bounce sweeps ——
        fallen_rows: list[dict] = []

        # Single threshold: sell at first touch of X% bounce (min=max=X)
        for x in [5, 8, 10, 12, 15, 18, 20, 25, 30, 35, 40, 50]:
            daily = [
                simulate_fallen_day(d, frames[d], lot_size=lot, cfg=cfg, bmin=float(x), bmax=float(x))
                for d in days
            ]
            sc = score(daily)
            fallen_rows.append(
                {"kind": "fallen_single", "label": f"bounce@{x}%", "bmin": x, "bmax": x, **sc}
            )

        # Ranges: book from min, force by max
        ranges = [
            (5, 15), (5, 20), (5, 30), (5, 50),
            (8, 20), (8, 30), (10, 20), (10, 25), (10, 30), (10, 40), (10, 50),
            (12, 25), (12, 30), (15, 30), (15, 40), (15, 50),
            (20, 40), (20, 50), (25, 50),
        ]
        for lo, hi in ranges:
            daily = [
                simulate_fallen_day(d, frames[d], lot_size=lot, cfg=cfg, bmin=float(lo), bmax=float(hi))
                for d in days
            ]
            sc = score(daily)
            fallen_rows.append(
                {
                    "kind": "fallen_range",
                    "label": f"bounce {lo}–{hi}%",
                    "bmin": lo,
                    "bmax": hi,
                    **sc,
                }
            )

        # Baseline: square both at 1.45, no 2nd
        base_daily = []
        for d in days:
            merge = cw.find_merge_after(
                frames[d], after_t=cw.MERGE_AFTER, before_t=None, exclude_pairs=set()
            )
            if not merge:
                base_daily.append({"day": d, "net": 0.0})
                continue
            lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot)
            res = cw.run_one_setup(
                frames[d], merge, lots=lots, lot_size=lot, cfg=cfg, setup_id=1
            )
            base_daily.append({"day": d, "net": float(res["net"]), "setups": [res], "setup_count": 1})
        base_sc = score(base_daily)
        fallen_rows.append(
            {"kind": "baseline", "label": "square-both@1.45×", "bmin": None, "bmax": None, **base_sc}
        )

        fallen_rows.sort(key=rank_key, reverse=True)

        # —— B) Winner retrace % (square both after peak giveback) ——
        winner_rows: list[dict] = []
        # hit baseline
        hit_pol = L.Policy("hit_1.45", WIN_MULT, "hit", max_setups=MAX_SETUPS, skip_2nd_if_net_ge=1e18)
        d_hit, s_hit = L.run_universe(days, frames, policy=hit_pol, lot_size=lot, cfg=cfg)
        winner_rows.append(
            {
                "kind": "winner_hit",
                "label": "hit@1.45× (no retrace)",
                "retrace_pct": 0,
                "net": s_hit["net"],
                "green": s_hit["green_days"],
                "red": s_hit["red_days"],
                "worst": s_hit["worst_day"],
                "best": s_hit["best_day"],
                "avg": s_hit["avg_day"],
            }
        )
        for dd in [5, 8, 10, 12, 15, 18, 20, 25, 30, 35, 40, 50]:
            pol = L.Policy(
                f"retrace_1.45_dd{dd}",
                WIN_MULT,
                "retrace",
                retrace_pct=float(dd),
                max_setups=MAX_SETUPS,
                skip_2nd_if_net_ge=1e18,
            )
            _, sc = L.run_universe(days, frames, policy=pol, lot_size=lot, cfg=cfg)
            winner_rows.append(
                {
                    "kind": "winner_retrace",
                    "label": f"winner giveback {dd}%",
                    "retrace_pct": dd,
                    "net": sc["net"],
                    "green": sc["green_days"],
                    "red": sc["red_days"],
                    "worst": sc["worst_day"],
                    "best": sc["best_day"],
                    "avg": sc["avg_day"],
                }
            )
        winner_rows.sort(key=rank_key, reverse=True)

    finally:
        cw.CAPITAL = old_cap

    best_f = fallen_rows[0]
    best_w = winner_rows[0]

    lines = [
        "=" * 88,
        "ALGO2 EXIT % SWEEP — LAST 5 CAUSAL · no 2nd entry · ₹1L × 90%",
        "Score: green days → worst day → total net",
        "=" * 88,
        "",
        "BEST FALLEN BOUNCE EXIT:",
        f"  {best_f['label']}  →  ₹{best_f['net']:,.2f}  |  {best_f['green']}G/{best_f['red']}R  |  "
        f"worst ₹{best_f['worst']:,.2f}  |  avg ₹{best_f['avg']:,.2f}",
        "",
        "BEST WINNER GIVEBACK EXIT:",
        f"  {best_w['label']}  →  ₹{best_w['net']:,.2f}  |  {best_w['green']}G/{best_w['red']}R  |  "
        f"worst ₹{best_w['worst']:,.2f}  |  avg ₹{best_w['avg']:,.2f}",
        "",
        "-" * 88,
        "TOP 12 FALLEN BOUNCE (after book winner @ 1.45×)",
        "-" * 88,
        f"{'rank':>4}  {'rule':<22}  {'net':>10}  {'G/R':>5}  {'worst':>10}  {'avg':>10}",
    ]
    for i, r in enumerate(fallen_rows[:12], 1):
        lines.append(
            f"{i:>4}  {r['label']:<22}  ₹{r['net']:>9,.2f}  {r['green']}G/{r['red']}R  "
            f"₹{r['worst']:>9,.2f}  ₹{r['avg']:>9,.2f}"
        )

    lines += [
        "",
        "-" * 88,
        "TOP 12 WINNER GIVEBACK (square both)",
        "-" * 88,
        f"{'rank':>4}  {'rule':<28}  {'net':>10}  {'G/R':>5}  {'worst':>10}  {'avg':>10}",
    ]
    for i, r in enumerate(winner_rows[:12], 1):
        lines.append(
            f"{i:>4}  {r['label']:<28}  ₹{r['net']:>9,.2f}  {r['green']}G/{r['red']}R  "
            f"₹{r['worst']:>9,.2f}  ₹{r['avg']:>9,.2f}"
        )

    # Overall pick
    overall = best_f if rank_key(best_f) >= rank_key(best_w) else best_w
    lines += [
        "",
        "=" * 88,
        f"OVERALL BEST EXIT: {overall['label']}",
        f"  ₹{overall['net']:,.2f} · {overall['green']}G/{overall['red']}R · worst ₹{overall['worst']:,.2f}",
        "=" * 88,
    ]

    text = "\n".join(lines)
    print(text)

    OUT.mkdir(parents=True, exist_ok=True)
    txt = OUT / "bt_algo_cw_exit_pct_sweep.txt"
    js = OUT / "bt_algo_cw_exit_pct_sweep.json"
    txt.write_text(text + "\n", encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "days": days,
                "best_fallen": best_f,
                "best_winner_retrace": best_w,
                "overall": overall,
                "fallen_all": fallen_rows,
                "winner_all": winner_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {txt}")
    print(f"Wrote {js}")


if __name__ == "__main__":
    main()

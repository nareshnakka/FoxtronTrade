"""
Algo-CW optimization matrix — last 5 days, causal, no look-ahead.
Compares levers: 2nd entry, lock-green, exit mult, fallen bounce %.
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


def score(daily: list[dict]) -> dict:
    nets = [float(d["net"]) for d in daily]
    return {
        "net": round(sum(nets), 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "worst": round(min(nets), 2) if nets else 0,
        "best": round(max(nets), 2) if nets else 0,
        "avg": round(sum(nets) / len(nets), 2) if nets else 0,
        "daily": [round(n, 2) for n in nets],
    }


def run_policy(days, frames, *, lot, cfg, win_mult, mode, max_setups, skip_ge, retrace_pct=0.0):
    pol = L.Policy(
        "x",
        float(win_mult),
        mode,
        retrace_pct=float(retrace_pct),
        max_setups=int(max_setups),
        skip_2nd_if_net_ge=float(skip_ge),
    )
    daily, sc = L.run_universe(days, frames, policy=pol, lot_size=lot, cfg=cfg)
    return score(daily), daily


def run_fallen(days, frames, *, lot, cfg, bmin, bmax, win_mult=1.45):
    old_c = cw.CAPITAL
    cw.CAPITAL = float(cfg.get("algo_cw_capital_inr") or 100_000)
    old = (F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT, F.MAX_SETUPS)
    F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT, F.MAX_SETUPS = bmin, bmax, win_mult, 1
    try:
        daily = []
        for d in days:
            merge = cw.find_merge_after(
                frames[d], after_t=cw.MERGE_AFTER, before_t=None, exclude_pairs=set()
            )
            if not merge:
                daily.append({"day": d, "net": 0.0, "setup_count": 0})
                continue
            lots = cw.lots_for_entry(merge["ce0"], merge["pe0"], lot)
            res = F.run_setup_fallen_retrace(
                frames[d], merge, lots=lots, lot_size=lot, cfg=cfg, setup_id=1
            )
            daily.append({"day": d, "net": float(res["net"]), "setup_count": 1, "setups": [res]})
        return score(daily), daily
    finally:
        F.BOUNCE_MIN_PCT, F.BOUNCE_MAX_PCT, F.WIN_MULT, F.MAX_SETUPS = old
        cw.CAPITAL = old_c


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    frames = {}
    for d in LAST5:
        path = cw.CHAIN / d / "viewer.json"
        if path.exists():
            frames[d] = list(json.loads(path.read_text(encoding="utf-8")).get("frames") or [])
    days = [d for d in LAST5 if d in frames]

    rows = []

    def add(label, group, sc):
        rows.append({"label": label, "group": group, **{k: sc[k] for k in ("net", "green", "red", "worst", "best", "avg", "daily")}})

    # —— Live-ish baselines ——
    sc, _ = run_policy(days, frames, lot=lot, cfg=cfg, win_mult=1.45, mode="hit", max_setups=2, skip_ge=15000)
    add("LIVE-ish: hit 1.45× · max2 · skip@₹15k", "baseline", sc)

    sc, _ = run_policy(days, frames, lot=lot, cfg=cfg, win_mult=1.45, mode="hit", max_setups=1, skip_ge=1e18)
    add("hit 1.45× · max1 (no 2nd)", "baseline", sc)

    sc, _ = run_policy(days, frames, lot=lot, cfg=cfg, win_mult=1.45, mode="hit", max_setups=2, skip_ge=1)
    add("LOCK-GREEN: hit 1.45× · skip 2nd if day>0", "lock", sc)

    # —— Exit mult (max1) ——
    for m in (1.35, 1.40, 1.45, 1.50, 1.55, 1.60, 1.70):
        sc, _ = run_policy(days, frames, lot=lot, cfg=cfg, win_mult=m, mode="hit", max_setups=1, skip_ge=1e18)
        add(f"hit {m:g}× · max1", "exit_mult", sc)

    # —— Winner giveback (hurts usually) ——
    for dd in (5, 10, 15):
        sc, _ = run_policy(
            days, frames, lot=lot, cfg=cfg, win_mult=1.45, mode="retrace",
            max_setups=1, skip_ge=1e18, retrace_pct=dd,
        )
        add(f"winner giveback {dd}% after 1.45× · max1", "winner_retrace", sc)

    # —— 2nd entry skip thresholds ——
    for skip in (1, 2000, 3000, 5000, 8000, 10000, 15000, 1e18):
        lab = "never skip 2nd" if skip >= 1e17 else f"skip 2nd if day≥₹{skip:,.0f}"
        sc, _ = run_policy(days, frames, lot=lot, cfg=cfg, win_mult=1.45, mode="hit", max_setups=2, skip_ge=skip)
        add(f"hit 1.45× · max2 · {lab}", "second_entry", sc)

    # —— Fallen bounce after winner book ——
    for b in (8, 10, 12, 15, 20):
        sc, _ = run_fallen(days, frames, lot=lot, cfg=cfg, bmin=float(b), bmax=float(b))
        add(f"book winner@1.45× + fallen bounce@{b}% · max1", "fallen", sc)

    sc, _ = run_fallen(days, frames, lot=lot, cfg=cfg, bmin=12.0, bmax=30.0)
    add("book winner@1.45× + fallen 12–30% · max1", "fallen", sc)

    # Combo: fallen@12 + no path for 2nd (already max1)
    # Combo: lock-green on square-both already above

    rows.sort(key=lambda r: (r["green"], r["worst"], r["net"]), reverse=True)

    lines = [
        "=" * 96,
        "ALGO-CW OPTIMIZE MATRIX — LAST 5 CAUSAL · ₹1L × 90% · 70–77",
        "Rank: green days → worst day → total net",
        "=" * 96,
        f"{'#':>2}  {'group':<14}  {'rule':<52}  {'net':>10}  {'G/R':>5}  {'worst':>9}  {'daily'}",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i:>2}  {r['group']:<14}  {r['label']:<52}  "
            f"₹{r['net']:>9,.0f}  {r['green']}G/{r['red']}R  ₹{r['worst']:>8,.0f}  {r['daily']}"
        )

    # Best by objective
    best_green = max(rows, key=lambda r: (r["green"], r["worst"], r["net"]))
    best_net = max(rows, key=lambda r: (r["net"], r["green"], r["worst"]))
    best_risk = max(
        [r for r in rows if r["red"] == 0],
        key=lambda r: (r["worst"], r["net"]),
        default=best_green,
    )

    lines += [
        "",
        "=" * 96,
        f"BEST GREEN/SAFE: {best_green['label']}  → ₹{best_green['net']:,.0f} · {best_green['green']}G/{best_green['red']}R · worst ₹{best_green['worst']:,.0f}",
        f"BEST TOTAL ₹:     {best_net['label']}  → ₹{best_net['net']:,.0f} · {best_net['green']}G/{best_net['red']}R · worst ₹{best_net['worst']:,.0f}",
        f"BEST 0-RED↑WORST:{best_risk['label']}  → ₹{best_risk['net']:,.0f} · worst ₹{best_risk['worst']:,.0f}",
        "=" * 96,
    ]
    text = "\n".join(lines)
    print(text)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bt_algo_cw_optimize_matrix.txt").write_text(text + "\n", encoding="utf-8")
    (OUT / "bt_algo_cw_optimize_matrix.json").write_text(
        json.dumps({"days": days, "rows": rows, "best_green": best_green, "best_net": best_net}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {OUT / 'bt_algo_cw_optimize_matrix.txt'}")


if __name__ == "__main__":
    main()

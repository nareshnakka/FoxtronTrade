"""Compare FastTrack vs Balanced reports."""
from __future__ import annotations

import json
from pathlib import Path


def load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def stats(d: dict, label: str) -> dict:
    t = d["totals"]
    c = d["capital"]
    daily = d["daily"]
    nets = [float(x.get("net") or 0) for x in daily]
    bals = [float(x.get("balance_close") or c["initial"]) for x in daily]
    peak = float(c["initial"])
    maxdd = 0.0
    maxdd_pct = 0.0
    for b in bals:
        peak = max(peak, b)
        dd = peak - b
        if dd > maxdd:
            maxdd = dd
        if peak > 0 and dd / peak > maxdd_pct:
            maxdd_pct = dd / peak
    trades = d.get("ledger") or []
    tn = [float(x.get("net") or 0) for x in trades]
    tw = [n for n in tn if n > 0]
    tl = [n for n in tn if n < 0]
    hedge_nets = [float(x.get("hedge_net") or 0) for x in trades if x.get("hedged")]
    return {
        "label": label,
        "net": round(t["net"]),
        "ret": c["return_pct"],
        "final": round(c["final_balance"]),
        "wl": f"{t['wins']}/{t['losses']}",
        "cov": f"{t['traded_days']}/{t['session_days']}",
        "gr": f"{t['green_days']}/{t['red_days']}",
        "invested": round(t["invested_total"]),
        "best_day": round(max(nets)) if nets else 0,
        "worst_day": round(min(nets)) if nets else 0,
        "max_dd": round(maxdd),
        "max_dd_pct": round(100 * maxdd_pct, 1),
        "avg_win": round(sum(tw) / len(tw)) if tw else 0,
        "avg_loss": round(sum(tl) / len(tl)) if tl else 0,
        "hedge_sum": round(sum(hedge_nets)) if hedge_nets else None,
    }


def main() -> int:
    pairs = [
        ("FT 30d", "data/backtests/week_2026-10-05_to_2026-11-03_fasttrack_report.json"),
        ("BAL 30d", "data/backtests/week_2026-10-05_to_2026-11-03_balanced_report.json"),
        ("FT 90d", "data/backtests/week_2026-08-06_to_2026-11-03_fasttrack_report.json"),
        ("BAL 90d", "data/backtests/week_2026-08-06_to_2026-11-03_balanced_report.json"),
    ]
    rows = [stats(load(p), lab) for lab, p in pairs]
    keys = [
        "label",
        "net",
        "ret",
        "final",
        "wl",
        "cov",
        "gr",
        "invested",
        "best_day",
        "worst_day",
        "max_dd",
        "max_dd_pct",
        "avg_win",
        "avg_loss",
        "hedge_sum",
    ]
    print("|".join(keys))
    for r in rows:
        print("|".join(str(r.get(k)) for k in keys))

    ft = load(pairs[0][1])
    bal = load(pairs[1][1])
    print("\n30d day gaps (FT net − BAL net) where |diff| > 1500:")
    fd = {x["date"]: float(x.get("net") or 0) for x in ft["daily"]}
    bd = {x["date"]: float(x.get("net") or 0) for x in bal["daily"]}
    gaps = []
    for d in fd:
        diff = fd[d] - bd[d]
        if abs(diff) > 1500:
            gaps.append((diff, d, fd[d], bd[d]))
    for diff, d, a, b in sorted(gaps, reverse=True):
        print(f"  {d}  FT {a:+.0f}  BAL {b:+.0f}  delta {diff:+.0f}")

    ft90 = load(pairs[2][1])
    bal90 = load(pairs[3][1])
    print("\n90d day gaps where |diff| > 3000:")
    fd = {x["date"]: float(x.get("net") or 0) for x in ft90["daily"]}
    bd = {x["date"]: float(x.get("net") or 0) for x in bal90["daily"]}
    gaps = []
    for d in fd:
        diff = fd[d] - bd[d]
        if abs(diff) > 3000:
            gaps.append((diff, d, fd[d], bd[d]))
    for diff, d, a, b in sorted(gaps, reverse=True)[:15]:
        print(f"  {d}  FT {a:+.0f}  BAL {b:+.0f}  delta {diff:+.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

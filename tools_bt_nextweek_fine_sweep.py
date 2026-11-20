"""Fine exit-mult sweep for next-week dual around the profit peak."""
from __future__ import annotations

import json
from pathlib import Path

import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

# Fine grid: +40% … +120% of entry (1.40× … 2.20×)
MULTS = [round(1.40 + i * 0.05, 2) for i in range(17)]  # 1.40 .. 2.20

OUT_JSON = Path("data/backtests/nextweek_fine_sweep.json")
OUT_TXT = Path("data/backtests/nextweek_fine_sweep.txt")


def sim_day(day, frames, meta, *, mult, lot_size, cfg):
    expiry = str(meta.get("expiry") or "")
    setups = []
    used = set()
    hunt = nw.MERGE_AFTER
    sid = 0
    day_net = 0.0
    old = nw.WIN_MULT
    nw.WIN_MULT = float(mult)
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
            res = nw.run_one_setup(
                frames, merge, lots=lots, lot_size=lot_size, cfg=cfg,
                setup_id=sid, expiry=expiry,
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
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "setups": len(setups),
        "detail": [
            {
                "id": s["setup_id"],
                "in": s["entry_t"],
                "out": s["exit_t"],
                "win": s["win_side"],
                "ce_mult": s["ce_mult"],
                "pe_mult": s["pe_mult"],
                "net": s["net"],
            }
            for s in setups
        ],
    }


def main():
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    days = nw.available_days()
    cache = {d: nw.load_nextweek_frames(d) for d in days}

    rows = []
    for mult in MULTS:
        daily = []
        for d in days:
            fr, meta = cache[d]
            if not fr:
                daily.append({"day": d, "net": 0, "setups": 0})
                continue
            daily.append(sim_day(d, fr, meta, mult=mult, lot_size=lot, cfg=cfg))
        total = round(sum(float(x["net"]) for x in daily), 2)
        pct = round((mult - 1) * 100, 1)
        rows.append({"mult": mult, "pct": pct, "total_net": total, "daily": daily})
        print(f"{mult:.2f}×  +{pct:5.1f}%  NET ₹{total:>12,.2f}")

    best = max(rows, key=lambda r: r["total_net"])
    # Robust zone: within 5% of best net
    floor = best["total_net"] * 0.95
    zone = [r for r in rows if r["total_net"] >= floor]

    # Per-day best on fine grid
    day_best = []
    for d in days:
        curve = []
        for r in rows:
            dd = next(x for x in r["daily"] if x["day"] == d)
            curve.append({"mult": r["mult"], "pct": r["pct"], "net": dd["net"]})
        b = max(curve, key=lambda x: x["net"])
        day_best.append({"day": d, **b})

    report = {
        "best": best,
        "robust_zone": zone,
        "rows": [{"mult": r["mult"], "pct": r["pct"], "total_net": r["total_net"]} for r in rows],
        "day_best": day_best,
        "full": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "=" * 80,
        "NEXT-WEEK fine exit sweep → recommended profit target",
        f"BEST FIXED: {best['mult']:.2f}×  (+{best['pct']:g}% on premium)  "
        f"NET ₹{best['total_net']:,.2f}",
        f"Robust zone (≥95% of best): "
        + ", ".join(f"{z['mult']:.2f}×(+{z['pct']:g}%)" for z in zone),
        "=" * 80,
        "",
        "TOTAL NET curve:",
    ]
    for r in rows:
        mark = " ◀ BEST" if r["mult"] == best["mult"] else ""
        lines.append(
            f"  {r['mult']:.2f}×  +{r['pct']:5.1f}%  ₹{r['total_net']:>12,.2f}{mark}"
        )
    lines.append("")
    lines.append("Per-day hindsight best (not a live rule):")
    for db in day_best:
        lines.append(
            f"  {db['day']}  {db['mult']:.2f}× (+{db['pct']:g}%)  ₹{db['net']:,.2f}"
        )

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"BEST {best['mult']:.2f}× (+{best['pct']:g}%)  NET ₹{best['total_net']:,.2f}")
    print("Robust:", [(z["mult"], z["pct"]) for z in zone])
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()

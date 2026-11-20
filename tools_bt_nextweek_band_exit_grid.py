"""
Next-week grid: merge-band × exit-mult → find best convergence + exit %.

Goal: profit every traded day when possible; else max day-wins / total net.
Walk-forward, no look-ahead. Uses SQLite week='next' only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_nextweek_daily_report as nw
from foxtrade.config_store import load_config

OUT_JSON = Path("data/backtests/nextweek_band_exit_grid.json")
OUT_TXT = Path("data/backtests/nextweek_band_exit_grid.txt")

# Convergence windows (lo, hi, label) — ~8pt wide like live 70–77
BANDS: list[tuple[float, float, str]] = [
    (26, 34, "30"),
    (36, 44, "40"),
    (46, 54, "50"),
    (56, 64, "60"),
    (66, 74, "70"),
    (70, 77, "70-77"),  # live
    (76, 84, "80"),
    (86, 94, "90"),
    (96, 104, "100"),
    (106, 114, "110"),
    (116, 124, "120"),
    (126, 134, "130"),
    (136, 144, "140"),
    (146, 154, "150"),
]

MULTS = [1.30, 1.40, 1.45, 1.50, 1.60, 1.70, 1.80, 1.90, 2.00, 2.20, 2.50]


def find_merge_band(
    frames: list[dict],
    *,
    band_lo: float,
    band_hi: float,
    after_t: str,
    before_t: str | None,
    exclude_pairs: set[tuple[int, int]],
) -> dict[str, Any] | None:
    center = (band_lo + band_hi) / 2.0
    streak = 0
    best: dict[str, Any] | None = None
    after_m = nw._hm_to_min(after_t)
    before_m = nw._hm_to_min(before_t) if before_t else 10_000

    def in_soft(px: float) -> bool:
        return band_lo - 2 <= px <= band_hi + 2

    for fr in frames:
        t = fr.get("t") or ""
        if not t or t < nw.MERGE_AFTER:
            continue
        tm = nw._hm_to_min(t)
        if tm < after_m:
            continue
        if tm >= before_m:
            break
        if t > nw.SESSION_END:
            break
        atm = int(fr.get("atm") or 0)
        ces: list[tuple[int, float]] = []
        pes: list[tuple[int, float]] = []
        for k in nw._strikes(fr):
            ce = nw._leg_c(fr, "CE", k)
            pe = nw._leg_c(fr, "PE", k)
            if ce is not None and in_soft(ce):
                ces.append((k, ce))
            if pe is not None and in_soft(pe):
                pes.append((k, pe))
        if not ces or not pes:
            streak = 0
            best = None
            continue
        pair = None
        for ck, ce in sorted(ces, key=lambda x: abs(x[1] - center)):
            for pk, pe in sorted(pes, key=lambda x: abs(x[1] - center)):
                if (ck, pk) in exclude_pairs:
                    continue
                gap = abs(ce - pe)
                mid = (ce + pe) / 2
                if gap > nw.MERGE_GAP or not (band_lo <= mid <= band_hi):
                    continue
                score = (gap, abs(mid - center), abs(((ck + pk) / 2) - atm))
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
        if streak >= 2:
            return best
    return best if streak >= 1 else None


def simulate_combo(
    day: str,
    frames: list[dict],
    meta: dict,
    *,
    band_lo: float,
    band_hi: float,
    mult: float,
    lot_size: int,
    cfg: dict,
) -> dict[str, Any]:
    expiry = str(meta.get("expiry") or "")
    setups: list[dict] = []
    used: set[tuple[int, int]] = set()
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
            merge = find_merge_band(
                frames,
                band_lo=band_lo,
                band_hi=band_hi,
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
            setups.append(
                {
                    "id": sid,
                    "entry_t": res["entry_t"],
                    "exit_t": res["exit_t"],
                    "win": res["win_side"],
                    "ce_k": res["ce_strike"],
                    "pe_k": res["pe_strike"],
                    "ce": f"{res['ce_entry']}→{res['ce_exit']} ({res['ce_mult']}×)",
                    "pe": f"{res['pe_entry']}→{res['pe_exit']} ({res['pe_mult']}×)",
                    "lots": res["lots"],
                    "net": res["net"],
                    "exit_why": res.get("exit_why"),
                }
            )
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
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "setup_count": len(setups),
        "setups": setups,
    }


def score_combo(daily: list[dict]) -> dict[str, Any]:
    traded = [d for d in daily if d["setup_count"] > 0]
    wins = [d for d in traded if float(d["net"]) > 0]
    losses = [d for d in traded if float(d["net"]) < 0]
    flats = [d for d in traded if float(d["net"]) == 0]
    total = round(sum(float(d["net"]) for d in daily), 2)
    every_day = len(traded) > 0 and len(losses) == 0
    return {
        "total_net": total,
        "days_traded": len(traded),
        "day_wins": len(wins),
        "day_losses": len(losses),
        "day_flats": len(flats),
        "profit_every_traded_day": every_day,
        "win_rate": round(len(wins) / len(traded), 3) if traded else 0.0,
        "min_day_net": min((float(d["net"]) for d in traded), default=0.0),
        "setups": sum(d["setup_count"] for d in daily),
    }


def main() -> None:
    cfg = load_config()
    lot_size = int(cfg.get("lot_size") or 75)
    days = nw.available_days()
    cache = {d: nw.load_nextweek_frames(d) for d in days}
    print(f"Next-week days: {days}")
    print(f"Bands={len(BANDS)}  Mults={len(MULTS)}  combos={len(BANDS)*len(MULTS)}")

    results: list[dict[str, Any]] = []
    for band_lo, band_hi, label in BANDS:
        for mult in MULTS:
            daily = []
            for d in days:
                fr, meta = cache[d]
                if not fr:
                    daily.append(
                        {"day": d, "net": 0, "setup_count": 0, "setups": []}
                    )
                    continue
                daily.append(
                    simulate_combo(
                        d,
                        fr,
                        meta,
                        band_lo=band_lo,
                        band_hi=band_hi,
                        mult=mult,
                        lot_size=lot_size,
                        cfg=cfg,
                    )
                )
            sc = score_combo(daily)
            row = {
                "band": label,
                "band_lo": band_lo,
                "band_hi": band_hi,
                "mult": mult,
                "pct": round((mult - 1) * 100, 1),
                **sc,
                "daily": daily,
            }
            results.append(row)
            flag = " ★ EVERY DAY" if sc["profit_every_traded_day"] else ""
            print(
                f"band {label:>5} ({band_lo:g}-{band_hi:g})  "
                f"{mult:.2f}×(+{row['pct']:5.1f}%)  "
                f"NET ₹{sc['total_net']:>10,.0f}  "
                f"{sc['day_wins']}W/{sc['day_losses']}L "
                f"traded={sc['days_traded']}{flag}"
            )

    # Rank: 1) profit every traded day  2) more day wins  3) higher min day  4) total net
    ranked = sorted(
        results,
        key=lambda r: (
            1 if r["profit_every_traded_day"] else 0,
            r["day_wins"],
            -r["day_losses"],
            r["min_day_net"],
            r["total_net"],
        ),
        reverse=True,
    )

    every = [r for r in ranked if r["profit_every_traded_day"] and r["days_traded"] >= 2]
    best_every = every[0] if every else None
    best_net = max(results, key=lambda r: r["total_net"])
    live_like = next(
        (r for r in results if r["band"] == "70-77" and abs(r["mult"] - 1.45) < 1e-9),
        None,
    )
    live_190 = next(
        (r for r in results if r["band"] == "70-77" and abs(r["mult"] - 1.90) < 1e-9),
        None,
    )

    # Compact top tables
    top_every = every[:15]
    top_net = sorted(results, key=lambda r: r["total_net"], reverse=True)[:15]
    top_consistency = ranked[:20]

    report = {
        "days": days,
        "bands": [{"label": l, "lo": a, "hi": b} for a, b, l in BANDS],
        "mults": MULTS,
        "best_profit_every_day": (
            {
                "band": best_every["band"],
                "band_range": [best_every["band_lo"], best_every["band_hi"]],
                "mult": best_every["mult"],
                "pct": best_every["pct"],
                "total_net": best_every["total_net"],
                "day_wins": best_every["day_wins"],
                "day_losses": best_every["day_losses"],
                "days_traded": best_every["days_traded"],
                "min_day_net": best_every["min_day_net"],
                "daily": best_every["daily"],
            }
            if best_every
            else None
        ),
        "best_total_net": {
            "band": best_net["band"],
            "band_range": [best_net["band_lo"], best_net["band_hi"]],
            "mult": best_net["mult"],
            "pct": best_net["pct"],
            "total_net": best_net["total_net"],
            "day_wins": best_net["day_wins"],
            "day_losses": best_net["day_losses"],
            "daily": best_net["daily"],
        },
        "baselines": {
            "70-77 @1.45": live_like,
            "70-77 @1.90": live_190,
        },
        "top_every_day": [
            {
                "band": r["band"],
                "mult": r["mult"],
                "pct": r["pct"],
                "total_net": r["total_net"],
                "day_wins": r["day_wins"],
                "days_traded": r["days_traded"],
                "min_day_net": r["min_day_net"],
            }
            for r in top_every
        ],
        "top_total_net": [
            {
                "band": r["band"],
                "mult": r["mult"],
                "pct": r["pct"],
                "total_net": r["total_net"],
                "day_wins": r["day_wins"],
                "day_losses": r["day_losses"],
            }
            for r in top_net
        ],
        "all_compact": [
            {
                "band": r["band"],
                "mult": r["mult"],
                "pct": r["pct"],
                "total_net": r["total_net"],
                "day_wins": r["day_wins"],
                "day_losses": r["day_losses"],
                "days_traded": r["days_traded"],
                "profit_every_traded_day": r["profit_every_traded_day"],
                "min_day_net": r["min_day_net"],
            }
            for r in results
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    # Full results are large — write compact + best details only in json
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("NEXT-WEEK GRID — convergence band × exit mult")
    lines.append(f"Days: {', '.join(days)}")
    lines.append("=" * 100)

    if best_every:
        lines.append("")
        lines.append(
            f"BEST FOR PROFIT EVERY TRADED DAY: "
            f"band {best_every['band']} ({best_every['band_lo']:g}–{best_every['band_hi']:g})  "
            f"exit {best_every['mult']:.2f}× (+{best_every['pct']:g}%)  "
            f"NET ₹{best_every['total_net']:,.2f}  "
            f"{best_every['day_wins']}W/0L over {best_every['days_traded']} days  "
            f"min day ₹{best_every['min_day_net']:,.2f}"
        )
        lines.append("  Day breakdown:")
        for d in best_every["daily"]:
            if d["setup_count"] == 0:
                lines.append(f"    {d['day']}  no merge  ₹0")
                continue
            lines.append(
                f"    {d['day']}  setups={d['setup_count']}  NET ₹{d['net']:,.2f}"
            )
            for s in d["setups"]:
                lines.append(
                    f"      #{s['id']} {s['entry_t']}→{s['exit_t']}  "
                    f"CE{s['ce_k']} {s['ce']}  PE{s['pe_k']} {s['pe']}  "
                    f"win={s['win']} lots={s['lots']}  NET ₹{s['net']:,.2f}"
                )
    else:
        lines.append("")
        lines.append(
            "NO combo made profit on EVERY traded day (≥2 days). "
            "Showing best consistency instead."
        )
        b = top_consistency[0]
        lines.append(
            f"  BEST CONSISTENCY: band {b['band']}  {b['mult']:.2f}×  "
            f"NET ₹{b['total_net']:,.2f}  {b['day_wins']}W/{b['day_losses']}L"
        )

    lines.append("")
    lines.append(
        f"BEST TOTAL NET: band {best_net['band']} "
        f"({best_net['band_lo']:g}–{best_net['band_hi']:g})  "
        f"exit {best_net['mult']:.2f}× (+{best_net['pct']:g}%)  "
        f"NET ₹{best_net['total_net']:,.2f}  "
        f"{best_net['day_wins']}W/{best_net['day_losses']}L"
    )
    for d in best_net["daily"]:
        if d["setup_count"] == 0:
            lines.append(f"    {d['day']}  no merge  ₹0")
            continue
        lines.append(f"    {d['day']}  setups={d['setup_count']}  NET ₹{d['net']:,.2f}")
        for s in d["setups"]:
            lines.append(
                f"      #{s['id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_k']} {s['ce']}  PE{s['pe_k']} {s['pe']}  "
                f"win={s['win']} lots={s['lots']}  NET ₹{s['net']:,.2f}"
            )

    lines.append("")
    lines.append("Baselines (70–77 live band):")
    if live_like:
        lines.append(
            f"  1.45×  NET ₹{live_like['total_net']:,.2f}  "
            f"{live_like['day_wins']}W/{live_like['day_losses']}L"
        )
    if live_190:
        lines.append(
            f"  1.90×  NET ₹{live_190['total_net']:,.2f}  "
            f"{live_190['day_wins']}W/{live_190['day_losses']}L"
        )

    lines.append("")
    lines.append("-" * 100)
    lines.append("TOP combos that profit EVERY traded day (≥2 days):")
    if not top_every:
        lines.append("  (none)")
    for r in top_every:
        lines.append(
            f"  band {r['band']:>5}  {r['mult']:.2f}×(+{r['pct']:5.1f}%)  "
            f"NET ₹{r['total_net']:>10,.2f}  "
            f"{r['day_wins']}W  traded={r['days_traded']}  "
            f"minDay ₹{r['min_day_net']:,.2f}"
        )

    lines.append("")
    lines.append("TOP 15 by TOTAL NET:")
    for r in top_net:
        every_flag = " ★" if r["day_losses"] == 0 and r["day_wins"] >= 2 else ""
        lines.append(
            f"  band {r['band']:>5}  {r['mult']:.2f}×(+{r['pct']:5.1f}%)  "
            f"NET ₹{r['total_net']:>10,.2f}  "
            f"{r['day_wins']}W/{r['day_losses']}L{every_flag}"
        )

    # Heat: best mult per band
    lines.append("")
    lines.append("Best exit per convergence band:")
    for band_lo, band_hi, label in BANDS:
        subset = [r for r in results if r["band"] == label]
        if not subset:
            continue
        b = max(subset, key=lambda r: (r["profit_every_traded_day"], r["total_net"]))
        lines.append(
            f"  {label:>5} ({band_lo:g}–{band_hi:g})  →  {b['mult']:.2f}× "
            f"(+{b['pct']:g}%)  NET ₹{b['total_net']:,.2f}  "
            f"{b['day_wins']}W/{b['day_losses']}L"
            + ("  ★ every day" if b["profit_every_traded_day"] else "")
        )

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print("=" * 72)
    if best_every:
        print(
            f"EVERY-DAY BEST: band {best_every['band']} @ {best_every['mult']:.2f}× "
            f"(+{best_every['pct']:g}%)  NET ₹{best_every['total_net']:,.2f}"
        )
    print(
        f"MAX NET: band {best_net['band']} @ {best_net['mult']:.2f}× "
        f"NET ₹{best_net['total_net']:,.2f}  "
        f"{best_net['day_wins']}W/{best_net['day_losses']}L"
    )
    print(f"Wrote {OUT_TXT}")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()

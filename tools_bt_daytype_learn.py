"""
Classify dual-pair day types from 1m chain (causal labels + early tells).

Day types (from trader playbook):
  STRETCH_3X     — winner reaches ≥3.0×
  FALLEN_REVERSE — fallen leg reverses ≥+25% from trough and/or crosses back above entry
  BREAKOUT_2X    — winner reaches ≥2.0× (strong directional day; may or may not hit 3×)
  DECAY          — neither side ≥1.45× by 13:00 and pair MTM weak / both < entry late

Also reports earliest causal time each label becomes true, and early (≤60m) features.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tools_bt_final_daily_report as cw

ROOT = Path(__file__).resolve().parent
CHAIN = ROOT / "data" / "option_chain_1m"
LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
OUT = ROOT / "data" / "backtests" / "daytype_last5.json"

BAND_LO, BAND_HI = 70.0, 77.0


def _hm(t: str) -> int:
    try:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _leg(fr: dict, side: str, k: int) -> float | None:
    row = (fr.get("legs") or {}).get(f"{side}:{k}")
    if not row or row.get("c") is None:
        return None
    return float(row["c"])


def analyze_day(day: str) -> dict[str, Any]:
    viewer = json.loads((CHAIN / day / "viewer.json").read_text(encoding="utf-8"))
    frames = list(viewer.get("frames") or [])
    cw.BAND_LO, cw.BAND_HI = BAND_LO, BAND_HI
    cw.BAND_CENTER = (BAND_LO + BAND_HI) / 2
    merge = cw.find_merge_after(frames, after_t="09:20", before_t=None, exclude_pairs=set())
    if not merge:
        return {"day": day, "note": "no merge", "types": []}

    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    entry_m = _hm(entry_t)

    series: list[dict[str, Any]] = []
    peak_ce = peak_pe = ce0, pe0
    # fix tuple unpack
    peak_ce, peak_pe = ce0, pe0
    trough_ce, trough_pe = ce0, pe0
    last_ce, last_pe = ce0, pe0
    t_1_45 = t_2x = t_3x = None
    t_fallen_rev = None
    winner_final = None

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t or t > "15:25":
            continue
        ce = _leg(fr, "CE", ce_k)
        pe = _leg(fr, "PE", pe_k)
        if ce is not None:
            last_ce = ce
            peak_ce = max(peak_ce, ce)
            trough_ce = min(trough_ce, ce)
        if pe is not None:
            last_pe = pe
            peak_pe = max(peak_pe, pe)
            trough_pe = min(trough_pe, pe)
        if ce is None and pe is None:
            continue
        ce_m, pe_m = last_ce / ce0, last_pe / pe0
        lead = "CE" if ce_m >= pe_m else "PE"
        series.append(
            {
                "t": t,
                "ce": round(last_ce, 2),
                "pe": round(last_pe, 2),
                "ce_x": round(ce_m, 3),
                "pe_x": round(pe_m, 3),
                "lead": lead,
                "pair_mtm": round((last_ce - ce0) + (last_pe - pe0), 2),
            }
        )
        mx = max(ce_m, pe_m)
        if t_1_45 is None and mx >= 1.45:
            t_1_45 = t
            winner_final = "CE" if ce_m >= pe_m else "PE"
        if t_2x is None and mx >= 2.0:
            t_2x = t
        if t_3x is None and mx >= 3.0:
            t_3x = t

        # Fallen reverse: lagging side made new trough then recovered ≥25% from trough
        # and is rising while still below entry OR crossed back above entry after being ≤0.75×
        for side, px0, trough, now in (
            ("CE", ce0, trough_ce, last_ce),
            ("PE", pe0, trough_pe, last_pe),
        ):
            if trough <= 0 or trough >= px0 * 0.95:
                continue  # never really fallen
            bounce = (now - trough) / trough
            was_deep = trough <= px0 * 0.75
            if was_deep and bounce >= 0.25 and t_fallen_rev is None:
                # only count if this side was not the early leader at 1.45
                t_fallen_rev = t

    # Early window features (first 60 minutes after entry)
    early = [s for s in series if _hm(s["t"]) <= entry_m + 60]
    mid = [s for s in series if entry_m + 60 < _hm(s["t"]) <= entry_m + 120]
    late = [s for s in series if _hm(s["t"]) >= _hm("14:00")]

    def _feat(rows: list[dict]) -> dict[str, Any]:
        if not rows:
            return {}
        return {
            "max_ce_x": max(r["ce_x"] for r in rows),
            "max_pe_x": max(r["pe_x"] for r in rows),
            "min_ce_x": min(r["ce_x"] for r in rows),
            "min_pe_x": min(r["pe_x"] for r in rows),
            "max_lead_x": max(max(r["ce_x"], r["pe_x"]) for r in rows),
            "end_pair_mtm": rows[-1]["pair_mtm"],
            "lead_flips": sum(
                1 for i in range(1, len(rows)) if rows[i]["lead"] != rows[i - 1]["lead"]
            ),
        }

    early_f, mid_f, late_f = _feat(early), _feat(mid), _feat(late)
    peak_ce_x, peak_pe_x = peak_ce / ce0, peak_pe / pe0
    winner = "CE" if peak_ce_x >= peak_pe_x else "PE"
    fallen = "PE" if winner == "CE" else "CE"
    fallen_peak_x = peak_pe_x if fallen == "PE" else peak_ce_x
    fallen_trough_x = (trough_pe / pe0) if fallen == "PE" else (trough_ce / ce0)
    fallen_bounce = None
    if fallen == "PE" and trough_pe > 0:
        fallen_bounce = round((peak_pe - trough_pe) / trough_pe * 100, 1)
    elif fallen == "CE" and trough_ce > 0:
        fallen_bounce = round((peak_ce - trough_ce) / trough_ce * 100, 1)

    types: list[str] = []
    if t_3x:
        types.append("STRETCH_3X")
    if t_2x:
        types.append("BREAKOUT_2X")
    # fallen reverse: trough ≤0.75× and later bounce ≥25% of trough (on fallen side)
    if fallen_trough_x <= 0.75 and (fallen_bounce or 0) >= 25:
        types.append("FALLEN_REVERSE")
    # decay: never 1.45 by 13:00 OR both peaks < 1.35 and end pair weak
    hit_145_by_13 = t_1_45 is not None and t_1_45 <= "13:00"
    if (not hit_145_by_13) or (max(peak_ce_x, peak_pe_x) < 1.35 and (late_f.get("end_pair_mtm") or 0) < 0):
        if "STRETCH_3X" not in types and "BREAKOUT_2X" not in types:
            types.append("DECAY")
    if not types:
        if max(peak_ce_x, peak_pe_x) >= 1.45:
            types.append("NORMAL_1.45")
        else:
            types.append("DECAY")

    # Early identification rules (causal hypothesesscored on this day)
    tells: list[str] = []
    if early_f:
        if early_f["max_lead_x"] >= 1.35 and early_f["lead_flips"] <= 2:
            tells.append("EARLY_TREND: lead≥1.35× in 60m with few flips → watch BREAKOUT_2X/3X")
        if early_f["max_lead_x"] < 1.20 and early_f["lead_flips"] >= 4:
            tells.append("EARLY_CHOP: lead<1.20× + many flips → DECAY / lock 1.45 if hit")
        if min(early_f["min_ce_x"], early_f["min_pe_x"]) <= 0.75 and early_f["max_lead_x"] >= 1.25:
            tells.append("EARLY_SPLIT: one side ≤0.75× while other ≥1.25× → FALLEN_REVERSE possible later")
        if early_f["max_lead_x"] >= 1.60:
            tells.append("EARLY_STRETCH: already ≥1.60× by +60m → hold for 2×/3×, don't tight-trail")

    return {
        "day": day,
        "merge": {
            "t": entry_t,
            "ce_k": ce_k,
            "pe_k": pe_k,
            "ce0": ce0,
            "pe0": pe0,
            "atm": merge.get("atm"),
        },
        "types": types,
        "milestones": {
            "t_1.45": t_1_45,
            "t_2.0": t_2x,
            "t_3.0": t_3x,
            "t_fallen_rev_signal": t_fallen_rev,
        },
        "peaks": {
            "winner": winner,
            "fallen": fallen,
            "peak_ce_x": round(peak_ce_x, 3),
            "peak_pe_x": round(peak_pe_x, 3),
            "fallen_trough_x": round(fallen_trough_x, 3),
            "fallen_bounce_pct_from_trough": fallen_bounce,
            "end_ce_x": round(last_ce / ce0, 3),
            "end_pe_x": round(last_pe / pe0, 3),
        },
        "early_60m": early_f,
        "mid_60_120m": mid_f,
        "late_after_14": late_f,
        "early_tells": tells,
    }


def main() -> None:
    days = [d for d in LAST5 if (CHAIN / d / "viewer.json").exists()]
    reports = [analyze_day(d) for d in days]

    print("=" * 88)
    print("DAY-TYPE MAP (first 70–77 merge pair, walk-forward labels)")
    print("=" * 88)
    for r in reports:
        if r.get("note"):
            print(f"{r['day']}: {r['note']}")
            continue
        p = r["peaks"]
        m = r["milestones"]
        print(f"\n{r['day']}  types={r['types']}")
        print(
            f"  merge {r['merge']['t']}  CE{r['merge']['ce_k']}@{r['merge']['ce0']}  "
            f"PE{r['merge']['pe_k']}@{r['merge']['pe0']}"
        )
        print(
            f"  peaks  CE {p['peak_ce_x']}×  PE {p['peak_pe_x']}×  "
            f"winner={p['winner']} fallen={p['fallen']} "
            f"fallen_trough={p['fallen_trough_x']}× bounce={p['fallen_bounce_pct_from_trough']}%"
        )
        print(
            f"  times  1.45@{m['t_1.45']}  2.0@{m['t_2.0']}  3.0@{m['t_3.0']}  "
            f"fallen_rev@{m['t_fallen_rev_signal']}"
        )
        e = r["early_60m"]
        print(
            f"  early60m  maxLead={e.get('max_lead_x')}  "
            f"minCE={e.get('min_ce_x')} minPE={e.get('min_pe_x')}  flips={e.get('lead_flips')}"
        )
        for t in r["early_tells"]:
            print(f"  >> {t}")

    # How to identify — playbook
    print("\n" + "=" * 88)
    print("HOW TO IDENTIFY (causal — only use info up to now)")
    print("=" * 88)
    print(
        """
1) DECAY day
   • After entry+45–60m: lead still <1.20× AND lead flips ≥4
   • By 11:30 still no 1.45× → treat as decay: book any 1.35–1.45, no reentry
   • Do NOT trail for 2×/3×

2) BREAKOUT_2X / STRETCH_3X day
   • Early: lead ≥1.35–1.60× within 60m with few flips (clean one-way)
   • After 1.45 hit: if winner keeps making higher highs for 15–20m → hold
   • Trail wide (arm ~1.30, give 10–15% DD) or scale: book half @1.45, runner for 2×/3×
   • 3× is rare — only press when early stretch already ≥1.60× and index one-way

3) FALLEN_REVERSE day
   • Early SPLIT: one leg ≤0.75× while other ≥1.25×
   • Wait for fallen trough + bounce ≥20–25% from trough (hammer/rev-hammer break)
   • Reentry = add/flip only after fallen bounce confirms — never catch knife at first dump

4) Mixed week rhythm (your rule of thumb)
   • ~1 stretch/3× · ~2 fallen-reverse · ~1 clean 2× breakout · ~1 decay
   • After 2–3 quiet/decay sessions, raise odds of stretch — but never force entry
   • Once today's type is tagged from early tells, pick exit mode:
       DECAY → lockgreen 1.45
       BREAKOUT/STRETCH → trail / runner
       FALLEN_REVERSE → hold winner, watch fallen bounce for optional add
"""
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"days": reports}, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()

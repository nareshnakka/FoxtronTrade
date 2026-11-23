"""
Learn Algo1 (CW 70–77) exit / reentry rules — causal only (no look-ahead).

Sweeps:
  • target mult: 1.40 / 1.45 / 1.50 / 1.55
  • exit style: hit (book now) | retrace (wait pullback from peak) | trail
  • morning cut: flat by 11:00 if not armed / if losing
  • reentry: after win / always / after 11:00 only

Scores: green days, worst day, total net — on last 5 days + all days.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tools_bt_final_daily_report as cw
from foxtrade.charges import estimate_option_charges
from foxtrade.config_store import load_config

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "backtests"
LAST5 = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]

BAND = (70.0, 77.0)
CAPITAL = 100_000.0
DEPLOY = 0.90
MERGE_AFTER = "09:20"
SESSION_END = "15:25"
SECOND_AFTER = "11:00"
LAST_ENTRY = "13:00"
MAX_SETUPS = 3
COOLDOWN_MIN = 1


@dataclass(frozen=True)
class Policy:
    name: str
    win_mult: float
    # hit = book both at first touch; retrace = after arm, wait pullback; trail = arm earlier then trail
    mode: str  # hit | retrace | trail
    retrace_pct: float = 0.0  # % off peak of winner before exit (retrace/trail)
    arm_mult: float = 0.0  # trail arm level (default win_mult*0.9 if 0)
    cut_by: str | None = None  # e.g. "11:00" — if still open and not armed, flat
    cut_if_losing: bool = True  # only cut_by when pair MTM < 0
    max_setups: int = 2
    reentry_after: str = "11:00"  # earliest hunt for setup #2+
    last_entry: str = "13:00"
    skip_2nd_if_net_ge: float = 15_000.0
    # After a loss, block reentry until this time (None = normal)
    loss_reentry_after: str | None = "11:00"


def _hm(hm: str) -> int:
    try:
        h, m = hm.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _leg(fr: dict, side: str, k: int) -> float | None:
    row = (fr.get("legs") or {}).get(f"{side}:{k}")
    if not row or row.get("c") is None:
        return None
    return float(row["c"])


def _charges(entry: float, exit_px: float, qty: int, cfg: dict) -> float:
    try:
        return float(
            estimate_option_charges(entry, exit_px, qty, cfg=cfg).get("charges") or 0
        )
    except Exception:
        return 0.0


def _close_pair(
    *,
    t: str,
    ce0: float,
    pe0: float,
    ce: float,
    pe: float,
    lots: int,
    lot_size: int,
    cfg: dict,
    why: str,
    win_side: str | None,
) -> dict[str, Any]:
    qty = lots * lot_size
    ce_pnl = (ce - ce0) * qty
    pe_pnl = (pe - pe0) * qty
    ce_ch = _charges(ce0, ce, qty, cfg)
    pe_ch = _charges(pe0, pe, qty, cfg)
    gross = ce_pnl + pe_pnl
    charges = ce_ch + pe_ch
    return {
        "entry_t": None,  # filled by caller
        "exit_t": t,
        "exit_why": why,
        "ce_entry": ce0,
        "pe_entry": pe0,
        "ce_exit": round(ce, 2),
        "pe_exit": round(pe, 2),
        "ce_mult": round(ce / ce0, 3) if ce0 else None,
        "pe_mult": round(pe / pe0, 3) if pe0 else None,
        "win_side": win_side or "EOD",
        "lots": lots,
        "gross": round(gross, 2),
        "charges": round(charges, 2),
        "net": round(gross - charges, 2),
    }


def run_setup(
    frames: list[dict],
    merge: dict[str, Any],
    *,
    policy: Policy,
    lots: int,
    lot_size: int,
    cfg: dict,
    setup_id: int,
) -> dict[str, Any]:
    ce_k, pe_k = int(merge["ce_k"]), int(merge["pe_k"])
    ce0, pe0 = float(merge["ce0"]), float(merge["pe0"])
    entry_t = str(merge["t"])
    last_ce, last_pe = ce0, pe0
    peak_ce, peak_pe = ce0, pe0
    armed = False
    arm_side: str | None = None
    win_mult = float(policy.win_mult)
    arm_mult = float(policy.arm_mult) if policy.arm_mult > 0 else (
        win_mult * 0.92 if policy.mode == "trail" else win_mult
    )
    retrace = float(policy.retrace_pct) / 100.0

    for fr in frames:
        t = fr.get("t") or ""
        if t < entry_t:
            continue
        if t > SESSION_END:
            break
        ce_now = _leg(fr, "CE", ce_k)
        pe_now = _leg(fr, "PE", pe_k)
        if ce_now is not None:
            last_ce = ce_now
            peak_ce = max(peak_ce, ce_now)
        if pe_now is not None:
            last_pe = pe_now
            peak_pe = max(peak_pe, pe_now)
        if ce_now is None and pe_now is None:
            continue
        ce, pe = last_ce, last_pe
        ce_m, pe_m = ce / ce0, pe / pe0
        pair_mtm = (ce - ce0) + (pe - pe0)

        # Morning / time cut (causal)
        if policy.cut_by and t >= policy.cut_by and not armed:
            losing = pair_mtm < 0
            if (not policy.cut_if_losing) or losing:
                why = f"time cut @{policy.cut_by} mtm={pair_mtm:.1f} armed=0"
                out = _close_pair(
                    t=t,
                    ce0=ce0,
                    pe0=pe0,
                    ce=ce,
                    pe=pe,
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    why=why,
                    win_side=None,
                )
                out.update(
                    {
                        "setup_id": setup_id,
                        "entry_t": entry_t,
                        "ce_strike": ce_k,
                        "pe_strike": pe_k,
                        "atm": merge.get("atm"),
                        "mid": round(float(merge["mid"]), 2),
                        "gap": round(float(merge["gap"]), 2),
                    }
                )
                return out

        # Arm on target / trail arm
        if not armed:
            if ce_m >= arm_mult or pe_m >= arm_mult:
                armed = True
                arm_side = "CE" if ce_m >= pe_m else "PE"
                if policy.mode == "hit" and (
                    ce_m >= win_mult or pe_m >= win_mult
                ):
                    # book immediately at first win_mult touch
                    win = "CE" if ce_m >= pe_m else "PE"
                    why = f"hit {win} ≥{win_mult:g}×"
                    out = _close_pair(
                        t=t,
                        ce0=ce0,
                        pe0=pe0,
                        ce=ce,
                        pe=pe,
                        lots=lots,
                        lot_size=lot_size,
                        cfg=cfg,
                        why=why,
                        win_side=win,
                    )
                    out.update(
                        {
                            "setup_id": setup_id,
                            "entry_t": entry_t,
                            "ce_strike": ce_k,
                            "pe_strike": pe_k,
                            "atm": merge.get("atm"),
                            "mid": round(float(merge["mid"]), 2),
                            "gap": round(float(merge["gap"]), 2),
                        }
                    )
                    return out
            continue

        # Armed: hit mode already returned; retrace/trail wait for pullback
        if policy.mode in ("retrace", "trail"):
            # Prefer the leading side
            if arm_side == "CE":
                peak, now, base = peak_ce, ce, ce0
                other = pe
            else:
                peak, now, base = peak_pe, pe, pe0
                other = ce
            # Also switch arm if other side overtakes
            if pe / pe0 > ce / ce0 and pe / pe0 >= arm_mult:
                arm_side = "PE"
                peak, now, base = peak_pe, pe, pe0
            elif ce / ce0 >= pe / pe0 and ce / ce0 >= arm_mult:
                arm_side = "CE"
                peak, now, base = peak_ce, ce, ce0

            # Need at least win_mult peak for retrace mode before allowing exit
            if policy.mode == "retrace" and peak / base < win_mult:
                continue
            if peak <= 0:
                continue
            dd = (peak - now) / peak
            # Exit on retrace OR if winner falls back under arm_mult after being above win_mult
            if dd >= retrace or (now / base < arm_mult and peak / base >= win_mult):
                why = (
                    f"{policy.mode} {arm_side} peak={peak:.1f} now={now:.1f} "
                    f"dd={dd*100:.1f}% target={win_mult:g}×"
                )
                out = _close_pair(
                    t=t,
                    ce0=ce0,
                    pe0=pe0,
                    ce=ce,
                    pe=pe,
                    lots=lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    why=why,
                    win_side=arm_side,
                )
                out.update(
                    {
                        "setup_id": setup_id,
                        "entry_t": entry_t,
                        "ce_strike": ce_k,
                        "pe_strike": pe_k,
                        "atm": merge.get("atm"),
                        "mid": round(float(merge["mid"]), 2),
                        "gap": round(float(merge["gap"]), 2),
                    }
                )
                return out

    # EOD
    out = _close_pair(
        t=SESSION_END,
        ce0=ce0,
        pe0=pe0,
        ce=last_ce,
        pe=last_pe,
        lots=lots,
        lot_size=lot_size,
        cfg=cfg,
        why=f"EOD armed={int(armed)}",
        win_side=arm_side,
    )
    # use last available bar time if possible
    for fr in reversed(frames):
        t = fr.get("t") or ""
        if entry_t <= t <= SESSION_END:
            out["exit_t"] = t
            break
    out.update(
        {
            "setup_id": setup_id,
            "entry_t": entry_t,
            "ce_strike": ce_k,
            "pe_strike": pe_k,
            "atm": merge.get("atm"),
            "mid": round(float(merge["mid"]), 2),
            "gap": round(float(merge["gap"]), 2),
        }
    )
    return out


def simulate_day(
    day: str,
    frames: list[dict],
    *,
    policy: Policy,
    lot_size: int,
    cfg: dict,
) -> dict[str, Any]:
    setups: list[dict] = []
    used: set[tuple[int, int]] = set()
    hunt = MERGE_AFTER
    sid = 0
    day_net = 0.0
    last_was_loss = False

    # Patch band on module for find_merge_after
    old = (cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.MERGE_AFTER, cw.SESSION_END)
    cw.BAND_LO, cw.BAND_HI = BAND
    cw.BAND_CENTER = (BAND[0] + BAND[1]) / 2.0
    cw.MERGE_AFTER = MERGE_AFTER
    cw.SESSION_END = SESSION_END
    try:
        while sid < policy.max_setups:
            before = None if sid == 0 else policy.last_entry
            if sid >= 1:
                if day_net >= policy.skip_2nd_if_net_ge:
                    break
                hunt = max(hunt, policy.reentry_after)
                if last_was_loss and policy.loss_reentry_after:
                    hunt = max(hunt, policy.loss_reentry_after)
            merge = cw.find_merge_after(
                frames, after_t=hunt, before_t=before, exclude_pairs=used
            )
            if not merge:
                break
            sid += 1
            lots = max(
                1,
                int((CAPITAL * DEPLOY) // ((merge["ce0"] + merge["pe0"]) * lot_size)),
            )
            res = run_setup(
                frames,
                merge,
                policy=policy,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                setup_id=sid,
            )
            setups.append(res)
            day_net = round(day_net + float(res["net"]), 2)
            last_was_loss = float(res["net"]) < 0
            used.add((int(merge["ce_k"]), int(merge["pe_k"])))
            used.add((int(merge["pe_k"]), int(merge["ce_k"])))
            exit_m = _hm(str(res.get("exit_t") or merge["t"])) + COOLDOWN_MIN
            hunt_m = max(exit_m, _hm(policy.reentry_after if sid >= 1 else MERGE_AFTER))
            h, m = divmod(hunt_m, 60)
            hunt = f"{h:02d}:{m:02d}"
            if hunt >= SESSION_END or hunt >= policy.last_entry:
                break
    finally:
        cw.BAND_LO, cw.BAND_HI, cw.BAND_CENTER, cw.MERGE_AFTER, cw.SESSION_END = old

    return {
        "day": day,
        "setups": setups,
        "setup_count": len(setups),
        "gross": round(sum(float(s["gross"]) for s in setups), 2),
        "charges": round(sum(float(s["charges"]) for s in setups), 2),
        "net": round(sum(float(s["net"]) for s in setups), 2),
        "note": None if setups else "no merge",
    }


def score(daily: list[dict]) -> dict[str, Any]:
    nets = [float(d.get("net") or 0) for d in daily]
    traded = [d for d in daily if d.get("setup_count")]
    green = sum(1 for n in nets if n > 0)
    red = sum(1 for n in nets if n < 0)
    flat = sum(1 for n in nets if n == 0)
    return {
        "days": len(daily),
        "green_days": green,
        "red_days": red,
        "flat_days": flat,
        "all_green_traded": all(float(d["net"]) > 0 for d in traded) if traded else False,
        "net": round(sum(nets), 2),
        "worst_day": round(min(nets) if nets else 0, 2),
        "best_day": round(max(nets) if nets else 0, 2),
        "avg_day": round(sum(nets) / len(nets), 2) if nets else 0,
    }


def policies() -> list[Policy]:
    out: list[Policy] = []
    # Baselines
    out.append(Policy("baseline_1.45_hit", 1.45, "hit", max_setups=2))
    out.append(Policy("hit_1.50", 1.50, "hit", max_setups=2))
    out.append(Policy("hit_1.40", 1.40, "hit", max_setups=2))
    out.append(Policy("hit_1.55", 1.55, "hit", max_setups=2))

    # Retrace after 1.5×
    for dd in (5, 8, 10, 12, 15):
        out.append(
            Policy(f"retrace_1.50_dd{dd}", 1.50, "retrace", retrace_pct=float(dd), max_setups=2)
        )
        out.append(
            Policy(f"retrace_1.45_dd{dd}", 1.45, "retrace", retrace_pct=float(dd), max_setups=2)
        )

    # Trail: arm 1.30, trail 8–12%
    for arm, dd in ((1.30, 8), (1.30, 10), (1.35, 8), (1.35, 12)):
        out.append(
            Policy(
                f"trail_arm{arm:g}_dd{dd}",
                1.50,
                "trail",
                retrace_pct=float(dd),
                arm_mult=arm,
                max_setups=2,
            )
        )

    # Time cut @ 11:00 if losing / always
    for mult in (1.45, 1.50):
        out.append(
            Policy(
                f"hit_{mult:g}_cut11_lose",
                mult,
                "hit",
                cut_by="11:00",
                cut_if_losing=True,
                max_setups=2,
            )
        )
        out.append(
            Policy(
                f"hit_{mult:g}_cut11_always",
                mult,
                "hit",
                cut_by="11:00",
                cut_if_losing=False,
                max_setups=2,
            )
        )

    # Retrace + cut11 lose
    out.append(
        Policy(
            "retrace_1.50_dd10_cut11_lose",
            1.50,
            "retrace",
            retrace_pct=10,
            cut_by="11:00",
            cut_if_losing=True,
            max_setups=2,
        )
    )
    out.append(
        Policy(
            "retrace_1.50_dd8_cut11_lose",
            1.50,
            "retrace",
            retrace_pct=8,
            cut_by="11:00",
            cut_if_losing=True,
            max_setups=2,
        )
    )

    # More setups / reentry learning
    out.append(
        Policy(
            "hit_1.50_max3_re11",
            1.50,
            "hit",
            max_setups=3,
            reentry_after="11:00",
            skip_2nd_if_net_ge=20_000,
        )
    )
    out.append(
        Policy(
            "retrace_1.50_dd10_max3",
            1.50,
            "retrace",
            retrace_pct=10,
            max_setups=3,
            reentry_after="11:00",
            cut_by="11:00",
            cut_if_losing=True,
            skip_2nd_if_net_ge=20_000,
        )
    )
    out.append(
        Policy(
            "hit_1.50_cut11_lose_max3_lossblock",
            1.50,
            "hit",
            cut_by="11:00",
            cut_if_losing=True,
            max_setups=3,
            loss_reentry_after="11:00",
            reentry_after="11:00",
        )
    )
    # Force flat noon if not armed
    out.append(
        Policy(
            "hit_1.50_cut12_lose",
            1.50,
            "hit",
            cut_by="12:00",
            cut_if_losing=True,
            max_setups=2,
        )
    )
    return out


def run_universe(
    days: list[str],
    day_frames: dict[str, list[dict]],
    *,
    policy: Policy,
    lot_size: int,
    cfg: dict,
) -> tuple[list[dict], dict]:
    daily = [
        simulate_day(d, day_frames[d], policy=policy, lot_size=lot_size, cfg=cfg)
        for d in days
    ]
    return daily, score(daily)


def main() -> None:
    cfg = load_config()
    lot = int(cfg.get("lot_size") or 75)
    all_days = cw.available_days()
    day_frames: dict[str, list[dict]] = {}
    for d in all_days:
        viewer = json.loads((cw.CHAIN / d / "viewer.json").read_text(encoding="utf-8"))
        day_frames[d] = list(viewer.get("frames") or [])

    last5 = [d for d in LAST5 if d in day_frames]
    pols = policies()
    rows: list[dict] = []

    print(f"Days all={len(all_days)} last5={last5}")
    print(f"Sweeping {len(pols)} policies (causal)...\n")

    for p in pols:
        d5, s5 = run_universe(last5, day_frames, policy=p, lot_size=lot, cfg=cfg)
        da, sa = run_universe(all_days, day_frames, policy=p, lot_size=lot, cfg=cfg)
        row = {
            "name": p.name,
            "policy": asdict(p),
            "last5": s5,
            "all": sa,
            "last5_daily": [
                {"day": x["day"], "net": x["net"], "setups": x["setup_count"]} for x in d5
            ],
        }
        rows.append(row)

    # Rank: prioritize last5 green days, then worst_day, then net, then all green
    def key(r: dict) -> tuple:
        s = r["last5"]
        a = r["all"]
        return (
            s["green_days"],
            1 if s["all_green_traded"] else 0,
            s["worst_day"],
            s["net"],
            a["green_days"],
            a["net"],
        )

    rows.sort(key=key, reverse=True)

    print("=" * 88)
    print("TOP policies on LAST 5 days (green days → worst day → net)")
    print("=" * 88)
    for r in rows[:12]:
        s = r["last5"]
        a = r["all"]
        print(
            f"{r['name']:<40}  L5 {s['green_days']}G/{s['red_days']}R "
            f"net₹{s['net']:>9,.0f}  worst₹{s['worst_day']:>8,.0f}  |  "
            f"ALL {a['green_days']}G/{a['red_days']}R net₹{a['net']:>9,.0f}"
        )
        print("   " + "  ".join(f"{x['day'][5:]}:{x['net']:,.0f}" for x in r["last5_daily"]))

    best = rows[0]
    print("\n" + "=" * 88)
    print(f"BEST: {best['name']}")
    print(f"Policy: {json.dumps(best['policy'], indent=2)}")
    print("Last5 daily:", best["last5_daily"])
    print("Last5 score:", best["last5"])
    print("All score:", best["all"])

    # Detail best on last5
    p = Policy(**{k: best["policy"][k] for k in Policy.__dataclass_fields__})
    d5, _ = run_universe(last5, day_frames, policy=p, lot_size=lot, cfg=cfg)
    print("\n--- Best policy trades (last 5) ---")
    for day in d5:
        print(f"\n{day['day']}  setups={day['setup_count']}  NET ₹{day['net']:,.2f}")
        for s in day.get("setups") or []:
            print(
                f"  #{s['setup_id']} {s['entry_t']}→{s['exit_t']}  "
                f"CE{s['ce_strike']} {s['ce_entry']}→{s['ce_exit']} ({s['ce_mult']}×)  "
                f"PE{s['pe_strike']} {s['pe_entry']}→{s['pe_exit']} ({s['pe_mult']}×)  "
                f"NET ₹{s['net']:,.2f}"
            )
            print(f"    {s.get('exit_why')}")

    # Lessons from baseline losers
    print("\n" + "=" * 88)
    print("LESSONS from baseline 1.45 hit losers (causal peaks before loss)")
    print("=" * 88)
    base = Policy("baseline_1.45_hit", 1.45, "hit", max_setups=2)
    for d in last5:
        day = simulate_day(d, day_frames[d], policy=base, lot_size=lot, cfg=cfg)
        for s in day.get("setups") or []:
            if float(s["net"]) >= 0:
                continue
            # Walk peaks causally for lesson
            ce_k, pe_k = int(s["ce_strike"]), int(s["pe_strike"])
            ce0, pe0 = float(s["ce_entry"]), float(s["pe_entry"])
            peak_ce = peak_pe = 0.0
            peak_t = s["entry_t"]
            for fr in day_frames[d]:
                t = fr.get("t") or ""
                if t < s["entry_t"] or t > s["exit_t"]:
                    continue
                ce = _leg(fr, "CE", ce_k)
                pe = _leg(fr, "PE", pe_k)
                if ce is not None and ce > peak_ce:
                    peak_ce, peak_t = ce, t
                if pe is not None and pe > peak_pe:
                    peak_pe, peak_t = pe, t
            print(
                f"  {d} #{s['setup_id']} entry {s['entry_t']} exit {s['exit_t']} "
                f"net₹{s['net']:,.0f}  peak CE {peak_ce/ce0:.2f}× PE {peak_pe/pe0:.2f}× "
                f"@~{peak_t}  → never booked 1.45×; EOD/drag killed pair"
            )

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / "algo1_exit_learn.json"
    out_path.write_text(
        json.dumps(
            {
                "last5_days": last5,
                "all_days": all_days,
                "ranking": [
                    {
                        "name": r["name"],
                        "last5": r["last5"],
                        "all": r["all"],
                        "last5_daily": r["last5_daily"],
                        "policy": r["policy"],
                    }
                    for r in rows
                ],
                "best": best,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()

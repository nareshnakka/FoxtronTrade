"""
Expiry-day ATM gamma / decay autopsy using backtest synth premiums.
Finds decay floor → blast clocks and fast-exit rules for Tuesday expiry.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median

from backtest_detailed import _day_prem_opens, _load_bars
from backtest_month_ledger import _load_month_days

OUT = Path("data/backtests/expiry_gamma_autopsy.json")
DELTA = 0.50
THETA = 0.015  # per minute — matches backtest
ENTRY_PREM = 135.0


def _mins(i: int) -> int:
    return i  # bars are 1-min from 09:15


def _hhmm(i: int) -> str:
    total = 9 * 60 + 15 + i
    return f"{total // 60:02d}:{total % 60:02d}"


def build_path(bars: list[dict], ce0: float, pe0: float) -> list[dict]:
    spot0 = float(bars[0]["close"])
    path = []
    for i, b in enumerate(bars):
        spot = float(b["close"])
        ds = spot - spot0
        ce = max(5.0, ce0 + DELTA * ds - THETA * i)
        pe = max(5.0, pe0 - DELTA * ds - THETA * i)
        path.append(
            {
                "i": i,
                "t": _hhmm(i),
                "mins": i,
                "spot": spot,
                "ce": ce,
                "pe": pe,
                "straddle": ce + pe,
            }
        )
    return path


def analyze_day(day: str, bars: list[dict], ce0: float, pe0: float) -> dict | None:
    if len(bars) < 120:
        return None
    path = build_path(bars, ce0, pe0)
    spot0 = path[0]["spot"]
    open_px = float(bars[0].get("open") or spot0)

    # --- Decay phase: straddle falling while |spot| quiet ---
    # Morning window 09:15-11:30 (0-135m)
    morn = path[:136]
    # Rolling 10m spot range
    quiet_end = None
    for i in range(30, len(morn)):
        w = [p["spot"] for p in path[i - 10 : i + 1]]
        if max(w) - min(w) <= 25:  # quiet 10m
            quiet_end = i
    # Straddle low in morning (before noon)
    morn_lo_i = min(range(len(morn)), key=lambda i: morn[i]["straddle"])
    morn_lo = morn[morn_lo_i]

    # --- OR (30m) ---
    or_hi = max(p["spot"] for p in path[:31])
    or_lo = min(p["spot"] for p in path[:31])

    # --- Trend reversal / gamma trigger: first decisive OR break after 10:00 ---
    # with acceleration (10m mom >= 25)
    trigger = None
    for p in path:
        if p["mins"] < 45:
            continue
        if p["mins"] > 240:  # after 13:15 skip new triggers for weekly pin
            break
        look = min(10, p["i"])
        mom = p["spot"] - path[p["i"] - look]["spot"]
        if p["spot"] > or_hi + 12 and mom >= 20:
            trigger = {
                "t": p["t"],
                "mins": p["mins"],
                "side": "CE",
                "spot": p["spot"],
                "mom10": round(mom, 1),
                "or_hi": round(or_hi, 1),
                "ce": round(p["ce"], 1),
                "pe": round(p["pe"], 1),
            }
            break
        if p["spot"] < or_lo - 12 and mom <= -20:
            trigger = {
                "t": p["t"],
                "mins": p["mins"],
                "side": "PE",
                "spot": p["spot"],
                "mom10": round(mom, 1),
                "or_lo": round(or_lo, 1),
                "ce": round(p["ce"], 1),
                "pe": round(p["pe"], 1),
            }
            break

    # --- Blast: max 15m winner-side premium expansion after 10:00 ---
    blast = None
    best = -1.0
    for i in range(45, len(path) - 15):
        p0 = path[i]
        p1 = path[i + 15]
        for side in ("CE", "PE"):
            a = p0[side.lower()]
            b = p1[side.lower()]
            exp = (b - a) / max(a, 1) * 100
            if exp > best:
                best = exp
                blast = {
                    "start_t": p0["t"],
                    "start_mins": p0["mins"],
                    "end_t": p1["t"],
                    "side": side,
                    "exp_pct": round(exp, 1),
                    "px_from": round(a, 1),
                    "px_to": round(b, 1),
                    "spot_move": round(p1["spot"] - p0["spot"], 1),
                }

    # --- Simulated trades ---
    def sim_trade(entry_i: int, side: str, max_hold: int = 25) -> dict | None:
        if entry_i >= len(path) - 3:
            return None
        entry = path[entry_i][side.lower()]
        peak = entry
        peak_i = entry_i
        exit_i = entry_i
        exit_px = entry
        exit_why = "time"
        for j in range(entry_i + 1, min(entry_i + max_hold + 1, len(path))):
            px = path[j][side.lower()]
            if px > peak:
                peak = px
                peak_i = j
            ret = (px - entry) / entry * 100
            # fast target
            if ret >= 10:
                exit_i, exit_px, exit_why = j, px, "target10"
                break
            # trail: lock after +5, give back 3pts of premium or 40% of gain
            if peak >= entry * 1.05:
                give = peak - px
                if give >= max(3.0, (peak - entry) * 0.4):
                    exit_i, exit_px, exit_why = j, px, "trail"
                    break
            # stop
            if ret <= -5:
                exit_i, exit_px, exit_why = j, px, "stop"
                break
        else:
            exit_i = min(entry_i + max_hold, len(path) - 1)
            exit_px = path[exit_i][side.lower()]
            exit_why = "time25"
        return {
            "side": side,
            "entry_t": path[entry_i]["t"],
            "exit_t": path[exit_i]["t"],
            "hold_m": exit_i - entry_i,
            "entry_px": round(entry, 1),
            "exit_px": round(exit_px, 1),
            "peak_px": round(peak, 1),
            "ret_pct": round((exit_px - entry) / entry * 100, 1),
            "why": exit_why,
        }

    # A) Naive early ATM CE at 09:30
    early_ce = sim_trade(15, "CE")
    early_pe = sim_trade(15, "PE")
    # B) Wait decay to morn low then buy both? pick direction from next 10m spot
    wait_i = max(morn_lo_i, 45)
    if wait_i + 10 < len(path):
        nxt = path[wait_i + 10]["spot"] - path[wait_i]["spot"]
        wait_side = "CE" if nxt >= 0 else "PE"
    else:
        wait_side = "CE" if path[-1]["spot"] >= spot0 else "PE"
    wait_trade = sim_trade(wait_i, wait_side)
    # C) OR-break trigger
    trig_trade = None
    if trigger:
        trig_trade = sim_trade(trigger["mins"], trigger["side"])
    # D) Blast start (hindsight — benchmark only)
    blast_trade = None
    if blast and blast["exp_pct"] >= 8:
        blast_trade = sim_trade(blast["start_mins"], blast["side"])

    # E) Causal gamma: after 11:00, first 5m side premium +≥4% with |mom10|≥25
    gamma_trade = None
    gamma_trig = None
    for i in range(105, min(210, len(path) - 5)):  # 11:00–12:45
        p0 = path[i]
        p5 = path[i + 5]
        mom = p5["spot"] - path[max(0, i - 5)]["spot"]
        ce_exp = (p5["ce"] - p0["ce"]) / max(p0["ce"], 1) * 100
        pe_exp = (p5["pe"] - p0["pe"]) / max(p0["pe"], 1) * 100
        if ce_exp >= 4 and mom >= 25:
            gamma_trig = {"t": p5["t"], "mins": p5["mins"], "side": "CE", "exp5": round(ce_exp, 1)}
            gamma_trade = sim_trade(p5["mins"], "CE", max_hold=20)
            break
        if pe_exp >= 4 and mom <= -25:
            gamma_trig = {"t": p5["t"], "mins": p5["mins"], "side": "PE", "exp5": round(pe_exp, 1)}
            gamma_trade = sim_trade(p5["mins"], "PE", max_hold=20)
            break

    return {
        "date": day,
        "spot_chg": round(path[-1]["spot"] - open_px, 1),
        "or_hi": round(or_hi, 1),
        "or_lo": round(or_lo, 1),
        "morn_straddle_low_t": morn_lo["t"],
        "morn_straddle_low_mins": morn_lo["mins"],
        "morn_decay_pct": round(
            (1 - morn_lo["straddle"] / max(path[0]["straddle"], 1)) * 100, 1
        ),
        "quiet_end_t": _hhmm(quiet_end) if quiet_end else None,
        "trigger": trigger,
        "blast": blast,
        "trades": {
            "early_ce_0930": early_ce,
            "early_pe_0930": early_pe,
            "wait_floor_then_dir": wait_trade,
            "or_break_trigger": trig_trade,
            "blast_start": blast_trade,
            "causal_gamma_after_1100": gamma_trade,
        },
        "gamma_trig": gamma_trig,
    }


def _summ(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t["ret_pct"] for t in trades]
    wins = [t for t in trades if t["ret_pct"] > 0]
    return {
        "n": len(trades),
        "win": len(wins),
        "lose": len(trades) - len(wins),
        "avg_ret": round(mean(rets), 1),
        "med_ret": round(median(rets), 1),
        "med_hold": round(median([t["hold_m"] for t in trades]), 0),
        "med_entry_mins": round(median([_mins_from_t(t["entry_t"]) for t in trades]), 0),
        "exit_why": dict(
            sorted(
                {
                    w: sum(1 for t in trades if t["why"] == w)
                    for w in {t["why"] for t in trades}
                }.items(),
                key=lambda x: -x[1],
            )
        ),
    }


def _mins_from_t(t: str) -> int:
    h, m = map(int, t.split(":"))
    return (h - 9) * 60 + (m - 15)


def main() -> int:
    days = _load_month_days(
        as_of=date(2026, 11, 2), calendar_days=220, all_available=False
    )
    expiry_days = [d for d in days if d.get("is_expiry_day")]
    rows = []
    for d in expiry_days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        ce0 = float(ce0 or ENTRY_PREM)
        pe0 = float(pe0 or ENTRY_PREM)
        row = analyze_day(d["date"], bars, ce0, pe0)
        if row:
            rows.append(row)

    styles = [
        "early_ce_0930",
        "early_pe_0930",
        "wait_floor_then_dir",
        "or_break_trigger",
        "blast_start",
        "causal_gamma_after_1100",
    ]
    style_stats = {}
    for s in styles:
        ts = [r["trades"][s] for r in rows if r["trades"].get(s)]
        style_stats[s] = _summ(ts)

    trig_mins = [
        r["trigger"]["mins"] for r in rows if r.get("trigger")
    ]
    blast_mins = [
        r["blast"]["start_mins"]
        for r in rows
        if r.get("blast") and r["blast"]["exp_pct"] >= 8
    ]
    floor_mins = [r["morn_straddle_low_mins"] for r in rows]

    # Clock buckets for OR-break wins
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        t = r["trades"].get("or_break_trigger")
        if not t:
            continue
        m = _mins_from_t(t["entry_t"])
        if m < 75:
            buckets["10:00-10:30"].append(t["ret_pct"])
        elif m < 120:
            buckets["10:30-11:15"].append(t["ret_pct"])
        elif m < 180:
            buckets["11:15-12:15"].append(t["ret_pct"])
        else:
            buckets["12:15-13:15"].append(t["ret_pct"])

    gamma_stats = style_stats.get("causal_gamma_after_1100", {})
    floor_stats = style_stats.get("wait_floor_then_dir", {})
    playbook = {
        "do_not": [
            "Buy ATM in first 90-120 minutes on expiry — pure theta (early CE ~coin flip)",
            "Trust first OR break 10:00-10:30 on expiry — mostly fake (avg negative)",
            "New ATM entries after 13:00 (pin / gamma chop)",
        ],
        "wait_for": [
            f"Straddle/ATM decay into morning floor (median ~{_hhmm(int(median(floor_mins)))})",
            "THEN either: direction from post-floor impulse, OR causal gamma (5m premium +≥4% + |mom|≥25)",
            "Window 11:00–12:45 is the money zone for expiry ATM",
        ],
        "enter": {
            "best_style": "wait_floor_then_dir",
            "alt_style": "causal_gamma_after_1100",
            "side": "ATM of impulse direction after decay floor",
            "confirm_bars": 3,
            "best_clock": "11:00-12:30",
            "premium_band": "125-145 ATM",
            "floor_stats": floor_stats,
            "gamma_stats": gamma_stats,
        },
        "exit_fast": {
            "target": "+10% premium",
            "trail": "after +5%, give back 40% of gain or ₹3",
            "stop": "-5%",
            "time_stop": "20-25 minutes max",
            "median_hold_m": floor_stats.get("med_hold") or gamma_stats.get("med_hold"),
        },
        "median_trigger_mins": round(median(trig_mins), 0) if trig_mins else None,
        "median_blast_mins": round(median(blast_mins), 0) if blast_mins else None,
        "or_break_by_clock": {
            k: {
                "n": len(v),
                "avg_ret": round(mean(v), 1),
                "win": sum(1 for x in v if x > 0),
            }
            for k, v in sorted(buckets.items())
        },
    }

    out = {
        "expiry_days": len(rows),
        "style_stats": style_stats,
        "playbook": playbook,
        "days": rows,
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"Expiry Tuesdays: {len(rows)}")
    print("\n=== STYLE COMPARISON (ATM synth, fast exits) ===")
    for s, st in style_stats.items():
        if st.get("n"):
            print(
                f"  {s:22s}  n={st['n']:2d}  W/L {st['win']}/{st['lose']}  "
                f"avg {st['avg_ret']:+.1f}%  med {st['med_ret']:+.1f}%  "
                f"hold {st['med_hold']:.0f}m  entry@{_hhmm(int(st['med_entry_mins']))}  "
                f"exits {st['exit_why']}"
            )
    print("\n=== PLAYBOOK ===")
    print("Median OR-break trigger:", playbook["median_trigger_mins"], "mins from open")
    print("OR-break by clock:", playbook["or_break_by_clock"])
    print("Exit:", playbook["exit_fast"])
    print("\nRecent expiries (OR-break):")
    for r in rows[-10:]:
        t = r["trades"].get("or_break_trigger")
        tr = r.get("trigger") or {}
        if t:
            print(
                f"  {r['date']} chg={r['spot_chg']:+.0f} "
                f"trig {tr.get('t')} {tr.get('side')} → "
                f"{t['ret_pct']:+.1f}% {t['why']} hold {t['hold_m']}m"
            )
        else:
            print(f"  {r['date']} chg={r['spot_chg']:+.0f} NO OR-break trigger")
    print(f"\nJSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

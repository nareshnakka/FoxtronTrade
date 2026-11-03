"""
Autopsy flat + loss days from latest year report; propose profit learnings.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

REPORT = Path("data/backtests/week_2025-11-03_to_2026-11-02_algo_report.json")
OUT = Path("data/backtests/profit_learn_autopsy.json")


def reason_bucket(text: str) -> str:
    t = (text or "").lower()
    rules = [
        ("chop", "chop / range-bound"),
        ("warming", "warming up"),
        ("no new entries", "after cutoff"),
        ("no directional", "no edge"),
        ("open buffer", "open buffer"),
        ("wait prefer", "prefer_after"),
        ("wait structure", "wait structure"),
        ("orb chase", "ORB chase w/o retest"),
        ("orb already failed", "ORB failed"),
        ("mid-chase", "CE mid-chase"),
        ("extended", "CE extended / wait SR"),
        ("path efficiency", "path efficiency"),
        ("range-bound", "range-bound gate"),
        ("sharp-fall", "sharp-fall CE block"),
        ("fail-rally", "fail-rally CE block"),
        ("late ce sr", "late SR in range"),
        ("snapback", "PE snapback"),
        ("spent dump", "PE spent dump"),
        ("day low", "PE not at day low"),
        ("day high", "CE not at day high"),
        ("falling mom", "CE breakout falling mom"),
        ("needs mom", "mom gate"),
        ("vs open", "vs_open floor"),
        ("spot ≤", "vs_open floor"),
        ("spot ≥", "vs_open floor"),
        ("post-decay", "post-decay wait/block"),
        ("expiry", "expiry gate"),
        ("daily loss", "daily loss halt"),
        ("bank after", "bank after win"),
        ("too few bars", "no bars"),
    ]
    for k, lab in rules:
        if k in t:
            return lab
    if not t.strip():
        return "silent / no signal"
    return "other: " + t[:50]


def main() -> int:
    r = json.loads(REPORT.read_text(encoding="utf-8"))
    flat_reasons: Counter = Counter()
    flat_days = []
    loss_days = []
    win_days = []
    loss_modes: Counter = Counter()
    loss_by_event: Counter = Counter()
    missed_up = []  # flat but day_chg large UP
    missed_dn = []

    for d in r["daily"]:
        net = float(d.get("net") or 0)
        setups = int(d.get("setups") or 0)
        chg = float(d.get("spot_chg") or 0)
        skip = str(d.get("skip") or "")
        tl = d.get("timeline") or []

        deny_c: Counter = Counter()
        for item in tl:
            if not isinstance(item, dict):
                continue
            if item.get("allow") is True:
                continue
            why = str(item.get("reason") or item.get("event") or "")
            if why:
                deny_c[reason_bucket(why)] += 1

        top_deny = deny_c.most_common(3)
        primary = top_deny[0][0] if top_deny else reason_bucket(skip) if skip else "silent / no signal"

        if setups == 0:
            flat_reasons[primary] += 1
            row = {
                "date": d["date"],
                "spot_chg": chg,
                "skip": skip,
                "primary_block": primary,
                "top_blocks": top_deny,
            }
            flat_days.append(row)
            if chg >= 100:
                missed_up.append(row)
            if chg <= -100:
                missed_dn.append(row)
            continue

        trades = d.get("trades") or []
        if net > 0:
            win_days.append(
                {
                    "date": d["date"],
                    "net": net,
                    "spot_chg": chg,
                    "trades": [
                        {
                            "side": t.get("side"),
                            "event": t.get("event"),
                            "net": float(t.get("net") or 0),
                            "entry": t.get("entry_time"),
                        }
                        for t in trades
                    ],
                }
            )
        else:
            for t in trades:
                side = t.get("side")
                evt = str(t.get("event") or "")
                tnet = float(t.get("net") or 0)
                mode = f"{side}:{evt}"
                loss_by_event[mode] += tnet
                # classify loss mode
                if "SR_RECLAIM" in evt:
                    loss_modes["CE_SR_fake_reclaim" if side == "CE" else "PE_SR"] += 1
                elif "BREAKOUT" in evt:
                    loss_modes["CE_breakout_fail"] += 1
                elif "BREAKDOWN" in evt:
                    loss_modes["PE_breakdown_snapback"] += 1
                elif "post_decay" in evt:
                    loss_modes["post_decay_knife"] += 1
                else:
                    loss_modes[f"other:{mode}"] += 1
            loss_days.append(
                {
                    "date": d["date"],
                    "net": net,
                    "spot_chg": chg,
                    "trades": [
                        {
                            "side": t.get("side"),
                            "event": t.get("event"),
                            "net": float(t.get("net") or 0),
                            "entry": t.get("entry_time"),
                            "exit": t.get("exit_time"),
                            "exit_reason": t.get("exit_reason"),
                        }
                        for t in trades
                    ],
                    "blocks_seen": top_deny,
                }
            )

    out = {
        "source": str(REPORT),
        "flat": {
            "count": len(flat_days),
            "reason_counts": flat_reasons.most_common(),
            "missed_up_ge_100": missed_up,
            "missed_down_le_m100": missed_dn,
            "days": flat_days,
        },
        "losses": {
            "count": len(loss_days),
            "mode_counts": loss_modes.most_common(),
            "pnl_by_event": {k: round(v, 0) for k, v in sorted(loss_by_event.items(), key=lambda x: x[1])},
            "days": sorted(loss_days, key=lambda x: x["net"]),
        },
        "wins": {
            "count": len(win_days),
            "total_net": round(sum(w["net"] for w in win_days), 0),
            "days": sorted(win_days, key=lambda x: -x["net"])[:20],
        },
        "learnings": [
            {
                "id": "L1_range_flat_ok",
                "when": "flat + chop/range-bound primary",
                "action": "keep flat — correct non-trade",
                "profit": "avoid death by 1000 cuts",
            },
            {
                "id": "L2_missed_up_trend",
                "when": "flat but spot_chg >= +100",
                "action": "allow CE SR/break only if vs>=70 and mom rising and day_eff>=0.35",
                "profit": "capture clean UP days currently skipped",
            },
            {
                "id": "L3_missed_sharp_pe",
                "when": "flat but spot_chg <= -150",
                "action": "allow one PE at day-low if mom<=-32 and vs<=-70 (no rebreak)",
                "profit": "harvest sharp falls instead of only avoiding CE",
            },
            {
                "id": "L4_cut_ce_sr_loss",
                "when": "loss CE SR_RECLAIM",
                "action": "stricter late/soft SR; require mom10>=15 and vs>=20",
                "profit": "remove largest repeat loser class",
            },
            {
                "id": "L5_cut_ce_break_loss",
                "when": "loss CE BREAKOUT",
                "action": "ce_min_break 75; block if path_eff<0.35; persist 3",
                "profit": "stop fake breakouts",
            },
            {
                "id": "L6_cut_pe_snap",
                "when": "loss PE BREAKDOWN",
                "action": "keep day-low + mom<=-32; add 3-bar hold after signal",
                "profit": "reduce snapback stops",
            },
            {
                "id": "L7_post_decay",
                "when": "loss post_decay",
                "action": "keep expiry min est 300; confirm 2; knife wait",
                "profit": "preserve Oct 13 style winners only",
            },
        ],
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("=== FLAT DAYS", len(flat_days), "===")
    for k, n in flat_reasons.most_common(12):
        print(f"  {n:3d}  {k}")
    print(f"\nMissed UP (>=+100): {len(missed_up)}")
    for row in missed_up[:12]:
        print(f"  {row['date']} chg={row['spot_chg']:+.0f} block={row['primary_block']}")
    print(f"Missed DOWN (<=-100): {len(missed_dn)}")
    for row in missed_dn[:12]:
        print(f"  {row['date']} chg={row['spot_chg']:+.0f} block={row['primary_block']}")

    print("\n=== LOSS DAYS", len(loss_days), "===")
    for k, n in loss_modes.most_common():
        print(f"  {n:3d}  {k}")
    print("  PnL by event:")
    for k, v in out["losses"]["pnl_by_event"].items():
        print(f"    {k}: {v:+,.0f}")
    print("\nWorst losses:")
    for row in out["losses"]["days"][:12]:
        t0 = row["trades"][0] if row["trades"] else {}
        print(
            f"  {row['date']} {row['net']:+,.0f} chg={row['spot_chg']:+.0f} "
            f"{t0.get('side')} {t0.get('event')} @{t0.get('entry')}"
        )

    print("\n=== TOP WINS ===")
    for row in out["wins"]["days"][:8]:
        t0 = row["trades"][0] if row["trades"] else {}
        print(
            f"  {row['date']} {row['net']:+,.0f} {t0.get('side')} {t0.get('event')}"
        )
    print(f"\nJSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

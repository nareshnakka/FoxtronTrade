"""Empirical: can prior-day features predict next open gap?"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

MINS = Path("data/expiries/last_3_months/minutes")


def main() -> int:
    files = sorted(MINS.glob("nifty_*.json"))
    days = []
    for p in files:
        rows = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or len(rows) < 50:
            continue
        o = float(rows[0].get("open") or rows[0]["close"])
        c = float(rows[-1]["close"])
        h = max(float(r["high"]) for r in rows)
        l = min(float(r["low"]) for r in rows)
        last = rows[-60:] if len(rows) >= 60 else rows
        lh = float(last[-1]["close"]) - float(last[0]["close"])
        days.append(
            {
                "date": p.stem.replace("nifty_", ""),
                "open": o,
                "close": c,
                "high": h,
                "low": l,
                "lh": lh,
                "vs_open": c - o,
                "close_loc": (c - l) / max(1e-9, h - l),
            }
        )

    gaps = []
    for i in range(len(days) - 1):
        a, b = days[i], days[i + 1]
        gap = b["open"] - a["close"]
        gaps.append(
            {
                "from": a["date"],
                "to": b["date"],
                "gap": gap,
                "prev_vs_open": a["vs_open"],
                "prev_lh": a["lh"],
                "prev_close_loc": a["close_loc"],
                "dir": "UP" if gap >= 20 else ("DOWN" if gap <= -20 else "FLAT"),
            }
        )

    print(f"sessions {len(days)}  gap samples {len(gaps)}")
    print("gap buckets |gap|>=20:", dict(Counter(g["dir"] for g in gaps)))
    n = len(gaps)
    up = sum(1 for g in gaps if g["dir"] == "UP")
    dn = sum(1 for g in gaps if g["dir"] == "DOWN")
    fl = sum(1 for g in gaps if g["dir"] == "FLAT")
    mean = sum(g["gap"] for g in gaps) / n
    print(
        f"baseline UP {100*up/n:.0f}%  DOWN {100*dn/n:.0f}%  FLAT {100*fl/n:.0f}%  "
        f"mean_gap {mean:+.1f}"
    )

    def rate(pred, want: str):
        sel = [g for g in gaps if pred(g)]
        if not sel:
            return 0, 0, 0.0
        hit = sum(1 for g in sel if g["dir"] == want)
        return len(sel), hit, 100.0 * hit / len(sel)

    checks = [
        ("prev UP >+70 → gap UP", lambda g: g["prev_vs_open"] > 70, "UP"),
        ("prev UP >+70 → gap DOWN (fade)", lambda g: g["prev_vs_open"] > 70, "DOWN"),
        ("prev DOWN <-70 → gap DOWN", lambda g: g["prev_vs_open"] < -70, "DOWN"),
        ("prev DOWN <-70 → gap UP (fade)", lambda g: g["prev_vs_open"] < -70, "UP"),
        ("last-hour >+30 → gap UP", lambda g: g["prev_lh"] > 30, "UP"),
        ("last-hour <-30 → gap DOWN", lambda g: g["prev_lh"] < -30, "DOWN"),
        ("close near high (>0.8) → gap UP", lambda g: g["prev_close_loc"] > 0.8, "UP"),
        ("close near high → gap DOWN fade", lambda g: g["prev_close_loc"] > 0.8, "DOWN"),
        ("close near low (<0.2) → gap DOWN", lambda g: g["prev_close_loc"] < 0.2, "DOWN"),
        ("close near low → gap UP fade", lambda g: g["prev_close_loc"] < 0.2, "UP"),
    ]
    print("\nConditional hit rates:")
    for name, pred, want in checks:
        nn, h, pct = rate(pred, want)
        print(f"  {name}: n={nn} hit={h} ({pct:.0f}%)")

    # Today's prediction from prior session if available
    if len(days) >= 2:
        prev = days[-2] if days[-1]["date"] >= "2026-11-03" else days[-1]
        # If last file is today, prior is -2; else prior is last complete
        today = "2026-11-03"
        prior = None
        for i, d in enumerate(days):
            if d["date"] == today and i > 0:
                prior = days[i - 1]
                break
        if prior is None:
            prior = days[-1]
        print(f"\nPrior day used: {prior['date']}")
        print(
            f"  vs_open {prior['vs_open']:+.1f}  last_hour {prior['lh']:+.1f}  "
            f"close_loc {prior['close_loc']:.2f}"
        )
        bias = []
        if prior["vs_open"] > 70:
            bias.append("strong UP day → mild fade/FLAT bias historically (not strong UP gap)")
        if prior["close_loc"] > 0.8:
            bias.append("closed near high")
        if prior["lh"] > 30:
            bias.append("strong last-hour bid")
        if prior["lh"] < -30:
            bias.append("weak last-hour offer")
        print("  notes:", "; ".join(bias) if bias else "no strong prior-day bias flags")

    sg = sorted(gaps, key=lambda g: abs(g["gap"]), reverse=True)[:6]
    print("\nLargest |gaps|:")
    for g in sg:
        print(
            f"  {g['from']}→{g['to']} gap={g['gap']:+.1f} "
            f"prev_vs={g['prev_vs_open']:+.1f} lh={g['prev_lh']:+.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

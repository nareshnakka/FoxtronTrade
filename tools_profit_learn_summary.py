"""Summarize year report after profit-learn gates."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

REPORT = Path("data/backtests/week_2025-11-03_to_2026-11-02_algo_report.json")
OUT = Path("data/backtests/profit_learn_after.json")
BEFORE_NET = 404_678  # prior 1yr @10L d100


def main() -> int:
    r = json.loads(REPORT.read_text(encoding="utf-8"))
    w = l = f = 0
    loss_days = []
    win_days = []
    modes: Counter = Counter()
    pnl_evt: Counter = Counter()
    flat_reasons: Counter = Counter()

    for d in r["daily"]:
        net = float(d.get("net") or 0)
        setups = int(d.get("setups") or 0)
        if setups == 0:
            f += 1
            # crude primary from timeline
            c: Counter = Counter()
            for item in d.get("timeline") or []:
                if not isinstance(item, dict) or item.get("allow") is True:
                    continue
                why = str(item.get("reason") or "").lower()
                if "chop" in why:
                    c["chop"] += 1
                elif "wait structure" in why:
                    c["wait structure"] += 1
                elif "open buffer" in why:
                    c["open buffer"] += 1
                elif "no new entries" in why:
                    c["after cutoff"] += 1
            flat_reasons[c.most_common(1)[0][0] if c else "silent"] += 1
            continue
        if net > 0:
            w += 1
            win_days.append({"date": d["date"], "net": net})
        else:
            l += 1
            trades = d.get("trades") or []
            loss_days.append({"date": d["date"], "net": net, "trades": trades})
            for t in trades:
                key = f"{t.get('side')}:{t.get('event')}"
                modes[key] += 1
                pnl_evt[key] += float(t.get("net") or 0)

    tot = sum(float(d.get("net") or 0) for d in r["daily"])
    mom: dict[str, float] = defaultdict(float)
    for d in r["daily"]:
        mom[d["date"][:7]] += float(d.get("net") or 0)

    out = {
        "net": round(tot, 0),
        "before_net": BEFORE_NET,
        "delta": round(tot - BEFORE_NET, 0),
        "wl_flat": [w, l, f],
        "loss_modes": modes.most_common(),
        "loss_pnl_by_event": {k: round(v, 0) for k, v in sorted(pnl_evt.items(), key=lambda x: x[1])},
        "loss_days": [
            {
                "date": x["date"],
                "net": round(x["net"], 0),
                "side": (x["trades"] or [{}])[0].get("side"),
                "event": (x["trades"] or [{}])[0].get("event"),
                "entry": (x["trades"] or [{}])[0].get("entry_time"),
            }
            for x in sorted(loss_days, key=lambda z: z["net"])
        ],
        "top_wins": sorted(win_days, key=lambda x: -x["net"])[:10],
        "mom": {k: round(v, 0) for k, v in sorted(mom.items())},
        "flat_reasons": flat_reasons.most_common(),
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"1yr net {tot:+,.0f} (before {BEFORE_NET:+,.0f}  delta {tot - BEFORE_NET:+,.0f})")
    print(f"W/L/flat {w}/{l}/{f}")
    print("loss modes", modes.most_common())
    print("loss pnl", out["loss_pnl_by_event"])
    print("losses:")
    for row in out["loss_days"]:
        print(f"  {row['date']} {row['net']:+,.0f} {row['side']} {row['event']} @{row['entry']}")
    print("MoM:")
    for k, v in out["mom"].items():
        print(f"  {k} {v:+,.0f}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

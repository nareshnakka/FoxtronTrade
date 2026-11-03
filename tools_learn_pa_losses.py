"""Learn gates from PA losing days vs winning PA days (causal features at entry)."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from backtest_detailed import _load_bars
from backtest_month_ledger import _load_month_days
from foxtrade.daily_pa import detect_daily_pa

REPORT = Path("data/backtests/week_2026-08-06_to_2026-11-03_algo_report.json")
OUT = Path("data/backtests/pa_loss_lessons_3m.json")


def _hhmm_to_i(t: str) -> int:
    hh, mm = map(int, t.split(":"))
    return (hh * 60 + mm) - (9 * 60 + 15)


def feats(closes: list[float], i: int) -> dict:
    o = closes[0]
    spot = closes[i]
    vs = spot - o
    mom10 = spot - closes[i - 10] if i >= 10 else 0.0
    mom5 = spot - closes[i - 5] if i >= 5 else 0.0
    hi = max(closes[: i + 1])
    lo = min(closes[: i + 1])
    rng = hi - lo
    give_ce = hi - spot
    give_pe = spot - lo
    # 15m OR
    orb = closes[:15]
    or_hi, or_lo = max(orb), min(orb)
    # chop proxy: path efficiency last 40
    w = closes[max(0, i - 39) : i + 1]
    span = max(w) - min(w)
    eff = abs(w[-1] - w[0]) / max(span, 1.0)
    # against open direction flip risk
    return {
        "vs": round(vs, 1),
        "mom10": round(mom10, 1),
        "mom5": round(mom5, 1),
        "rng": round(rng, 1),
        "give_ce": round(give_ce, 1),
        "give_pe": round(give_pe, 1),
        "or_w": round(or_hi - or_lo, 1),
        "eff40": round(eff, 2),
        "above_or": spot > or_hi,
        "below_or": spot < or_lo,
        "mins": i,
    }


def main() -> int:
    j = json.loads(REPORT.read_text(encoding="utf-8"))
    days = {
        d["date"]: d
        for d in _load_month_days(as_of=date(2026, 11, 3), calendar_days=100, all_available=False)
    }
    rows = []
    for d in j.get("daily") or []:
        for t in d.get("trades") or []:
            if not t.get("daily_pa"):
                continue
            day = d["date"]
            bars = _load_bars(days[day]["minutes_path"])
            closes = [float(b["close"]) for b in bars]
            i = _hhmm_to_i(str(t["entry_time"]))
            i = max(0, min(i, len(closes) - 1))
            f = feats(closes, i)
            f.update(
                {
                    "date": day,
                    "side": t["side"],
                    "entry_t": t["entry_time"],
                    "held": t.get("held_m"),
                    "net": round(float(t.get("net") or 0)),
                    "event": t.get("event"),
                    "day_oc": d.get("spot_chg"),
                    "win": float(t.get("net") or 0) > 0,
                }
            )
            rows.append(f)

    losses = [r for r in rows if not r["win"]]
    wins = [r for r in rows if r["win"]]
    print(f"PA trades {len(rows)} W {len(wins)} L {len(losses)}")

    def avg(key, xs):
        return round(sum(r[key] for r in xs) / max(1, len(xs)), 1)

    print("\nFeature averages L vs W:")
    for k in ("vs", "mom10", "mom5", "rng", "give_ce", "give_pe", "or_w", "eff40", "mins", "held"):
        if k == "held":
            print(
                f"  {k:8} L={avg(k, losses) if losses else 0} W={avg(k, wins) if wins else 0}"
            )
        else:
            print(f"  {k:8} L={avg(k, losses)} W={avg(k, wins)}")

    print("\nLoss detail:")
    for r in losses:
        print(
            f"  {r['date']} {r['side']} {r['entry_t']} net {r['net']:+} "
            f"vs {r['vs']:+} mom10 {r['mom10']:+} giveCE {r['give_ce']} "
            f"givePE {r['give_pe']} eff {r['eff40']} or_w {r['or_w']} "
            f"aboveOR {r['above_or']} belowOR {r['below_or']} day_oc {r['day_oc']}"
        )

    # Candidate gates: count how many losses each would have blocked + wins blocked
    gates = {
        "PE_only_if_vs<=-35": lambda r: not (r["side"] == "PE" and r["vs"] > -35),
        "CE_only_if_vs>=+40": lambda r: not (r["side"] == "CE" and r["vs"] < 40),
        "block_if_eff40<0.25": lambda r: r["eff40"] >= 0.25,
        "block_if_giveback>45": lambda r: (
            (r["side"] == "CE" and r["give_ce"] <= 45)
            or (r["side"] == "PE" and r["give_pe"] <= 45)
        ),
        "block_PE_before_10:30": lambda r: not (
            r["side"] == "PE" and r["mins"] < 75
        ),
        "block_entry_before_10:20": lambda r: r["mins"] >= 65,
        "mom10_aligned_strong": lambda r: (
            (r["side"] == "CE" and r["mom10"] >= 10)
            or (r["side"] == "PE" and r["mom10"] <= -10)
        ),
        "CE_not_if_give>40": lambda r: not (r["side"] == "CE" and r["give_ce"] > 40),
        "PE_not_if_give>40": lambda r: not (r["side"] == "PE" and r["give_pe"] > 40),
        "block_PB_if_or_w>90": lambda r: r["or_w"] <= 90,
        "combo_A": lambda r: (
            r["mins"] >= 65
            and r["eff40"] >= 0.22
            and (
                (r["side"] == "CE" and r["vs"] >= 35 and r["give_ce"] <= 50 and r["mom10"] >= 8)
                or (r["side"] == "PE" and r["vs"] <= -35 and r["give_pe"] <= 50 and r["mom10"] <= -8)
            )
        ),
        "combo_B": lambda r: (
            r["mins"] >= 55
            and (
                (r["side"] == "CE" and r["vs"] >= 40 and r["give_ce"] <= 55 and r["mom10"] >= 10)
                or (r["side"] == "PE" and r["vs"] <= -40 and r["give_pe"] <= 55 and r["mom10"] <= -10)
            )
            and r["eff40"] >= 0.20
        ),
        "combo_C_sr_quality": lambda r: (
            r["mins"] >= 60
            and r["eff40"] >= 0.25
            and abs(r["mom10"]) >= 10
            and (
                (r["side"] == "CE" and r["vs"] >= 30 and r["give_ce"] <= 45)
                or (r["side"] == "PE" and r["vs"] <= -30 and r["give_pe"] <= 45)
            )
        ),
    }

    print("\nGate impact (block losses / keep wins):")
    gate_stats = {}
    for name, fn in gates.items():
        bl = sum(1 for r in losses if not fn(r))
        bw = sum(1 for r in wins if not fn(r))
        kl = sum(1 for r in losses if fn(r))
        kw = sum(1 for r in wins if fn(r))
        gate_stats[name] = {
            "block_L": bl,
            "block_W": bw,
            "keep_L": kl,
            "keep_W": kw,
            "kept_net": sum(r["net"] for r in rows if fn(r)),
        }
        print(
            f"  {name:28} block L {bl}/{len(losses)} W {bw}/{len(wins)} "
            f"kept_net {gate_stats[name]['kept_net']:+}"
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {"losses": losses, "wins": wins, "gates": gate_stats},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nJSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

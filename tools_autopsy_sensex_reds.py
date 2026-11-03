"""Autopsy Sensex FastTrack reds — path, event, S/R features at entry."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from backtest_detailed import _load_bars

REPORT = Path("data/backtests/sensex_fasttrack_full_report.json")
OUT = Path("data/sensex/red_autopsy.json")


def feats(closes: list[float], i: int) -> dict:
    o = closes[0]
    spot = closes[i]
    vs = spot - o
    mom10 = spot - closes[i - 10] if i >= 10 else 0.0
    hi = max(closes[: i + 1])
    lo = min(closes[: i + 1])
    give_ce = hi - spot
    give_pe = spot - lo
    orb = closes[:15]
    or_w = max(orb) - min(orb) if len(orb) >= 15 else 0.0
    w = closes[max(0, i - 39) : i + 1]
    span = max(w) - min(w)
    eff = abs(w[-1] - w[0]) / max(span, 1.0)
    # local S/R 25
    look = closes[max(0, i - 25) : i]
    loc_lo = min(look) if look else spot
    loc_hi = max(look) if look else spot
    mid = (loc_lo + loc_hi) / 2.0
    return {
        "vs": round(vs, 1),
        "mom10": round(mom10, 1),
        "rng": round(hi - lo, 1),
        "give_ce": round(give_ce, 1),
        "give_pe": round(give_pe, 1),
        "or_w": round(or_w, 1),
        "eff40": round(eff, 2),
        "dist_sup": round(spot - loc_lo, 1),
        "dist_res": round(loc_hi - spot, 1),
        "above_mid": spot >= mid,
        "mins": i,
    }


def main() -> int:
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    trades = d.get("ledger") or []
    rows = []
    for t in trades:
        day = t["day"]
        path = Path(f"data/sensex/minutes/sensex_{day}.json")
        if not path.exists():
            continue
        bars = _load_bars(path)
        closes = [float(b["close"]) for b in bars]
        i = int(t.get("entry_i") or 0)
        i = max(0, min(i, len(closes) - 1))
        f = feats(closes, i)
        tag = (
            "PA"
            if t.get("daily_pa")
            else (
                "MICRO"
                if t.get("micro")
                else ("SOFT" if t.get("soft") else "DIR")
            )
        )
        f.update(
            {
                "date": day,
                "side": t["side"],
                "entry_t": t.get("entry_time"),
                "net": round(float(t.get("net") or 0)),
                "event": t.get("event"),
                "tag": tag,
                "held": t.get("held_m"),
                "win": float(t.get("net") or 0) > 0,
                "day_oc": None,
            }
        )
        rows.append(f)

    # attach day OC
    day_oc = {
        x["date"]: float(x.get("spot_chg") or 0)
        for x in d.get("daily") or []
    }
    for r in rows:
        r["day_oc"] = day_oc.get(r["date"])

    losses = [r for r in rows if not r["win"]]
    wins = [r for r in rows if r["win"]]
    print(f"Trades {len(rows)} W {len(wins)} L {len(losses)}")

    by = Counter()
    net_by: Counter = Counter()
    for r in rows:
        key = f"{r['tag']}:{r['event']}"
        by[key] += 1
        net_by[key] += r["net"]
    print("\nBy path:event")
    for k, n in by.most_common(25):
        wl_l = sum(1 for r in rows if f"{r['tag']}:{r['event']}" == k and not r["win"])
        wl_w = sum(1 for r in rows if f"{r['tag']}:{r['event']}" == k and r["win"])
        print(f"  {k:42} n={n:3} W/L {wl_w}/{wl_l} net={net_by[k]:+.0f}")

    def avg(key, xs):
        return round(sum(r[key] for r in xs) / max(1, len(xs)), 1)

    print("\nFeature avg L vs W:")
    for k in (
        "vs",
        "mom10",
        "rng",
        "give_ce",
        "give_pe",
        "or_w",
        "eff40",
        "dist_sup",
        "dist_res",
        "mins",
        "held",
    ):
        print(f"  {k:10} L={avg(k, losses)} W={avg(k, wins)}")

    # counter-trend losses
    ct = [
        r
        for r in losses
        if r["day_oc"] is not None
        and (
            (r["side"] == "CE" and r["day_oc"] < -150)
            or (r["side"] == "PE" and r["day_oc"] > 150)
        )
    ]
    print(f"\nCounter-trend losses (hindsight day_oc): {len(ct)}")

    # Candidate quality gates on existing trades
    gates = {
        "eff40>=0.35": lambda r: r["eff40"] >= 0.35,
        "eff40>=0.45": lambda r: r["eff40"] >= 0.45,
        "|mom10|>=40": lambda r: abs(r["mom10"]) >= 40,
        "|mom10|>=55": lambda r: abs(r["mom10"]) >= 55,
        "CE vs>=250": lambda r: not (r["side"] == "CE" and r["vs"] < 250),
        "PE vs<=-220": lambda r: not (r["side"] == "PE" and r["vs"] > -220),
        "CE give<=180": lambda r: not (r["side"] == "CE" and r["give_ce"] > 180),
        "PE give<=180": lambda r: not (r["side"] == "PE" and r["give_pe"] > 180),
        "block PA": lambda r: r["tag"] != "PA",
        "block MICRO": lambda r: r["tag"] != "MICRO",
        "block SOFT": lambda r: r["tag"] != "SOFT",
        "DIR only": lambda r: r["tag"] == "DIR",
        "SR reclaim/reject only": lambda r: str(r["event"] or "")
        in ("SR_RECLAIM", "SR_REJECT", "PA_SR_RECLAIM", "PA_SR_REJECT", "BREAKDOWN", "BREAKOUT"),
        "no PB_RESUME": lambda r: "PB_RESUME" not in str(r["event"] or ""),
        "mins>=60": lambda r: r["mins"] >= 60,
        "near S/R (dist<=40)": lambda r: (
            (r["side"] == "CE" and r["dist_sup"] <= 40)
            or (r["side"] == "PE" and r["dist_res"] <= 40)
            or r["tag"] == "DIR"
        ),
        "quality_A": lambda r: (
            r["eff40"] >= 0.40
            and abs(r["mom10"]) >= 45
            and (
                (r["side"] == "CE" and r["vs"] >= 220 and r["give_ce"] <= 200)
                or (r["side"] == "PE" and r["vs"] <= -200 and r["give_pe"] <= 200)
            )
            and "PB_RESUME" not in str(r["event"] or "")
        ),
        "quality_B": lambda r: (
            r["tag"] == "DIR"
            and r["eff40"] >= 0.35
            and abs(r["mom10"]) >= 40
            and (
                (r["side"] == "CE" and r["vs"] >= 250)
                or (r["side"] == "PE" and r["vs"] <= -220)
            )
        ),
        "quality_C_sr": lambda r: (
            r["eff40"] >= 0.38
            and abs(r["mom10"]) >= 50
            and str(r["event"] or "")
            in (
                "SR_RECLAIM",
                "SR_REJECT",
                "BREAKDOWN",
                "BREAKOUT",
                "PA_SR_RECLAIM",
                "PA_SR_REJECT",
            )
            and (
                (r["side"] == "CE" and r["vs"] >= 200)
                or (r["side"] == "PE" and r["vs"] <= -180)
            )
        ),
    }
    print("\nGate impact (block L / block W / kept_net):")
    gate_stats = {}
    for name, fn in gates.items():
        bl = sum(1 for r in losses if not fn(r))
        bw = sum(1 for r in wins if not fn(r))
        kept = sum(r["net"] for r in rows if fn(r))
        gate_stats[name] = {"block_L": bl, "block_W": bw, "kept_net": kept}
        print(
            f"  {name:28} block L {bl}/{len(losses)} W {bw}/{len(wins)} "
            f"kept_net {kept:+}"
        )

    OUT.write_text(
        json.dumps(
            {"losses": losses, "wins": wins, "gates": gate_stats, "by_event": dict(by)},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nJSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

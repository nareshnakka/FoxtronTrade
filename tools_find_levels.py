"""Find Nifty support/resistance from recent minute bars."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

MINS = Path("data/expiries/last_3_months/minutes")


def _load(day: str) -> list[dict]:
    p = MINS / f"nifty_{day}.json"
    if not p.exists():
        return []
    rows = json.loads(p.read_text(encoding="utf-8"))
    return rows if isinstance(rows, list) else []


def _swing_highs(closes: list[float], highs: list[float], left: int = 5, right: int = 5) -> list[float]:
    out = []
    for i in range(left, len(highs) - right):
        w = highs[i - left : i + right + 1]
        if highs[i] >= max(w) - 1e-9:
            out.append(highs[i])
    return out


def _cluster(levels: list[float], spot: float, tol: float = 25.0) -> list[dict]:
    """Merge nearby levels; rank resistance above spot / support below."""
    levels = sorted(levels)
    clusters: list[list[float]] = []
    for lv in levels:
        if not clusters or abs(lv - clusters[-1][-1]) > tol:
            clusters.append([lv])
        else:
            clusters[-1].append(lv)
    rows = []
    for c in clusters:
        mid = sum(c) / len(c)
        rows.append({"level": round(mid, 1), "hits": len(c), "dist": round(mid - spot, 1)})
    return rows


def main() -> int:
    today = date(2026, 11, 3).isoformat()
    # prior sessions
    files = sorted(MINS.glob("nifty_*.json"))
    days = [p.stem.replace("nifty_", "") for p in files if p.stat().st_size > 50]
    if today not in days and days:
        today = days[-1]
    idx = days.index(today) if today in days else len(days) - 1
    prior = days[idx - 1] if idx > 0 else None

    bars = _load(today)
    if len(bars) < 30:
        print("No bars")
        return 1
    opens = [float(b.get("open") or b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    spot = closes[-1]
    day_open = opens[0]
    day_high = max(highs)
    day_low = min(lows)
    orb_h = max(highs[:30])
    orb_l = min(lows[:30])

    raw = [day_high, orb_h, day_open]
    # round numbers every 50 around spot
    base = int(spot // 50) * 50
    for n in range(-4, 8):
        raw.append(float(base + 50 * n))

    if prior:
        pb = _load(prior)
        if pb:
            ph = max(float(b["high"]) for b in pb)
            pl = min(float(b["low"]) for b in pb)
            pc = float(pb[-1]["close"])
            po = float(pb[0].get("open") or pb[0]["close"])
            # classic pivots from prior day
            pp = (ph + pl + pc) / 3
            r1 = 2 * pp - pl
            r2 = pp + (ph - pl)
            r3 = ph + 2 * (pp - pl)
            s1 = 2 * pp - ph
            s2 = pp - (ph - pl)
            raw.extend([ph, pc, po, pp, r1, r2, r3, s1, s2, pl])
            print(f"Prior {prior}: H {ph:.1f} L {pl:.1f} C {pc:.1f}  PP {pp:.1f}")
            print(f"  R1 {r1:.1f}  R2 {r2:.1f}  R3 {r3:.1f}")
            print(f"  S1 {s1:.1f}  S2 {s2:.1f}")

    swings = _swing_highs(closes, highs, 8, 8)
    raw.extend(swings[-12:])

    clustered = _cluster(raw, spot, tol=20)
    resists = [c for c in clustered if c["dist"] >= 5]
    supports = [c for c in clustered if c["dist"] <= -5]
    resists = sorted(resists, key=lambda x: x["dist"])[:6]
    supports = sorted(supports, key=lambda x: -x["dist"])[:6]

    print(f"\nToday {today}  spot {spot:.1f}  open {day_open:.1f}  "
          f"high {day_high:.1f}  low {day_low:.1f}  vs_open {spot-day_open:+.1f}")
    print(f"ORB(30m)  high {orb_h:.1f}  low {orb_l:.1f}")
    print("\nResistance (above spot):")
    if not resists:
        print(f"  — at/through day high {day_high:.1f} (spot is at highs)")
    for r in resists:
        tag = []
        if abs(r["level"] - day_high) < 15:
            tag.append("day high")
        if abs(r["level"] - orb_h) < 15:
            tag.append("ORB high")
        print(f"  {r['level']:.1f}  (+{r['dist']:.0f})  hits={r['hits']}  "
              f"{', '.join(tag)}")
    print("\nSupport (below spot):")
    for s in supports:
        tag = []
        if abs(s["level"] - day_low) < 15:
            tag.append("day low")
        if abs(s["level"] - orb_l) < 15:
            tag.append("ORB low")
        if abs(s["level"] - day_open) < 15:
            tag.append("day open")
        print(f"  {s['level']:.1f}  ({s['dist']:+.0f})  hits={s['hits']}  "
              f"{', '.join(tag)}")

    # nearest key resistance summary
    if resists:
        nr = resists[0]
        print(f"\nNearest resistance: {nr['level']:.1f} (~{nr['dist']:.0f} pts above)")
    else:
        print(f"\nNearest resistance: day high {day_high:.1f} (price AT resistance / making highs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

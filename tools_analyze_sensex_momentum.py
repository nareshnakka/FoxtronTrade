"""Compare Sensex vs Nifty day/OR/momentum structure; propose point scale."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

SX = Path("data/sensex/minutes")
NF = Path("data/expiries/last_3_months/minutes")
OUT = Path("data/sensex/momentum_analysis.json")


def _closes(path: Path) -> list[float]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("bars") or []
    return [float(r["close"]) for r in rows]


def day_feats(closes: list[float]) -> dict | None:
    if len(closes) < 60:
        return None
    o, c = closes[0], closes[-1]
    hi, lo = max(closes), min(closes)
    orb = closes[:15]
    or_w = max(orb) - min(orb) if len(orb) >= 15 else 0.0
    # sample mom10 at mid-morning (~60m) and lunch (~150m)
    moms = []
    for i in (45, 60, 90, 120, 150):
        if i < len(closes):
            moms.append(closes[i] - closes[i - 10])
    return {
        "chg": c - o,
        "rng": hi - lo,
        "or_w": or_w,
        "abs_chg": abs(c - o),
        "mom10_med": statistics.median([abs(m) for m in moms]) if moms else 0.0,
        "spot": c,
    }


def collect(glob_dir: Path, prefix: str) -> list[dict]:
    rows = []
    for p in sorted(glob_dir.glob(f"{prefix}_*.json")):
        try:
            f = day_feats(_closes(p))
        except Exception:
            continue
        if not f:
            continue
        f["date"] = p.stem.replace(f"{prefix}_", "")
        rows.append(f)
    return rows


def summarize(xs: list[dict], name: str) -> dict:
    def med(key: str) -> float:
        v = [float(r[key]) for r in xs if r.get(key) is not None]
        return round(statistics.median(v), 1) if v else 0.0

    def p90(key: str) -> float:
        v = sorted(float(r[key]) for r in xs if r.get(key) is not None)
        if not v:
            return 0.0
        return round(v[int(0.9 * (len(v) - 1))], 1)

    return {
        "name": name,
        "n": len(xs),
        "med_rng": med("rng"),
        "p90_rng": p90("rng"),
        "med_or": med("or_w"),
        "p90_or": p90("or_w"),
        "med_abs_chg": med("abs_chg"),
        "med_mom10": med("mom10_med"),
        "med_spot": med("spot"),
    }


def main() -> int:
    sx = collect(SX, "sensex")
    nf = collect(NF, "nifty")
    sxs = summarize(sx, "sensex")
    nfs = summarize(nf, "nifty")
    scale = {}
    for k in ("med_rng", "med_or", "med_abs_chg", "med_mom10", "med_spot"):
        a, b = sxs[k], nfs[k]
        scale[k] = round(a / b, 3) if b else None
    # Prefer range-based scale for point gates
    gate_scale = scale.get("med_rng") or scale.get("med_spot") or 3.0
    report = {
        "sensex": sxs,
        "nifty": nfs,
        "scale_ratios": scale,
        "recommended_point_scale": round(float(gate_scale), 2),
        "note": (
            "Multiply Nifty point gates by recommended_point_scale for Sensex. "
            "% exits stay unchanged."
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

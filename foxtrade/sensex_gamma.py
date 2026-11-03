"""
Sensex gamma-blast detector (minute or tick closes).

Used to arm Thursday Sensex sessions — only trade when a real blast prints.
Also labels Tuesday expiry energy for logs.
"""
from __future__ import annotations

from typing import Any


def detect_sensex_gamma_blast(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Causal gamma blast on Sensex spot path.

    Fires when short-horizon momentum + range expansion confirm a dealer-style
    squeeze / dump (typical mid-week or expiry blast).
    """
    cfg = cfg or {}
    out: dict[str, Any] = {
        "allow": False,
        "side": None,
        "confidence": 0.0,
        "event": None,
        "reason": "warming",
        "mom5": 0.0,
        "mom10": 0.0,
        "vs_open": 0.0,
        "day_rng": 0.0,
        "early_rng": 0.0,
    }
    if not cfg.get("sensex_gamma_detect_enabled", True):
        out["reason"] = "gamma detect disabled"
        return out
    if len(closes) < 40:
        return out

    scale = float(cfg.get("point_scale") or cfg.get("sensex_point_scale") or 3.68)
    # Absolute Sensex pts — tuned so Thu arms ~blast days, not every session
    min_mom10 = float(cfg.get("sensex_gamma_min_mom10", round(48 * scale)))
    min_mom5 = float(cfg.get("sensex_gamma_min_mom5", round(32 * scale)))
    min_vs = float(cfg.get("sensex_gamma_min_vs_open", round(85 * scale)))
    expand_mult = float(cfg.get("sensex_gamma_range_expand_mult", 1.65))
    min_day_rng = float(cfg.get("sensex_gamma_min_day_range", round(95 * scale)))
    early_bars = max(15, int(cfg.get("sensex_gamma_early_bars", 30)))
    min_conf = float(cfg.get("sensex_gamma_min_confidence", 0.74))
    # Burst: last 3m must carry a chunk of the 10m move (true blast, not grind)
    burst_frac = float(cfg.get("sensex_gamma_burst_frac", 0.45))
    min_burst3 = float(cfg.get("sensex_gamma_min_burst3", round(22 * scale)))

    spot = float(closes[-1])
    day_open = float(closes[0])
    vs_open = spot - day_open
    mom5 = spot - float(closes[-6]) if len(closes) > 5 else 0.0
    mom10 = spot - float(closes[-11]) if len(closes) > 10 else 0.0
    day_hi = max(closes)
    day_lo = min(closes)
    day_rng = day_hi - day_lo
    early = closes[: min(early_bars, len(closes))]
    early_rng = max(early) - min(early) if early else 0.0

    out.update(
        {
            "mom5": round(mom5, 1),
            "mom10": round(mom10, 1),
            "vs_open": round(vs_open, 1),
            "day_rng": round(day_rng, 1),
            "early_rng": round(early_rng, 1),
        }
    )

    # Direction from dominant short mom
    if mom10 > 0 and mom5 >= 0:
        side = "CE"
    elif mom10 < 0 and mom5 <= 0:
        side = "PE"
    elif abs(mom5) >= abs(mom10) * 0.85:
        side = "CE" if mom5 > 0 else "PE"
    else:
        out["reason"] = "mom not aligned"
        return out

    abs_mom10 = abs(mom10)
    abs_mom5 = abs(mom5)
    abs_vs = abs(vs_open)
    burst3 = spot - float(closes[-4]) if len(closes) > 3 else 0.0
    abs_burst = abs(burst3)
    out["burst3"] = round(burst3, 1)

    expand_ok = early_rng > 0 and day_rng >= max(
        min_day_rng, early_rng * expand_mult
    )
    # Need both horizon moms — blast, not slow drift
    mom_ok = abs_mom10 >= min_mom10 and abs_mom5 >= min_mom5 * 0.75
    drive_ok = abs_vs >= min_vs
    burst_ok = abs_burst >= min_burst3 and (
        abs_mom10 <= 1e-6 or abs_burst >= abs_mom10 * burst_frac
    )
    # Burst must agree with side
    if side == "CE" and burst3 < 0:
        burst_ok = False
    if side == "PE" and burst3 > 0:
        burst_ok = False

    if not (expand_ok and mom_ok and drive_ok and burst_ok):
        bits = []
        if not expand_ok:
            bits.append(
                f"rng {day_rng:.0f}<{max(min_day_rng, early_rng * expand_mult):.0f}"
            )
        if not mom_ok:
            bits.append(f"mom10 {mom10:.0f}/mom5 {mom5:.0f}")
        if not drive_ok:
            bits.append(f"|vs| {abs_vs:.0f}<{min_vs:.0f}")
        if not burst_ok:
            bits.append(f"burst3 {burst3:.0f}")
        out["reason"] = "no gamma · " + ", ".join(bits)
        return out

    # Continuity: last 3 bars not hard-reversing the blast
    if len(closes) >= 4:
        a, b, c = float(closes[-3]), float(closes[-2]), float(closes[-1])
        soft = max(8.0, min_mom5 * 0.15)
        if side == "CE" and not (c >= b - soft and b >= a - soft * 1.2):
            out["reason"] = "gamma fade CE"
            return out
        if side == "PE" and not (c <= b + soft and b <= a + soft * 1.2):
            out["reason"] = "gamma fade PE"
            return out

    conf = 0.66
    conf += min(0.14, abs_mom10 / max(min_mom10 * 4, 1))
    conf += min(0.08, abs_mom5 / max(min_mom5 * 3, 1))
    conf += min(0.06, abs_burst / max(min_burst3 * 3, 1))
    if expand_ok:
        conf += 0.04
    if abs_vs >= min_vs * 1.25:
        conf += 0.04
    conf = min(0.96, conf)
    if conf < min_conf:
        out["reason"] = f"gamma conf {conf:.2f} < {min_conf}"
        out["confidence"] = round(conf, 3)
        out["side"] = side
        return out

    out.update(
        {
            "allow": True,
            "side": side,
            "confidence": round(conf, 3),
            "event": "GAMMA_BLAST_UP" if side == "CE" else "GAMMA_BLAST_DOWN",
            "reason": (
                f"gamma {side} mom10={mom10:+.0f} mom5={mom5:+.0f} "
                f"vs={vs_open:+.0f} rng {early_rng:.0f}→{day_rng:.0f}"
            ),
        }
    )
    return out

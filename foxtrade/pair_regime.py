"""
Track CE/PE pair regime after convergence.

Weekly pattern (user): ~2 times/week one leg expands 2–3x,
the other crushes toward 80 / 70 / 50.

Regimes:
- BREAKOUT_EXPAND — directional move, winner accelerating
- RANGE_BOUND — chop near mid, S/R holding
- DECAY — both bleeding / theta grind
- CRUSH_LOSER — one side collapsing hard
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CRUSH_LEVELS = (80.0, 70.0, 50.0)


@dataclass
class PairRegime:
    regime: str = "UNKNOWN"
    winner: str | None = None  # CE | PE
    loser: str | None = None
    ce_ltp: float | None = None
    pe_ltp: float | None = None
    ce_mult: float | None = None  # vs mid/ref
    pe_mult: float | None = None
    crush_level: float | None = None  # crossed 80/70/50
    expand_mult: float | None = None
    near_support: bool = False
    near_resistance: bool = False
    range_bound: bool = False
    decaying: bool = False
    advice: str = ""
    reasons: list[str] = field(default_factory=list)


def evaluate_pair_regime(
    *,
    ce_ltp: float,
    pe_ltp: float,
    ce_ref: float | None,
    pe_ref: float | None,
    ce_hist: list[float],
    pe_hist: list[float],
    index: dict[str, Any],
    decay_score: float = 0.0,
) -> PairRegime:
    out = PairRegime(ce_ltp=ce_ltp, pe_ltp=pe_ltp)
    ref_ce = ce_ref or ce_ltp
    ref_pe = pe_ref or pe_ltp
    out.ce_mult = ce_ltp / ref_ce if ref_ce else 1.0
    out.pe_mult = pe_ltp / ref_pe if ref_pe else 1.0

    # Winner / loser by multiple vs ref (convergence mid area)
    if out.ce_mult >= out.pe_mult:
        out.winner, out.loser = "CE", "PE"
        out.expand_mult = out.ce_mult
        loser_px = pe_ltp
    else:
        out.winner, out.loser = "PE", "CE"
        out.expand_mult = out.pe_mult
        loser_px = ce_ltp

    for lvl in CRUSH_LEVELS:
        if loser_px <= lvl:
            out.crush_level = lvl
            break

    spot = index.get("spot")
    support = index.get("support")
    resistance = index.get("resistance")
    if spot is not None and support is not None:
        out.near_support = spot <= support * 1.0015
    if spot is not None and resistance is not None:
        out.near_resistance = spot >= resistance * 0.9985

    # Range-bound: both premiums near ref and spot between S/R
    mid_gap = abs(ce_ltp - pe_ltp)
    both_near = abs(out.ce_mult - 1) < 0.12 and abs(out.pe_mult - 1) < 0.12
    ema9, ema21 = index.get("ema9"), index.get("ema21")
    flat = ema9 is not None and ema21 is not None and abs(ema9 - ema21) < 20
    out.range_bound = both_near and flat and mid_gap < 15
    if out.range_bound:
        out.reasons.append("range-bound premiums + flat EMAs")

    # Decay: both down from ref
    out.decaying = (ce_ltp < ref_ce - 5 and pe_ltp < ref_pe - 5) or decay_score >= 0.55
    if out.decaying:
        out.reasons.append("both legs decaying / decay-score high")

    # Recent slopes
    if len(ce_hist) >= 40 and len(pe_hist) >= 40:
        ce_slope = ce_hist[-1] - ce_hist[-40]
        pe_slope = pe_hist[-1] - pe_hist[-40]
        if ce_slope > 8 and pe_slope < -3:
            out.winner, out.loser = "CE", "PE"
            out.reasons.append(f"CE expanding {ce_slope:+.1f} / PE {pe_slope:+.1f}")
        elif pe_slope > 8 and ce_slope < -3:
            out.winner, out.loser = "PE", "CE"
            out.reasons.append(f"PE expanding {pe_slope:+.1f} / CE {ce_slope:+.1f}")

    # Classify regime
    if out.expand_mult and out.expand_mult >= 1.8:
        out.regime = "BREAKOUT_EXPAND"
        out.advice = f"ride {out.winner} toward 2–3x; manage {out.loser} crush"
    elif out.crush_level is not None and (out.expand_mult or 1) >= 1.25:
        out.regime = "CRUSH_LOSER"
        out.advice = f"{out.loser} crushed ≤{out.crush_level:.0f}; favor {out.winner}"
    elif out.decaying:
        out.regime = "DECAY"
        out.advice = "avoid new adds; square weak longs; wait for impulse"
    elif out.range_bound or (out.near_support and out.near_resistance):
        out.regime = "RANGE_BOUND"
        out.advice = "chop — tight targets / no chase; wait S/R break"
    elif out.near_resistance:
        out.regime = "AT_RESISTANCE"
        out.advice = "prefer PE / fade CE trap at resistance"
        out.reasons.append("spot at resistance")
    elif out.near_support:
        out.regime = "AT_SUPPORT"
        out.advice = "prefer CE bounce / careful PE"
        out.reasons.append("spot at support")
    else:
        out.regime = "NEUTRAL"
        out.advice = "watch converge / skew"

    if out.crush_level:
        out.reasons.append(f"loser ≤{out.crush_level:.0f}")
    if out.expand_mult and out.expand_mult >= 2.0:
        out.reasons.append(f"winner ~{out.expand_mult:.1f}x")

    return out


def manage_long_decision(
    *,
    side: str,
    entry: float,
    px: float,
    regime: PairRegime,
    cfg: dict[str, Any],
) -> str | None:
    """
    Return exit reason or None to hold.
    Extends beyond 169–175 when BREAKOUT_EXPAND toward 2–3x;
    cuts earlier in DECAY / RANGE; respects crush if wrong side.
    """
    target_low = float(cfg.get("target_low", 169))
    target_high = float(cfg.get("target_high", 175))
    stop = float(cfg.get("stop_loss", 120))
    expand_cap = float(cfg.get("expand_target_mult", 2.5))  # book near 2.5x
    trail_after = float(cfg.get("expand_trail_after_mult", 2.0))

    mult = px / entry if entry else 1.0
    is_winner = regime.winner == side
    is_loser = regime.loser == side

    if px <= stop:
        return f"stop {stop}"

    # Wrong side crushing hard — don't wait for 120 if already structurally dead
    if is_loser and regime.crush_level is not None and px <= float(cfg.get("crush_cut", 85)):
        return f"wrong-side crush ≤{regime.crush_level:.0f}"

    if regime.regime == "DECAY" and px < entry - 8:
        return "decay cut"

    if regime.regime == "RANGE_BOUND":
        if px >= target_low:
            return f"range target {px:.1f}"
        if px <= entry - 10:
            return "range failed"

    # Strong expand: hold past 169 toward 2–3x, trail
    if regime.regime in ("BREAKOUT_EXPAND", "CRUSH_LOSER") and is_winner:
        if mult >= expand_cap:
            return f"expand cap {mult:.1f}x"
        if mult >= trail_after and regime.expand_mult and px < entry * trail_after * 0.92:
            return f"trail off {mult:.1f}x peak zone"
        # still below classic target — hold for expand
        if px >= target_high and mult < trail_after:
            # allow scale logic: soft book only if S/R against
            if side == "CE" and regime.near_resistance:
                return f"CE at R + target {target_high}"
            if side == "PE" and regime.near_support:
                return f"PE at S + target {target_high}"
        return None

    # Default classic target band
    if px >= target_low:
        return f"target hit ({px:.1f} in {target_low}-{target_high})"

    return None

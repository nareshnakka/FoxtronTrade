"""
Expert-style entry / risk filters.

Goals (no lookahead):
- Survive opening stop-hunts / false dumps that reverse
- Skip chop; require premium confirmation with the spot move
- Size lots from capital + risk % (e.g. ₹2L book)
- Daily loss circuit-breaker
"""

from __future__ import annotations

from typing import Any


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def minutes_from_open(closes: list[float]) -> int:
    return max(0, len(closes) - 1)


def detect_opening_shakeout(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Classic institutional open: dump (or spike) then reclaim.
    If early extreme is fading back through open → fade the first move.
    Only valid in the early session — do not block afternoon S/R entries.
    """
    cfg = cfg or {}
    window = int(cfg.get("shakeout_window_minutes", 25))
    soft = float(cfg.get("trend_soft_pts", 35))
    if len(closes) < 5:
        return {"active": False, "prefer_side": None, "reason": ""}
    # Expire open-shakeout after early window (lesson 2026-11-02)
    mins = minutes_from_open(closes)
    if mins > int(cfg.get("shakeout_valid_minutes", window * 2)):
        return {"active": False, "prefer_side": None, "block_side": None, "reason": ""}

    open_px = float(closes[0])
    spot = float(closes[-1])
    early = closes[: min(window, len(closes))]
    early_low = min(early)
    early_high = max(early)
    vs_open = spot - open_px
    dump_depth = open_px - early_low
    spike_height = early_high - open_px

    # Fake dump then reclaim / grind up
    if dump_depth >= soft and vs_open >= -soft * 0.35 and spot > early_low + soft * 0.6:
        return {
            "active": True,
            "prefer_side": "CE",
            "block_side": "PE",
            "reason": f"open dump −{dump_depth:.0f} then reclaim (fade PE)",
            "dump_depth": round(dump_depth, 1),
            "spike_height": round(spike_height, 1),
        }
    # Fake spike then fail
    if spike_height >= soft and vs_open <= soft * 0.35 and spot < early_high - soft * 0.6:
        return {
            "active": True,
            "prefer_side": "PE",
            "block_side": "CE",
            "reason": f"open spike +{spike_height:.0f} then fail (fade CE)",
            "dump_depth": round(dump_depth, 1),
            "spike_height": round(spike_height, 1),
        }
    return {"active": False, "prefer_side": None, "block_side": None, "reason": ""}


def chop_score(closes: list[float], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """High score = range / mean-reverting tape — avoid directional entries."""
    cfg = cfg or {}
    look = int(cfg.get("chop_lookback_minutes", 30))
    if len(closes) < max(15, look // 2):
        return {"score": 0.0, "is_chop": False, "reason": "warming"}
    window = closes[-look:] if len(closes) >= look else closes
    hi, lo = max(window), min(window)
    rng = hi - lo
    net = abs(window[-1] - window[0])
    # many turns / little net progress
    flips = 0
    for i in range(2, len(window)):
        d1 = window[i - 1] - window[i - 2]
        d2 = window[i] - window[i - 1]
        if d1 * d2 < 0:
            flips += 1
    efficiency = net / max(rng, 1.0)
    score = 0.0
    reasons = []
    if rng < float(cfg.get("chop_range_pts", 55)):
        score += 0.35
        reasons.append(f"tight range {rng:.0f}")
    if efficiency < 0.35:
        score += 0.35
        reasons.append(f"low efficiency {efficiency:.2f}")
    if flips >= max(6, look // 5):
        score += 0.25
        reasons.append(f"whipsaw flips={flips}")
    # Near-open grind after first hour = institutional range — sit out
    if len(closes) >= 60:
        vs = abs(float(closes[-1]) - float(closes[0]))
        if vs < float(cfg.get("chop_near_open_pts", 45)) and rng < float(cfg.get("chop_range_pts", 55)) * 1.4:
            score += 0.4
            reasons.append(f"near-open grind Δ{vs:.0f}")
    thr = float(cfg.get("chop_score_threshold", 0.5))
    return {
        "score": round(min(1.0, score), 3),
        "is_chop": score >= thr,
        "range": round(rng, 1),
        "efficiency": round(efficiency, 3),
        "reason": "; ".join(reasons) or "ok",
    }


def orb_levels(closes: list[float], *, cfg: dict[str, Any] | None = None) -> dict[str, float]:
    """Opening-range high/low from first orb_minutes (causal)."""
    cfg = cfg or {}
    orb_n = int(cfg.get("orb_minutes", 45))
    if len(closes) < max(5, orb_n // 2):
        n = max(5, min(len(closes), orb_n))
    else:
        n = min(orb_n, len(closes))
    orb = closes[:n]
    return {
        "orb_high": float(max(orb)),
        "orb_low": float(min(orb)),
        "orb_n": float(n),
        "day_open": float(closes[0]),
        "day_high": float(max(closes)),
        "day_low": float(min(closes)),
        "spot": float(closes[-1]),
    }


def orb_broke_without_retest(
    closes: list[float],
    *,
    side: str,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    True when price broke OR, extended, then never pulled back to OR — classic chase.
    Lesson 2026-11-02: first ORB CE chase at day high stopped out; wait retest/dip.
    """
    cfg = cfg or {}
    if not cfg.get("block_orb_chase_without_retest", True):
        return False
    lv = orb_levels(closes, cfg=cfg)
    orb_n = int(lv["orb_n"])
    if len(closes) <= orb_n + 3:
        return False
    buf = float(cfg.get("break_buffer_pts", 8))
    # Retest = pullback from post-break peak back to OR (not "still near OR")
    prox = float(cfg.get("orb_retest_proximity_pts", 8))
    min_ext = float(cfg.get("orb_chase_min_extension", 12))
    orb_hi = lv["orb_high"]
    orb_lo = lv["orb_low"]
    spot = lv["spot"]
    day_hi = lv["day_high"]
    day_lo = lv["day_low"]
    extreme_prox = float(cfg.get("extreme_proximity_pts", 18))
    broke = False
    retested = False
    extension = 0.0
    peak = None
    trough = None
    for px in closes[orb_n:]:
        px = float(px)
        if side == "CE":
            if px >= orb_hi + buf:
                broke = True
                extension = max(extension, px - orb_hi)
                peak = px if peak is None else max(peak, px)
            if (
                broke
                and peak is not None
                and extension >= min_ext
                and (peak - px) >= min_ext * 0.45
                and px <= orb_hi + prox
            ):
                retested = True
        else:
            if px <= orb_lo - buf:
                broke = True
                extension = max(extension, orb_lo - px)
                trough = px if trough is None else min(trough, px)
            if (
                broke
                and trough is not None
                and extension >= min_ext
                and (px - trough) >= min_ext * 0.45
                and px >= orb_lo - prox
            ):
                retested = True
    if not (broke and extension >= min_ext and not retested):
        return False
    # Chase = still pressed against session extreme without having retested OR
    if side == "CE":
        return spot >= day_hi - extreme_prox
    return spot <= day_lo + extreme_prox


def orb_break_already_failed(
    closes: list[float],
    *,
    side: str,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    True if OR was broken then price fell back inside OR (failed breakout).
    Lesson 2026-11-02: after failed ORB CE, don't re-chase highs — wait S/R reclaim.
    """
    cfg = cfg or {}
    if not cfg.get("block_orb_after_failed_break", True):
        return False
    lv = orb_levels(closes, cfg=cfg)
    orb_n = int(lv["orb_n"])
    if len(closes) <= orb_n + 5:
        return False
    buf = float(cfg.get("break_buffer_pts", 8))
    orb_hi = lv["orb_high"]
    orb_lo = lv["orb_low"]
    broke = False
    inside_bars = 0
    for px in closes[orb_n:]:
        px = float(px)
        if side == "CE":
            if px >= orb_hi + buf:
                broke = True
            elif broke and px < orb_hi:
                inside_bars += 1
        else:
            if px <= orb_lo - buf:
                broke = True
            elif broke and px > orb_lo:
                inside_bars += 1
    return bool(broke and inside_bars >= int(cfg.get("orb_fail_inside_bars", 2)))


def detect_sr_range_entry(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Index range / S/R entry (lesson 2026-11-02).

    UP day: after a real upside swing, wait for pullback toward open / OR high
    (support), then buy CE when momentum turns up — do not chase day high.
    DOWN day: mirror for PE at resistance / OR low.
    """
    cfg = cfg or {}
    if not cfg.get("prefer_sr_dip_entry", True):
        return {"active": False, "prefer_side": None, "reason": "", "event": None}

    if len(closes) < int(cfg.get("orb_minutes", 30)) + 10:
        return {"active": False, "prefer_side": None, "reason": "", "event": None}

    soft = float(cfg.get("trend_soft_pts", 35))
    hard = float(cfg.get("trend_hard_pts", 70))
    min_ext = float(cfg.get("sr_dip_min_extent", 50))
    pull_min = float(cfg.get("sr_dip_pullback_min", 25))
    prox = float(cfg.get("sr_dip_proximity_pts", 22))
    hold_n = int(cfg.get("sr_dip_turn_bars", 4))
    lv = orb_levels(closes, cfg=cfg)
    open_px = lv["day_open"]
    spot = lv["spot"]
    day_hi = lv["day_high"]
    day_lo = lv["day_low"]
    orb_hi = lv["orb_high"]
    orb_lo = lv["orb_low"]
    vs_open = spot - open_px
    up_ext = day_hi - open_px
    dn_ext = open_px - day_lo
    recent = closes[-hold_n:] if len(closes) >= hold_n else closes
    turn_up = len(recent) >= 3 and float(recent[-1]) > float(recent[0]) and float(recent[-1]) >= min(recent) + soft * 0.15
    turn_dn = len(recent) >= 3 and float(recent[-1]) < float(recent[0]) and float(recent[-1]) <= max(recent) - soft * 0.15

    # Support = higher of open and OR high (UP-day dip buy zone)
    ce_support = max(open_px, orb_hi - soft * 0.25)
    # Also treat deep dump through open then reclaim as S/R shakeout
    dumped_to_support = (day_hi - spot) >= pull_min or vs_open <= soft * 0.35
    near_ce_sup = abs(spot - ce_support) <= prox or (vs_open >= -soft * 0.4 and vs_open <= soft and spot >= min(closes[-20:]) + soft * 0.2)
    # Must have printed a real UP swing first.
    # Range-bound autopsy (Oct 5/14 loss vs Oct 7/27 win):
    #   losses = CE bought 20–30pts *under* support while 10m mom falling
    #   wins   = spot at/above support OR 10m mom already rising
    # Rule: reject only when under support AND mom still down.
    mom_n = min(10, max(0, len(closes) - 1))
    mom10 = float(closes[-1] - closes[-(mom_n + 1)]) if mom_n >= 5 else 0.0
    max_under = float(cfg.get("sr_reclaim_max_under_support", 12.0))
    if (
        cfg.get("enable_sr_reclaim", True)
        and up_ext >= min_ext
        and dumped_to_support
        and near_ce_sup
        and turn_up
        and spot < day_hi - prox * 0.5
    ):
        # Prefer reclaim of open after undercut
        undercut = day_lo <= open_px + soft * 0.15
        reclaim = spot >= open_px - prox * 0.35
        vs_sup = spot - ce_support
        # Hard floor: never buy CE more than max_under below support
        # (mom rising while still deep under support still knifed Oct 5 PM).
        # Soft reject: slightly under support is OK only if mom already up.
        fake_undercut = vs_sup < -max_under or (vs_sup < -max_under * 0.5 and mom10 < 0)
        # Train Feb/May/Sep 2026: weak/late/dead-cat CE reclaim bleed.
        # Keep Oct winners (soft vs early, rising mom, not after huge giveback).
        min_mom = float(cfg.get("sr_reclaim_min_mom10", 0))
        weak_mom = mom10 < min_mom
        mins = max(0, len(closes) - 1)
        late_after = int(cfg.get("sr_soft_no_entry_after_minutes", 210))
        late_min_vs = float(cfg.get("sr_late_min_vs_open", 35))
        late_soft = mins >= late_after and vs_open < late_min_vs
        # Profit-learn 1yr: late CE SR losses (Apr20 13:34, Jun4 13:27) vs
        # Jul24 13:51 win (spot well above support + strong mom). After hard
        # cutoff, demand clear reclaim above support — not a knife probe.
        late_hard_after = int(cfg.get("sr_late_hard_after_minutes", 240))
        late_above = float(cfg.get("sr_late_min_above_support", 15))
        late_mom = float(cfg.get("sr_late_min_mom10", 20))
        late_hard = (
            mins >= late_hard_after
            and (vs_sup < late_above or mom10 < late_mom)
        )
        # Soft session (not yet hard UP): block deep under-support knives
        # (Apr8 under-10). Allow 0–5pt flicker so Oct28/Nov2 reclaim can print.
        soft_under = bool(cfg.get("sr_require_above_support_if_soft", True)) and (
            vs_open < hard and vs_sup < -5.0
        )
        # Profit-learn: hard UP but stalling 10m mom = Oct27 SR knife.
        # Soft mid-vs reclaim (Apr2 mom~6) still allowed.
        hard_stall_mom = float(cfg.get("sr_hard_min_mom10", 15))
        hard_stall = vs_open >= hard and mom10 < hard_stall_mom
        # Morning SR after spent OR (≥100pts) = Oct27 even with mom~19.
        # Jul24 afternoon SR (mins>150) still allowed.
        mins_now = max(0, len(closes) - 1)
        or_range = float(orb_hi - orb_lo) if orb_hi and orb_lo else 0.0
        spent_or = float(cfg.get("sr_spent_or_range_pts", 100))
        spent_until = int(cfg.get("sr_spent_or_until_minutes", 150))
        spent_or_am = (
            or_range >= spent_or
            and mins_now < spent_until
            and mom10 < float(cfg.get("sr_spent_or_min_mom10", 25))
        )
        giveback = day_hi - spot
        max_gb = float(cfg.get("sr_max_giveback_pts", 0))
        # Soft-day dead-cat: classic. Hard-day also — Nov4 2026 SR knife bought
        # CE after ~70pt giveback from day high while still "hard UP".
        hard_gb = float(cfg.get("sr_hard_max_giveback_pts", 0)) or max_gb
        dead_cat = max_gb > 0 and giveback >= max_gb and vs_open < hard
        hard_dead_cat = hard_gb > 0 and giveback >= hard_gb and vs_open >= hard
        # Train Jun 2026: soft early SR (vs +8) needs strong rising mom.
        soft_vs = float(cfg.get("sr_soft_vs_open", 25))
        soft_mom = float(cfg.get("sr_soft_min_mom10", 18))
        soft_early = vs_open < soft_vs and mom10 < soft_mom
        if (
            not fake_undercut
            and not weak_mom
            and not late_soft
            and not late_hard
            and not soft_under
            and not hard_stall
            and not spent_or_am
            and not dead_cat
            and not hard_dead_cat
            and not soft_early
            and (
                (undercut and reclaim)
                or (spot >= ce_support - prox and vs_open >= -soft * 0.25)
            )
        ):
            return {
                "active": True,
                "prefer_side": "CE",
                "block_side": "PE",
                "event": "SR_RECLAIM",
                "reason": (
                    f"UP-range S/R: high +{up_ext:.0f} → dip to support "
                    f"{ce_support:.0f} → CE reclaim (spot {spot:.0f})"
                ),
                "support": round(ce_support, 1),
                "resistance": round(day_hi, 1),
                "strength": min(1.0, up_ext / max(hard, 1) * 0.55 + 0.35),
            }

    pe_resist = min(open_px, orb_lo + soft * 0.25)
    dumped_to_resist = (spot - day_lo) >= pull_min or vs_open >= -soft * 0.35
    near_pe_res = abs(spot - pe_resist) <= prox
    if (
        cfg.get("enable_sr_reject", True)
        and dn_ext >= min_ext
        and dumped_to_resist
        and near_pe_res
        and turn_dn
        and spot > day_lo + prox * 0.5
    ):
        spiked = day_hi >= open_px - soft * 0.15
        fail = spot <= open_px + prox * 0.35
        if (spiked and fail) or (spot <= pe_resist + prox and vs_open <= soft * 0.25):
            return {
                "active": True,
                "prefer_side": "PE",
                "block_side": "CE",
                "event": "SR_REJECT",
                "reason": (
                    f"DOWN-range S/R: low {dn_ext:.0f} → bounce to resist "
                    f"{pe_resist:.0f} → PE reject (spot {spot:.0f})"
                ),
                "support": round(day_lo, 1),
                "resistance": round(pe_resist, 1),
                "strength": min(1.0, dn_ext / max(hard, 1) * 0.55 + 0.35),
            }

    return {"active": False, "prefer_side": None, "reason": "", "event": None}


def detect_opening_range_break(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Classic ORB: first orb_minutes define range; trade break + hold.
    Skip tiny (chop) or huge (already spent) opening ranges.
    """
    cfg = cfg or {}
    orb_n = int(cfg.get("orb_minutes", 45))
    if len(closes) < orb_n + 5:
        return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0, "style": "orb"}

    orb = closes[:orb_n]
    orb_hi, orb_lo = max(orb), min(orb)
    orb_rng = orb_hi - orb_lo
    min_rng = float(cfg.get("orb_min_range_pts", 35))
    max_rng = float(cfg.get("orb_max_range_pts", 140))
    if orb_rng < min_rng:
        return {
            "event": None,
            "prefer_side": None,
            "reason": f"ORB too tight {orb_rng:.0f}",
            "strength": 0.0,
            "style": "orb",
            "orb_high": orb_hi,
            "orb_low": orb_lo,
        }
    if orb_rng > max_rng:
        return {
            "event": None,
            "prefer_side": None,
            "reason": f"ORB already spent {orb_rng:.0f}",
            "strength": 0.0,
            "style": "orb",
            "orb_high": orb_hi,
            "orb_low": orb_lo,
        }

    buf = float(cfg.get("break_buffer_pts", 10))
    hold_n = int(cfg.get("break_hold_minutes", 5))
    spot = float(closes[-1])
    day_open = float(closes[0])
    recent = closes[-hold_n:]
    # Must still be outside OR on the hold window
    if spot >= orb_hi + buf and min(recent) >= orb_hi - buf * 0.2 and spot >= day_open:
        strength = min(1.0, (spot - orb_hi) / max(orb_rng * 0.35, buf))
        return {
            "event": "BREAKOUT",
            "prefer_side": "CE",
            "reason": f"ORB break ↑ {orb_hi:.0f} (OR {orb_rng:.0f}pts)",
            "strength": round(strength, 3),
            "level": orb_hi,
            "style": "orb",
            "orb_high": orb_hi,
            "orb_low": orb_lo,
        }
    if spot <= orb_lo - buf and max(recent) <= orb_lo + buf * 0.2 and spot <= day_open:
        strength = min(1.0, (orb_lo - spot) / max(orb_rng * 0.35, buf))
        return {
            "event": "BREAKDOWN",
            "prefer_side": "PE",
            "reason": f"ORB break ↓ {orb_lo:.0f} (OR {orb_rng:.0f}pts)",
            "strength": round(strength, 3),
            "level": orb_lo,
            "style": "orb",
            "orb_high": orb_hi,
            "orb_low": orb_lo,
        }
    return {
        "event": None,
        "prefer_side": None,
        "reason": "",
        "strength": 0.0,
        "style": "orb",
        "orb_high": orb_hi,
        "orb_low": orb_lo,
    }


def detect_breakout_breakdown(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Intraday breakout / breakdown vs rolling structure (no lookahead).

    Prefer ORB when entry_style=orb (calibrated default). Else swing structure.
    BREAKOUT: spot clears level and holds near session high → CE
    BREAKDOWN: spot breaks level and holds near session low → PE
    """
    cfg = cfg or {}
    style = str(cfg.get("entry_style", "orb")).lower()
    if style == "orb":
        orb = detect_opening_range_break(closes, cfg=cfg)
        if orb.get("event") or len(closes) < int(cfg.get("orb_minutes", 45)) + 8:
            return orb
        # Fall through to swing if ORB silent after range is established

    if len(closes) < 20:
        return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}

    look = int(cfg.get("structure_lookback_minutes", 20))
    buf = float(cfg.get("break_buffer_pts", 8))
    hold_n = int(cfg.get("break_hold_minutes", 3))
    spot = float(closes[-1])
    day_hi = max(closes)
    day_lo = min(closes)
    # Prior window excludes the last few bars so "break" is vs past structure
    prior = closes[-(look + hold_n) : -hold_n] if len(closes) > look + hold_n else closes[:-hold_n]
    if len(prior) < 8:
        prior = closes[:-hold_n] if len(closes) > hold_n else closes[:-1]
    if not prior:
        return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}

    swing_hi = max(prior)
    swing_lo = min(prior)
    recent = closes[-hold_n:] if hold_n > 0 else [spot]
    held_high = min(recent) >= swing_hi - buf * 0.25
    held_low = max(recent) <= swing_lo + buf * 0.25

    if spot >= swing_hi + buf and held_high and spot >= day_hi - float(cfg.get("extreme_proximity_pts", 12)):
        strength = min(1.0, (spot - swing_hi) / max(buf * 2, 1))
        return {
            "event": "BREAKOUT",
            "prefer_side": "CE",
            "reason": f"breakout above {swing_hi:.0f} (+{spot - swing_hi:.0f})",
            "strength": round(strength, 3),
            "level": swing_hi,
        }
    if spot <= swing_lo - buf and held_low and spot <= day_lo + float(cfg.get("extreme_proximity_pts", 12)):
        strength = min(1.0, (swing_lo - spot) / max(buf * 2, 1))
        return {
            "event": "BREAKDOWN",
            "prefer_side": "PE",
            "reason": f"breakdown below {swing_lo:.0f} ({spot - swing_lo:.0f})",
            "strength": round(strength, 3),
            "level": swing_lo,
        }
    return {"event": None, "prefer_side": None, "reason": "", "strength": 0.0}


def premium_confirms_side(
    side: str,
    premium_hist: list[float],
    *,
    min_slope: float = 1.5,
    lookback: int = 5,
) -> bool:
    """Require the option premium itself to firm in entry direction (anti IV-crush)."""
    if len(premium_hist) < lookback + 1:
        return True  # not enough data — don't block cold start forever
    slope = float(premium_hist[-1]) - float(premium_hist[-(lookback + 1)])
    if side in ("CE", "PE"):
        return slope >= min_slope
    return True


def stop_loss_pct(cfg: dict[str, Any]) -> float:
    """Fractional stop distance (default 5%)."""
    return max(0.01, float(cfg.get("stop_loss_pct", 5.0)) / 100.0)


def target_pct_band(cfg: dict[str, Any]) -> tuple[float, float]:
    """(min_take_profit_frac, max_take_profit_frac) — default 5% .. 50%."""
    lo = max(0.01, float(cfg.get("target_pct_low", 5.0)) / 100.0)
    hi = max(lo, float(cfg.get("target_pct_high", 50.0)) / 100.0)
    return lo, hi


def _cfg_capital(cfg: dict[str, Any], *, algo: str | None = None) -> float:
    """Resolve capital for an algo book; falls back to shared capital_inr."""
    base = float(cfg.get("capital_inr") or 200_000)
    if not algo:
        return base
    key = {
        "algo1": "algo1_capital_inr",
        "algo1_sx": "algo1_capital_inr",
        "algo_cw": "algo_cw_capital_inr",
        "algo_nw": "algo_nw_capital_inr",
    }.get(algo)
    if not key:
        return base
    raw = cfg.get(key)
    if raw is None and algo == "algo_nw":
        raw = cfg.get("algo_cw_capital_inr")
    return float(raw if raw is not None else base)


def _cfg_deploy(cfg: dict[str, Any], *, algo: str | None = None) -> float:
    base = float(cfg.get("deploy_pct_of_balance") or 0.9)
    if not algo:
        return base
    key = {
        "algo1": "algo1_deploy_pct",
        "algo1_sx": "algo1_deploy_pct",
        "algo_cw": "algo_cw_deploy_pct",
        "algo_nw": "algo_nw_deploy_pct",
    }.get(algo)
    if not key:
        return base
    raw = cfg.get(key)
    if raw is None and algo == "algo_nw":
        raw = cfg.get("algo_cw_deploy_pct")
    return float(raw if raw is not None else base)


def size_dual_pair_lots(
    cfg: dict[str, Any], *, ce_entry: float, pe_entry: float, algo: str | None = None
) -> int:
    """
    Equal lots on CE+PE from capital × deploy / ((CE+PE) × lot_size).
    Matches dual 70–80 backtests (e.g. ₹2L × 90%).
    """
    capital = _cfg_capital(cfg, algo=algo)
    lot_size = int(cfg.get("lot_size", 75))
    max_lots = int(cfg.get("max_lots_per_trade", 20))
    min_lots = int(cfg.get("min_lots_per_trade", 1))
    deploy = _cfg_deploy(cfg, algo=algo)
    budget = capital * min(0.95, max(0.50, deploy))
    pair_cost = (float(ce_entry) + float(pe_entry)) * lot_size
    lots = int(budget // max(pair_cost, 1.0))
    return max(min_lots, min(max_lots, max(lots, min_lots if lots == 0 else lots)))


def size_lots(
    cfg: dict[str, Any],
    *,
    stop_points: float | None = None,
    entry: float | None = None,
    algo: str | None = None,
) -> int:
    """
    Lot sizing for live/paper.
    Prefer deploy_pct_of_balance × capital ÷ (premium × lot_size) when entry known
    (matches multi_1l_d75 aggressive ~70–80% deploy). Else fall back to risk %.
    """
    capital = _cfg_capital(cfg, algo=algo)
    lot_size = int(cfg.get("lot_size", 75))
    max_lots = int(cfg.get("max_lots_per_trade", 4))
    min_lots = int(cfg.get("min_lots_per_trade", 1))

    deploy = _cfg_deploy(cfg, algo=algo) if algo else float(cfg.get("deploy_pct_of_balance", 0) or 0)
    if deploy > 0 and entry is not None and float(entry) > 0:
        # Auto pack: spend ~70–80% of capital; allow enough lots to fill the budget
        budget = capital * min(0.95, max(0.50, deploy))
        per_lot_prem = float(entry) * lot_size
        lots = int(budget // max(per_lot_prem, 1.0))
        # Respect the configured cap; the old max(...,20) silently overrode it.
        return max(min_lots, min(max_lots, max(lots, min_lots if lots == 0 else lots)))

    risk_pct = float(cfg.get("risk_per_trade_pct", 1.0)) / 100.0
    if stop_points is not None:
        stop_pts = float(stop_points)
    elif cfg.get("use_percent_exits", True) and entry is not None:
        stop_pts = float(entry) * stop_loss_pct(cfg)
    elif cfg.get("use_percent_exits", True):
        mid = (float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))) / 2.0
        stop_pts = mid * stop_loss_pct(cfg)
    else:
        stop_pts = float(cfg.get("stop_loss_points", 18))
    risk_budget = capital * risk_pct
    per_lot = max(stop_pts * lot_size, 1.0)
    lots = int(risk_budget // per_lot)
    return max(min_lots, min(max_lots, lots))


def stop_price(entry: float, cfg: dict[str, Any]) -> float:
    """
    Stop under entry.
    Percent mode (default): entry * (1 − stop_loss_pct).
    Legacy: entry − points, floored by absolute stop_loss.
    """
    entry = float(entry)
    if cfg.get("use_percent_exits", True):
        return round(entry * (1.0 - stop_loss_pct(cfg)), 2)
    pts = float(cfg.get("stop_loss_points", 18))
    abs_floor = float(cfg.get("stop_loss", 100))
    return max(abs_floor, entry - pts)


def target_price(entry: float, cfg: dict[str, Any], *, trail: bool = False) -> float:
    """
    Initial / stretch target above entry.
    Percent mode: entry * (1 + target_pct_low) ; trail stretch uses target_pct_high.
    """
    entry = float(entry)
    if cfg.get("use_percent_exits", True):
        lo, hi = target_pct_band(cfg)
        pct = hi if trail else lo
        return round(entry * (1.0 + pct), 2)
    pts = float(cfg.get("target_points", 35))
    hard = float(cfg.get("target_high" if trail else "target_low", 169))
    by_pts = entry + pts
    return max(hard, by_pts) if trail else max(float(cfg.get("target_low", 169)), by_pts)


def entry_min_hold_minutes(cfg: dict[str, Any], side: str | None = None) -> float:
    """
    Minimum hold before soft exits.

    Learned pace: CE markets grind slowly → longer hold; PE dumps fast → shorter.
    """
    base = max(0.0, float(cfg.get("entry_min_hold_minutes", 5)))
    if not cfg.get("side_pace_rule_enabled", True):
        return base
    s = (side or "").upper()
    if s == "CE":
        return max(base, float(cfg.get("ce_entry_min_hold_minutes", 8)))
    if s == "PE":
        return max(0.0, float(cfg.get("pe_entry_min_hold_minutes", 3)))
    return base


def entry_hold_loss_points(
    cfg: dict[str, Any],
    confidence: float | None = None,
    side: str | None = None,
) -> float:
    """
    Adverse premium points that may exit during the min-hold window.

    Higher confidence → closer to the full budget (more patience).
    Lower confidence → tighter early cut.
    CE gets a wider noise budget (slow grind); PE cuts tighter (fast fall).
    """
    base = max(1.0, float(cfg.get("entry_min_hold_loss_points", 10)))
    low_pts = max(1.0, float(cfg.get("entry_min_hold_loss_points_low_conf", 7)))
    if cfg.get("side_pace_rule_enabled", True):
        s = (side or "").upper()
        if s == "CE":
            base = max(base, float(cfg.get("ce_entry_min_hold_loss_points", 12)))
            low_pts = max(low_pts, float(cfg.get("ce_entry_min_hold_loss_points_low_conf", 9)))
        elif s == "PE":
            base = min(base, float(cfg.get("pe_entry_min_hold_loss_points", 8)))
            low_pts = min(low_pts, float(cfg.get("pe_entry_min_hold_loss_points_low_conf", 6)))
    high = float(cfg.get("entry_hold_high_confidence", 0.72))
    low = float(cfg.get("entry_hold_low_confidence", 0.55))
    conf = float(confidence) if confidence is not None else (high + low) / 2.0
    if conf >= high:
        return round(base, 2)
    if conf <= low:
        return round(low_pts, 2)
    # Linear blend between low_pts and base
    span = max(1e-6, high - low)
    t = (conf - low) / span
    return round(low_pts + t * (base - low_pts), 2)


def side_trail_giveback_pct(cfg: dict[str, Any], side: str | None = None) -> float:
    """CE trails looser (slow grind); PE trails tighter (fast fall)."""
    base = float(cfg.get("trail_giveback_pct", 4.0))
    if not cfg.get("side_pace_rule_enabled", True):
        return base
    s = (side or "").upper()
    if s == "CE":
        return float(cfg.get("ce_trail_giveback_pct", max(base, 5.5)))
    if s == "PE":
        return float(cfg.get("pe_trail_giveback_pct", min(base, 3.0)))
    return base


def entry_hold_gate(
    *,
    entry: float,
    px: float,
    held_minutes: float,
    cfg: dict[str, Any],
    confidence: float | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    """
    Every entry: wait at least entry_min_hold_minutes for soft exits, or allow
    an early cut once premium is down entry_hold_loss_points (confidence-scaled).
    Side pace: CE patient, PE sharp.
    """
    entry = float(entry or 0)
    px = float(px or 0)
    min_hold = entry_min_hold_minutes(cfg, side)
    loss_pts = entry_hold_loss_points(cfg, confidence, side)
    adverse = (entry - px) if entry > 0 else 0.0
    in_hold = held_minutes < min_hold
    hard = entry > 0 and adverse >= loss_pts
    return {
        "min_hold_minutes": min_hold,
        "loss_points": loss_pts,
        "adverse_points": round(adverse, 2),
        "in_hold": in_hold,
        "allow_soft_exit": (not in_hold) or hard,
        "hard_loss_exit": hard,
        "side": (side or "").upper() or None,
        "confidence": None if confidence is None else round(float(confidence), 3),
        "reason": (
            f"hold loss −{adverse:.1f}pt ({(side or '?').upper()} gate {loss_pts:g})"
            if hard
            else ""
        ),
    }


def apply_entry_hold_gate(
    decision: dict[str, Any],
    *,
    entry: float,
    px: float,
    held_minutes: float,
    cfg: dict[str, Any],
    confidence: float | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    """Suppress soft exits inside the min-hold; always honor confidence loss pts."""
    gate = entry_hold_gate(
        entry=entry,
        px=px,
        held_minutes=held_minutes,
        cfg=cfg,
        confidence=confidence,
        side=side,
    )
    out = dict(decision or {})
    out["hold_gate"] = gate
    if gate["hard_loss_exit"]:
        out["action"] = "exit"
        out["reason"] = gate["reason"]
        return out
    if out.get("action") == "exit" and gate["in_hold"] and not gate["allow_soft_exit"]:
        out["action"] = "hold"
        out["reason"] = (
            f"min hold {gate['min_hold_minutes']:g}m "
            f"(need −{gate['loss_points']:g}pt or wait)"
        )
    return out


def manage_percent_exit(
    *,
    entry: float,
    px: float,
    peak: float,
    cfg: dict[str, Any],
    held_minutes: float = 0.0,
    confidence: float | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    """
    Percent risk manager for long premium:
      − hard stop at −stop_loss_pct (default 5%)
      − once +target_pct_low (5%), lock stop to +lock_profit_pct
      − trail stop under peak by trail_giveback_pct of entry
      − hard take-profit at +target_pct_high (50%)
      − time-stop only while still red
      − first entry_min_hold_minutes: soft exits blocked unless −loss_points
      − CE slow-grind / PE fast-fall side pace (hold + trail)

    Returns {action: hold|exit, reason, stop_px, target_px, peak, mult}.
    """
    entry = float(entry)
    px = float(px)
    peak = max(float(peak), px, entry)
    mult = px / entry if entry else 1.0
    peak_mult = peak / entry if entry else 1.0
    gate = entry_hold_gate(
        entry=entry,
        px=px,
        held_minutes=held_minutes,
        cfg=cfg,
        confidence=confidence,
        side=side,
    )
    if gate["hard_loss_exit"]:
        sl = entry - float(gate["loss_points"])
        tg = target_price(entry, cfg)
        return {
            "action": "exit",
            "reason": gate["reason"],
            "stop_px": sl,
            "target_px": tg,
            "peak": peak,
            "mult": mult,
            "hold_gate": gate,
        }

    if not cfg.get("use_percent_exits", True):
        sl = stop_price(entry, cfg)
        tg = target_price(entry, cfg)
        # During min-hold, only the confidence loss-points stop may fire.
        if gate["in_hold"]:
            sl = min(float(sl), entry - float(gate["loss_points"]))
        if px >= tg and not gate["in_hold"]:
            return {"action": "exit", "reason": f"target {tg:g}", "stop_px": sl, "target_px": tg, "peak": peak, "mult": mult, "hold_gate": gate}
        if px <= sl:
            return {"action": "exit", "reason": f"stop {sl:g}", "stop_px": sl, "target_px": tg, "peak": peak, "mult": mult, "hold_gate": gate}
        time_stop_m = float(cfg.get("time_stop_minutes", 18))
        # Expiry gamma: cut losers faster (autopsy med hold ~8m)
        if cfg.get("treat_as_expiry") or cfg.get("is_expiry_session"):
            time_stop_m = min(
                time_stop_m, float(cfg.get("expiry_time_stop_minutes", 20))
            )
        if (
            not gate["in_hold"]
            and held_minutes >= time_stop_m
            and px < entry
        ):
            return {
                "action": "exit",
                "reason": f"time stop {time_stop_m:g}m fail",
                "stop_px": sl,
                "target_px": tg,
                "peak": peak,
                "mult": mult,
                "hold_gate": gate,
            }
        return {"action": "hold", "reason": "", "stop_px": sl, "target_px": tg, "peak": peak, "mult": mult, "hold_gate": gate}

    sl_pct = stop_loss_pct(cfg)
    lo_pct, hi_pct = target_pct_band(cfg)
    lock_pct = float(cfg.get("lock_profit_pct", 1.0)) / 100.0
    giveback = side_trail_giveback_pct(cfg, side) / 100.0
    # Once armed, trail stop sits giveback below peak (as fraction of entry)
    hard_sl = entry * (1.0 - sl_pct)
    hard_tg = entry * (1.0 + hi_pct)
    first_tg = entry * (1.0 + lo_pct)

    if peak_mult >= 1.0 + lo_pct:
        # Locked floor after first target, then trail under peak
        locked = entry * (1.0 + lock_pct)
        trail_sl = peak - (entry * giveback)
        sl = max(hard_sl, locked, trail_sl)
    else:
        sl = hard_sl

    tg = hard_tg  # stretch ceiling; first target arms trail rather than flat exit

    # Early-bar grace: wider catastrophic stop only (avoids 1-minute noise stops)
    grace_m = float(cfg.get("stop_grace_minutes", 0))
    grace_sl_pct = float(cfg.get("stop_grace_pct", 8.0)) / 100.0
    in_grace = grace_m > 0 and held_minutes < grace_m and peak_mult < 1.0 + lo_pct
    if in_grace:
        sl = entry * (1.0 - max(sl_pct, grace_sl_pct))
    # Min-hold: ignore tight %-stops until −loss_points (confidence-scaled)
    if gate["in_hold"]:
        sl = min(float(sl), entry - float(gate["loss_points"]))

    # Prefer banking at first target if configured (scalp mode)
    if (
        cfg.get("exit_at_first_target", False)
        and px >= first_tg
        and not gate["in_hold"]
    ):
        return {
            "action": "exit",
            "reason": f"first target +{lo_pct*100:.0f}%",
            "stop_px": sl,
            "target_px": first_tg,
            "peak": peak,
            "mult": mult,
            "hold_gate": gate,
        }

    if px >= hard_tg and not gate["in_hold"]:
        return {
            "action": "exit",
            "reason": f"max target +{hi_pct*100:.0f}%",
            "stop_px": sl,
            "target_px": hard_tg,
            "peak": peak,
            "mult": mult,
            "hold_gate": gate,
        }
    if px <= sl:
        tag = "trail stop" if peak_mult >= 1.0 + lo_pct and sl > hard_sl + 0.01 else "stop"
        if in_grace and not gate["in_hold"]:
            tag = "grace stop"
        if gate["in_hold"]:
            tag = "hold loss"
        return {
            "action": "exit",
            "reason": f"{tag} {sl:g} ({(sl/entry - 1)*100:+.1f}%)",
            "stop_px": sl,
            "target_px": tg,
            "peak": peak,
            "mult": mult,
            "hold_gate": gate,
        }

    time_stop_m = float(cfg.get("time_stop_minutes", 25))
    if cfg.get("treat_as_expiry") or cfg.get("is_expiry_session"):
        time_stop_m = min(
            time_stop_m, float(cfg.get("expiry_time_stop_minutes", 20))
        )
    # Don't time-stop once we've armed the trail (in profit territory)
    if (
        not gate["in_hold"]
        and held_minutes >= time_stop_m
        and px < entry
        and peak_mult < 1.0 + lo_pct
    ):
        return {
            "action": "exit",
            "reason": f"time stop {time_stop_m:g}m fail",
            "stop_px": sl,
            "target_px": tg,
            "peak": peak,
            "mult": mult,
            "hold_gate": gate,
        }

    return {
        "action": "hold",
        "reason": "",
        "stop_px": round(sl, 2),
        "target_px": round(tg, 2),
        "peak": peak,
        "mult": round(mult, 4),
        "hold_gate": gate,
    }


def daily_loss_halted(day_pnl: float, cfg: dict[str, Any]) -> bool:
    capital = float(cfg.get("capital_inr", 200_000))
    max_dd = float(cfg.get("max_daily_loss_pct", 2.0) or 0) / 100.0
    if max_dd <= 0:
        return False  # 0 / missing = circuit disabled
    return day_pnl <= -(capital * max_dd)


def period_loss_halted(period_pnl: float, cfg: dict[str, Any], *, pct_key: str) -> bool:
    """True when period PnL has breached -capital × pct_key (e.g. max_weekly_loss_pct)."""
    capital = float(cfg.get("capital_inr", 200_000))
    max_dd = float(cfg.get(pct_key, 0) or 0) / 100.0
    if max_dd <= 0:
        return False
    return period_pnl <= -(capital * max_dd)


def monthly_target_reached(month_pnl: float, cfg: dict[str, Any]) -> bool:
    """Optional: stop new risk once monthly profit target is hit (bank the month)."""
    if not cfg.get("bank_month_at_target", False):
        return False
    target = float(cfg.get("monthly_profit_target_inr", 0) or 0)
    return target > 0 and month_pnl >= target


def evaluate_entry(
    *,
    side: str,
    conf: float,
    closes: list[float],
    premium_hist: list[float],
    trend: dict[str, Any],
    cfg: dict[str, Any],
    day_pnl: float = 0.0,
    losses_today: int = 0,
    last_loss_side: str | None = None,
    setups_today: int = 0,
) -> dict[str, Any]:
    """
    Gate a proposed entry. Returns {ok, lots, reason, stop, target}.

    Allows multiple entries/day (up to max_trades_per_day) when market shows
    breakout / breakdown / shakeout / strong session trend — not one-and-done.
    """
    reasons: list[str] = []
    if daily_loss_halted(day_pnl, cfg):
        return {"ok": False, "lots": 0, "reason": "daily loss circuit open", "stop": None, "target": None}

    max_setups = int(cfg.get("max_trades_per_day", 15))
    if setups_today >= max_setups:
        return {
            "ok": False,
            "lots": 0,
            "reason": f"day setup cap {max_setups}",
            "stop": None,
            "target": None,
        }

    mins = minutes_from_open(closes)
    shake = detect_opening_shakeout(closes, cfg=cfg)
    brk = detect_breakout_breakdown(closes, cfg=cfg)
    delay = int(cfg.get("entry_delay_minutes", 12))
    prefer_after = int(cfg.get("prefer_entry_after_minutes", 20))
    vs_open_early = trend.get("vs_open")
    explosive = vs_open_early is not None and abs(float(vs_open_early)) >= float(
        cfg.get("trend_hard_pts", 70)
    ) * float(cfg.get("explosive_mult", 1.6))
    structure_ok = bool(
        (brk.get("event") and brk.get("prefer_side") == side)
        or (shake.get("active") and shake.get("prefer_side") == side)
        or explosive
        or (trend.get("block_counter_trend") and trend.get("prefer_side") == side)
    )

    if mins < delay:
        if not (
            structure_ok
            and mins >= max(8, delay // 2)
        ):
            return {
                "ok": False,
                "lots": 0,
                "reason": f"open buffer {mins}/{delay}m",
                "stop": None,
                "target": None,
            }

    # Early session: need breakout/breakdown/explosive — otherwise wait prefer_after
    if mins < prefer_after and not structure_ok:
        return {
            "ok": False,
            "lots": 0,
            "reason": f"wait structure/break until +{prefer_after}m (now {mins}m)",
            "stop": None,
            "target": None,
        }

    if shake.get("active") and shake.get("block_side") == side:
        return {
            "ok": False,
            "lots": 0,
            "reason": shake.get("reason") or "shakeout block",
            "stop": None,
            "target": None,
        }

    # Re-entries (2nd+) must ride a fresh breakout/breakdown or strong trend
    if setups_today >= 1 and cfg.get("reentry_needs_break", True):
        if brk.get("prefer_side") == side and brk.get("event"):
            reasons.append(str(brk.get("reason")))
        elif trend.get("prefer_side") == side and float(trend.get("strength") or 0) >= 0.55:
            reasons.append(f"re-entry on session {trend.get('bias')}")
        elif shake.get("active") and shake.get("prefer_side") == side:
            reasons.append(str(shake.get("reason")))
        else:
            return {
                "ok": False,
                "lots": 0,
                "reason": "re-entry needs breakout/breakdown or strong trend",
                "stop": None,
                "target": None,
            }
    elif brk.get("prefer_side") == side and brk.get("event"):
        reasons.append(str(brk.get("reason")))

    # Early hard trend often = stop-hunt: require hold beyond confirm minutes
    confirm_m = int(cfg.get("trend_confirm_minutes", 20))
    if mins < confirm_m and trend.get("block_counter_trend"):
        # only trust hard bias if last 8m still on that side of open by soft pts
        soft = float(cfg.get("trend_soft_pts", 35))
        open_px = float(closes[0])
        recent = closes[-min(8, len(closes)) :]
        prefer = trend.get("prefer_side")
        if prefer == "PE" and min(recent) > open_px - soft * 0.5:
            return {
                "ok": False,
                "lots": 0,
                "reason": "early dump not held — wait (anti fake PE)",
                "stop": None,
                "target": None,
            }
        if prefer == "CE" and max(recent) < open_px + soft * 0.5 and mins < confirm_m:
            # CE early is ok if reclaiming; PE already blocked above
            pass

    chop = chop_score(closes, cfg)
    if chop.get("is_chop") and float(trend.get("strength") or 0) < 0.55:
        return {
            "ok": False,
            "lots": 0,
            "reason": f"chop skip ({chop.get('reason')})",
            "stop": None,
            "target": None,
        }

    min_conf = float(cfg.get("algo1_min_confidence", 0.52))
    if losses_today >= 1:
        min_conf = max(min_conf, float(cfg.get("post_loss_min_confidence", 0.62)))
    if losses_today >= 2:
        min_conf = max(min_conf, float(cfg.get("post_loss_min_confidence_2", 0.70)))
    if last_loss_side and last_loss_side == side and losses_today >= 1:
        # Same-side re-entry after stop: allow on fresh breakout/breakdown
        if not (
            (brk.get("event") and brk.get("prefer_side") == side)
            or float(trend.get("strength") or 0) >= 0.55
        ):
            return {
                "ok": False,
                "lots": 0,
                "reason": f"no re-buy {side} after stop without break/trend",
                "stop": None,
                "target": None,
            }

    if conf < min_conf:
        return {
            "ok": False,
            "lots": 0,
            "reason": f"conf {conf:.0%} < {min_conf:.0%}",
            "stop": None,
            "target": None,
        }

    slope_need = float(cfg.get("premium_confirm_slope", 1.5))
    if cfg.get("require_premium_confirm", True) and mins >= delay:
        if not premium_confirms_side(side, premium_hist, min_slope=slope_need):
            return {
                "ok": False,
                "lots": 0,
                "reason": "premium not firming with side (IV/bleed guard)",
                "stop": None,
                "target": None,
            }

    # Don't buy "dip" into a still-falling 10m tape — wait for turn (anti stop-hunt)
    pullback_ok = float(cfg.get("trend_pullback_ok_pts", 25))
    lookback = int(cfg.get("trend_momentum_minutes", 10))
    if len(closes) > lookback:
        mom = float(closes[-1]) - float(closes[-(lookback + 1)])
        if side == "CE" and mom <= -pullback_ok and not (
            shake.get("active") and shake.get("prefer_side") == "CE"
        ):
            return {
                "ok": False,
                "lots": 0,
                "reason": f"spot still dumping {mom:.0f}/{lookback}m — wait turn for CE",
                "stop": None,
                "target": None,
            }
        if side == "PE" and mom >= pullback_ok and not (
            shake.get("active") and shake.get("prefer_side") == "PE"
        ):
            return {
                "ok": False,
                "lots": 0,
                "reason": f"spot still bouncing +{mom:.0f}/{lookback}m — wait turn for PE",
                "stop": None,
                "target": None,
            }

    # Breakout confirmation: CE only near session high, PE near session low
    # (stops buying mid-range while institutions reload)
    if len(closes) >= 20 and cfg.get("require_session_extreme", True):
        spot = float(closes[-1])
        hi = max(closes)
        lo = min(closes)
        prox = float(cfg.get("extreme_proximity_pts", 12))
        if side == "CE" and spot < hi - prox:
            return {
                "ok": False,
                "lots": 0,
                "reason": f"CE wait — spot {spot:.0f} not near day high {hi:.0f}",
                "stop": None,
                "target": None,
            }
        if side == "PE" and spot > lo + prox:
            return {
                "ok": False,
                "lots": 0,
                "reason": f"PE wait — spot {spot:.0f} not near day low {lo:.0f}",
                "stop": None,
                "target": None,
            }

    if shake.get("active") and shake.get("prefer_side") == side:
        reasons.append(str(shake.get("reason")))

    lots = size_lots(cfg)
    # Soft loss budget: shrink size, hard halt only at high streak / daily circuit
    halt_n = int(cfg.get("max_losses_before_halt", 8))
    if losses_today >= halt_n:
        return {
            "ok": False,
            "lots": 0,
            "reason": f"loss streak halt ({losses_today}/{halt_n})",
            "stop": None,
            "target": None,
        }
    if losses_today >= 2:
        lots = max(1, lots // 2)
    if losses_today >= 4:
        lots = 1

    # Directional edge vs open — softer when breakout/breakdown confirms
    # loose_entry_mode: use ce/pe_min_vs_open floors only (no soft/hard 35/70 lockout)
    hard = float(cfg.get("trend_hard_pts", 70))
    soft = float(cfg.get("trend_soft_pts", 35))
    vs_open = trend.get("vs_open")
    structure_side = bool(
        (shake.get("active") and shake.get("prefer_side") == side)
        or (brk.get("event") and brk.get("prefer_side") == side)
    )
    if vs_open is not None and not cfg.get("loose_entry_mode", False):
        vo = float(vs_open)
        if side == "CE":
            need = soft * 0.6 if structure_side else soft
            if not structure_side and float(trend.get("strength") or 0) < 0.45:
                need = hard
            if vo < need:
                return {
                    "ok": False,
                    "lots": 0,
                    "reason": f"CE needs spot ≥+{need:.0f} vs open (now {vo:+.0f})",
                    "stop": None,
                    "target": None,
                }
        if side == "PE":
            need = soft * 0.6 if structure_side else soft
            if not structure_side and float(trend.get("strength") or 0) < 0.45:
                need = hard
            if vo > -need:
                return {
                    "ok": False,
                    "lots": 0,
                    "reason": f"PE needs spot ≤−{need:.0f} vs open (now {vo:+.0f})",
                    "stop": None,
                    "target": None,
                }
    elif vs_open is not None and cfg.get("loose_entry_mode", False):
        vo = float(vs_open)
        ce_need = float(cfg.get("ce_min_vs_open", 15))
        pe_need = float(cfg.get("pe_min_vs_open", 15))
        if side == "CE" and vo < ce_need:
            return {
                "ok": False,
                "lots": 0,
                "reason": f"CE needs spot ≥+{ce_need:.0f} vs open (now {vo:+.0f})",
                "stop": None,
                "target": None,
            }
        if side == "PE" and vo > -pe_need:
            return {
                "ok": False,
                "lots": 0,
                "reason": f"PE needs spot ≤−{pe_need:.0f} vs open (now {vo:+.0f})",
                "stop": None,
                "target": None,
            }

    # Optional bank-after-win (default OFF — allow multiple winners)
    if int(cfg.get("bank_after_win", 0)) and day_pnl > 0 and losses_today == 0:
        return {
            "ok": False,
            "lots": 0,
            "reason": "banked — day locked after win",
            "stop": None,
            "target": None,
        }
    entry_px = float(premium_hist[-1]) if premium_hist else 135.0
    return {
        "ok": True,
        "lots": lots,
        "reason": "; ".join(reasons) if reasons else "entry cleared",
        "stop": stop_price(entry_px, cfg),
        "target": target_price(entry_px, cfg, trail=float(trend.get("strength") or 0) >= 0.45),
        "break_event": brk.get("event"),
    }

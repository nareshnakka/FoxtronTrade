"""
Live market-direction analysis → one side only (CE or PE).

No convergence, no dual legs, no CE/PE premium merge.
Pure spot structure: trend, breakout, breakdown, shakeout.
Calibrated for high-selectivity: trade only clear directional days.
"""

from __future__ import annotations

from typing import Any

from foxtrade.expert_filters import (
    chop_score,
    detect_breakout_breakdown,
    detect_opening_shakeout,
    detect_sr_range_entry,
    minutes_from_open,
    orb_break_already_failed,
    orb_broke_without_retest,
)
from foxtrade.session_trend import compute_session_trend


def _sensex_expiry_gamma_overlay(
    cfg: dict[str, Any],
    *,
    is_expiry_day: bool,
) -> dict[str, Any]:
    """
    Sensex expiry / Thu-gamma sessions: allow ORB blast entries.
    Kept off on normal Wed so the quiet SR book is preserved.
    """
    if str(cfg.get("underlying") or "").upper() != "SENSEX":
        return cfg
    if not (is_expiry_day or cfg.get("treat_as_expiry") or cfg.get("is_expiry_session")):
        return cfg
    if cfg.get("sensex_expiry_orb_gamma", True) is False:
        return cfg
    # ORB on for expiry/gamma — but keep retest gate (naked chase is -EV).
    return {
        **cfg,
        "enable_orb_breakout": True,
        "enable_orb_breakdown": True,
        "min_break_strength": float(
            cfg.get("sensex_expiry_min_break_strength", 0.22)
        ),
        "block_orb_chase_without_retest": bool(
            cfg.get("sensex_expiry_require_orb_retest", True)
        ),
        "block_orb_after_failed_break": bool(
            cfg.get("sensex_expiry_block_failed_orb", True)
        ),
        "soft_vs_open_on_strong_break": True,
        "expiry_min_entry_minutes": int(
            cfg.get(
                "sensex_expiry_gamma_min_entry_minutes",
                min(45, int(cfg.get("expiry_min_entry_minutes", 75))),
            )
        ),
        "expiry_no_entry_after_minutes": int(
            cfg.get(
                "sensex_expiry_gamma_no_entry_after",
                max(210, int(cfg.get("expiry_no_entry_after_minutes", 200))),
            )
        ),
        "expiry_min_mom": float(
            cfg.get(
                "sensex_expiry_gamma_min_mom",
                min(45.0, float(cfg.get("expiry_min_mom", 59) or 59)),
            )
        ),
    }


def analyze_market(
    closes: list[float],
    *,
    cfg: dict[str, Any] | None = None,
    ema9: float | None = None,
    ema21: float | None = None,
    is_expiry_day: bool = False,
) -> dict[str, Any]:
    """
    Return one-sided trade bias from spot path only.

    {side: CE|PE|None, confidence: 0..1, reason, event, allow_entry}
    """
    cfg = _sensex_expiry_gamma_overlay(cfg or {}, is_expiry_day=is_expiry_day)
    if len(closes) < 8:
        return {
            "side": None,
            "confidence": 0.0,
            "reason": "warming up",
            "event": None,
            "allow_entry": False,
        }

    day_open = float(closes[0])
    spot = float(closes[-1])
    trend = compute_session_trend(
        spot=spot, day_open=day_open, closes=closes, ema9=ema9, ema21=ema21, cfg=cfg
    )
    brk = detect_breakout_breakdown(closes, cfg=cfg)
    shake = detect_opening_shakeout(closes, cfg=cfg)
    sr = detect_sr_range_entry(closes, cfg=cfg)
    chop = chop_score(closes, cfg)
    mins = minutes_from_open(closes)
    delay = int(cfg.get("entry_delay_minutes", 12))
    prefer_after = int(cfg.get("prefer_entry_after_minutes", 25))
    if str(cfg.get("entry_style", "orb")).lower() == "orb":
        prefer_after = max(prefer_after, int(cfg.get("orb_minutes", 45)) + 2)
    # Hard session cut. Prefer_sr keeps window open to sr_no_entry so a 1-bar
    # SR flicker after +195m does not abort persist (Jul24 13:51 win). Cap at
    # sr_no_entry (300 ≈ 14:15) to block Nov2 14:45 late knife.
    session_end_cut = int(cfg.get("no_entry_after_minutes", 195))
    if cfg.get("prefer_sr_dip_entry", True) and cfg.get("enable_sr_reclaim", True):
        session_end_cut = max(
            session_end_cut, int(cfg.get("sr_no_entry_after_minutes", 300))
        )

    soft = float(cfg.get("trend_soft_pts", 35))
    hard = float(cfg.get("trend_hard_pts", 70))
    vs_open = spot - day_open
    look = int(cfg.get("trend_momentum_minutes", 10))
    mom = spot - float(closes[-(look + 1)]) if len(closes) > look else 0.0

    # Late-day / expiry: only allow explosive continuation
    if mins >= session_end_cut:
        return {
            "side": None,
            "confidence": 0.0,
            "reason": f"no new entries after +{session_end_cut}m",
            "event": None,
            "allow_entry": False,
            "trend": trend,
            "break": brk,
        }

    # Range-bound train: chop days — do NOT take CE SR/breakout (fake structure).
    # Only hard PE breakdown at the low may proceed.
    if chop.get("is_chop") and float(trend.get("strength") or 0) < 0.55:
        _lo = min(closes) if closes else spot
        _prox = float(cfg.get("extreme_proximity_pts", 12))
        pe_chop_ok = (
            cfg.get("allow_pe_breakdown_in_chop", True)
            and brk.get("event") == "BREAKDOWN"
            and vs_open <= -hard
            and spot <= _lo + _prox
        )
        if cfg.get("block_structure_in_chop", True) and not pe_chop_ok:
            return {
                "side": None,
                "confidence": 0.0,
                "reason": f"chop — no trade ({chop.get('reason')})",
                "event": "CHOP",
                "allow_entry": False,
                "trend": trend,
                "break": brk,
            }
        if (
            not cfg.get("block_structure_in_chop", True)
            and not brk.get("event")
            and not sr.get("active")
        ):
            return {
                "side": None,
                "confidence": 0.0,
                "reason": f"chop — no trade ({chop.get('reason')})",
                "event": "CHOP",
                "allow_entry": False,
                "trend": trend,
                "break": brk,
            }

    score_ce = 0.0
    score_pe = 0.0
    tags: list[str] = []

    # Breakout / breakdown dominate
    if brk.get("event") == "BREAKOUT":
        score_ce += 0.50 + 0.25 * float(brk.get("strength") or 0)
        tags.append(str(brk.get("reason")))
    elif brk.get("event") == "BREAKDOWN":
        score_pe += 0.50 + 0.25 * float(brk.get("strength") or 0)
        tags.append(str(brk.get("reason")))

    # Session trend
    if trend.get("prefer_side") == "CE":
        score_ce += 0.22 + 0.22 * float(trend.get("strength") or 0)
        tags.append(f"session UP {trend.get('reason')}")
    elif trend.get("prefer_side") == "PE":
        score_pe += 0.22 + 0.22 * float(trend.get("strength") or 0)
        tags.append(f"session DOWN {trend.get('reason')}")

    # Shakeout (institutional fake)
    if shake.get("active") and shake.get("prefer_side") == "CE":
        score_ce += 0.38
        score_pe -= 0.25
        tags.append(str(shake.get("reason")))
    elif shake.get("active") and shake.get("prefer_side") == "PE":
        score_pe += 0.38
        score_ce -= 0.25
        tags.append(str(shake.get("reason")))

    # Range / S/R dip-reclaim (UP-day buy support, DOWN-day fade resistance)
    if sr.get("active") and sr.get("prefer_side") == "CE":
        score_ce += 0.48 + 0.2 * float(sr.get("strength") or 0)
        score_pe -= 0.3
        tags.append(str(sr.get("reason")))
    elif sr.get("active") and sr.get("prefer_side") == "PE":
        score_pe += 0.48 + 0.2 * float(sr.get("strength") or 0)
        score_ce -= 0.3
        tags.append(str(sr.get("reason")))

    # Short momentum MUST align with side (anti-fade into knife)
    mom_ok_ce = mom >= soft * 0.25
    mom_ok_pe = mom <= -soft * 0.25
    if mom_ok_ce:
        score_ce += 0.15
        tags.append(f"mom +{mom:.0f}/{look}m")
    elif mom_ok_pe:
        score_pe += 0.15
        tags.append(f"mom {mom:.0f}/{look}m")

    # EMA stack
    if ema9 is not None and ema21 is not None:
        if ema9 > ema21 and vs_open > 0:
            score_ce += 0.08
            tags.append("EMA bull")
        elif ema9 < ema21 and vs_open < 0:
            score_pe += 0.08
            tags.append("EMA bear")

    # Near day extreme confirmation
    day_hi, day_lo = max(closes), min(closes)
    prox = float(cfg.get("extreme_proximity_pts", 12))
    if spot >= day_hi - prox and vs_open >= soft * 0.5:
        score_ce += 0.12
        tags.append("near day high")
    if spot <= day_lo + prox and vs_open <= -soft * 0.5:
        score_pe += 0.12
        tags.append("near day low")

    # Range expansion vs first 30m — institutions pushing
    if len(closes) >= 35:
        early = closes[:30]
        early_rng = max(early) - min(early)
        day_rng = day_hi - day_lo
        if day_rng >= early_rng * float(cfg.get("range_expand_mult", 1.35)) and day_rng >= soft:
            if spot >= day_open + soft * 0.5:
                score_ce += 0.1
                tags.append("range expand UP")
            elif spot <= day_open - soft * 0.5:
                score_pe += 0.1
                tags.append("range expand DOWN")

    if score_ce <= 0 and score_pe <= 0:
        return {
            "side": None,
            "confidence": 0.0,
            "reason": "no directional edge",
            "event": None,
            "allow_entry": False,
            "trend": trend,
            "break": brk,
        }

    if score_ce >= score_pe:
        side, raw = "CE", score_ce
        other = score_pe
    else:
        side, raw = "PE", score_pe
        other = score_ce

    # Must clearly win — one side only, no coin-flip
    margin = raw - other
    conf = min(0.95, 0.42 + raw * 0.38 + margin * 0.22)
    min_conf = float(cfg.get("directional_min_confidence", 0.68))
    min_margin = float(cfg.get("directional_min_margin", 0.22))
    allow = conf >= min_conf and margin >= min_margin

    # Strict mode (calibrated): ORB / structure break required (shakeout alone is weak)
    min_brk = float(cfg.get("min_break_strength", 0.35))
    allow_shake_entry = bool(cfg.get("allow_shakeout_entry", False))
    sr_ok = bool(
        cfg.get("prefer_sr_dip_entry", True)
        and sr.get("active")
        and sr.get("prefer_side") == side
    )
    enable_breakout = bool(cfg.get("enable_orb_breakout", True))
    enable_breakdown = bool(cfg.get("enable_orb_breakdown", True))
    quality = bool(
        (
            enable_breakout
            and brk.get("event") == "BREAKOUT"
            and side == "CE"
            and float(brk.get("strength") or 0) >= min_brk
        )
        or (
            enable_breakdown
            and brk.get("event") == "BREAKDOWN"
            and side == "PE"
            and float(brk.get("strength") or 0) >= min_brk
        )
        or (allow_shake_entry and shake.get("active") and shake.get("prefer_side") == side)
        or sr_ok
    )
    if not cfg.get("require_structure_event", True):
        quality = quality or bool(
            trend.get("block_counter_trend")
            and trend.get("prefer_side") == side
            and abs(vs_open) >= hard
            and mins >= int(cfg.get("trend_confirm_minutes", 20))
        )
    if not quality:
        allow = False
        tags.append("wait structure (break/shake)")

    # N-bar continuation — reject one-tick fake breaks (stability: 5 bars)
    # S/R reclaim uses its own turn bars — skip strict ORB continuation
    cont_n = int(cfg.get("continue_bars", 3 if cfg.get("require_3bar_continue", True) else 0))
    if cont_n >= 2 and len(closes) >= cont_n + 1 and not sr_ok:
        window = [float(x) for x in closes[-(cont_n):]]
        if side == "CE":
            ok_cont = all(window[i] >= window[i - 1] - soft * 0.12 for i in range(1, len(window)))
            if not ok_cont:
                allow = False
                tags.append(f"CE needs {cont_n}-bar hold-up")
        if side == "PE":
            ok_cont = all(window[i] <= window[i - 1] + soft * 0.12 for i in range(1, len(window)))
            if not ok_cont:
                allow = False
                tags.append(f"PE needs {cont_n}-bar hold-down")

    # Momentum alignment hard gate (S/R uses local turn — allow softer mom)
    if cfg.get("require_momentum_align", True) and not sr_ok:
        if side == "CE" and not mom_ok_ce:
            allow = False
            tags.append("CE needs rising momentum")
        if side == "PE" and not mom_ok_pe:
            allow = False
            tags.append("PE needs falling momentum")

    # Post-break confirmation: stay beyond OR/structure for N minutes after event
    post_n = int(cfg.get("post_break_confirm_minutes", 0))
    if post_n > 0 and brk.get("event") and brk.get("prefer_side") == side and len(closes) >= post_n + 2:
        lvl = float(brk.get("level") or 0)
        recent = closes[-post_n:]
        if side == "CE" and min(recent) < lvl:
            allow = False
            tags.append(f"CE post-break not held {post_n}m")
        if side == "PE" and max(recent) > lvl:
            allow = False
            tags.append(f"PE post-break not held {post_n}m")

    # Stability autopsy (3mo): PE first-trades ~71% green; CE needs stronger |vs_open|;
    # mid-chase CE (10–45pts beyond OR) was ~17–25% win — block unless hard trend.
    if cfg.get("stability_mode", False):
        look15 = min(15, len(closes) - 1)
        # S/R reclaim uses local turn bars — don't demand 15m slope
        if look15 >= 8 and not sr_ok:
            slope15 = spot - float(closes[-(look15 + 1)])
            if side == "CE" and slope15 < soft * 0.15:
                allow = False
                tags.append("CE 15m slope weak")
            if side == "PE" and slope15 > -soft * 0.15:
                allow = False
                tags.append("PE 15m slope weak")
        if len(closes) >= 40 and not sr_ok:
            w = closes[-40:]
            eff = abs(w[-1] - w[0]) / max(max(w) - min(w), 1.0)
            if eff < float(cfg.get("min_path_efficiency", 0.28)):
                allow = False
                tags.append(f"path efficiency {eff:.2f} too choppy")
        # CE mid-chase beyond OR (learned: worst bucket)
        if (
            side == "CE"
            and brk.get("event") == "BREAKOUT"
            and cfg.get("block_ce_mid_chase", True)
            and not sr_ok
        ):
            orb_n = int(cfg.get("orb_minutes", 45))
            if len(closes) > orb_n + 2:
                or_hi = max(closes[:orb_n])
                beyond = spot - or_hi
                lo = float(cfg.get("ce_mid_chase_low", 12))
                hi = float(cfg.get("ce_mid_chase_high", 45))
                ce_hard = float(cfg.get("ce_min_vs_open", 85))
                if lo <= beyond <= hi and vs_open < ce_hard:
                    allow = False
                    tags.append(f"CE mid-chase beyond OR {beyond:.0f}pts")
                # Lesson Oct 6 2026: hard-UP late chase (+44 beyond OR) stopped
                # even when vs_open already hard — wait S/R dip instead.
                max_ext = float(cfg.get("breakout_max_extension_pts", 25))
                if (
                    beyond >= max_ext
                    and cfg.get("prefer_sr_dip_entry", True)
                ):
                    allow = False
                    tags.append(
                        f"CE breakout extended +{beyond:.0f} — wait S/R dip"
                    )

    # Session extreme gate (skip for S/R dip — we intentionally buy off the high).
    # Hard CE break may grind off the exact high. PE must stay near day low —
    # train May 2026: relax_extreme let PE buy bounce after crash (21/29 knives).
    hard_break = (
        (side == "CE" and brk.get("event") == "BREAKOUT" and vs_open >= hard)
        or (side == "PE" and brk.get("event") == "BREAKDOWN" and vs_open <= -hard)
    )
    relax_ext = hard_break and cfg.get("relax_extreme_on_hard_break", True)
    if side == "PE" and not cfg.get("relax_extreme_pe_on_hard_break", False):
        relax_ext = False
    if (
        cfg.get("require_session_extreme", True)
        and len(closes) >= 15
        and not sr_ok
        and not relax_ext
    ):
        if side == "CE" and spot < day_hi - prox:
            allow = False
            tags.append("CE not at day high")
        if side == "PE" and spot > day_lo + prox:
            allow = False
            tags.append("PE not at day low")

    # Block first ORB chase without retest (lesson 2026-11-02 morning stops)
    if (
        allow
        and not sr_ok
        and brk.get("event") in ("BREAKOUT", "BREAKDOWN")
        and brk.get("prefer_side") == side
        and orb_broke_without_retest(closes, side=side, cfg=cfg)
    ):
        allow = False
        tags.append("ORB chase w/o retest — wait S/R dip")

    # After a failed ORB (broke then back inside), don't re-chase — wait S/R.
    # Hard-trend rebreak only when that side's S/R fallback is OFF
    # (Aug 2026: PE dead-ended because enable_sr_reject=False).
    if (
        allow
        and not sr_ok
        and brk.get("event") in ("BREAKOUT", "BREAKDOWN")
        and brk.get("prefer_side") == side
        and orb_break_already_failed(closes, side=side, cfg=cfg)
    ):
        sr_fallback_on = bool(cfg.get("prefer_sr_dip_entry", True)) and (
            (side == "CE" and cfg.get("enable_sr_reclaim", True))
            or (side == "PE" and cfg.get("enable_sr_reject", True))
        )
        # PE rebreak needs decisive falling mom (Oct 28 knife was soft -15/10m).
        pe_rebreak_mom = float(cfg.get("pe_rebreak_min_mom", soft * 0.55))
        hard_holds = (
            (side == "CE" and vs_open >= hard and mom_ok_ce)
            or (
                side == "PE"
                and vs_open <= -hard
                and mom_ok_pe
                and mom <= -pe_rebreak_mom
            )
        )
        if (
            (not sr_fallback_on)
            and hard_holds
            and cfg.get("allow_orb_rebreak_hard_trend", True)
        ):
            tags.append("ORB rebreak under hard trend")
        else:
            allow = False
            tags.append("ORB already failed — wait S/R reclaim")

    if mins < delay:
        allow = False
        tags.append(f"open buffer {mins}/{delay}m")
    elif mins < prefer_after and not (
        (brk.get("event") and brk.get("prefer_side") == side)
        or (shake.get("active") and shake.get("prefer_side") == side)
        or sr_ok
    ):
        allow = False
        tags.append(f"wait prefer_after +{prefer_after}m")

    # Expiry day (Tue): wait out morning ATM theta, then trade the
    # post-decay impulse / gamma window. Autopsy 29 expiries:
    #   early ATM ~coin-flip; wait-floor~11:30 → +7% med +10% / 24W5L
    #   early OR-break 10:00-10:30 → negative expectancy
    if is_expiry_day or cfg.get("treat_as_expiry", False):
        exp_min = int(cfg.get("expiry_min_entry_minutes", 120))  # ~11:15
        exp_max = int(cfg.get("expiry_no_entry_after_minutes", 195))  # ~12:30
        if mins < exp_min:
            allow = False
            tags.append(f"expiry: wait decay floor {mins}/{exp_min}m")
        if mins >= exp_max:
            allow = False
            tags.append("expiry: no late pin entries")
        # Still need structure — breakout/breakdown OR SR after floor
        if allow and not (
            (brk.get("event") and brk.get("prefer_side") == side) or sr_ok
        ):
            allow = False
            tags.append("expiry: need break/SR after decay")
        # Stronger mom on expiry (anti fake OR)
        exp_mom = float(cfg.get("expiry_min_mom", 25))
        if allow and abs(mom) < exp_mom:
            allow = False
            tags.append(f"expiry: need |mom|≥{exp_mom:.0f}")

    # Block counter to hard session move
    if trend.get("block_counter_trend") and trend.get("prefer_side") and side != trend.get("prefer_side"):
        allow = False
        tags.append("blocked counter to hard session trend")

    if shake.get("active") and shake.get("block_side") == side:
        allow = False
        tags.append("blocked by shakeout")

    # Asymmetric vs-open (autopsy): CE needs ~85pts; PE works from ~55pts
    # S/R reclaim may enter near open after a dump — use softer floor.
    # Strong ORB + hard trend: slightly softer floor (Aug 3/25 missed +200 days).
    ce_min = float(cfg.get("ce_min_vs_open", cfg.get("min_vs_open_entry", soft * 0.4)))
    pe_min = float(cfg.get("pe_min_vs_open", cfg.get("min_vs_open_entry", soft * 0.4)))
    if sr_ok:
        ce_min = min(ce_min, float(cfg.get("sr_ce_min_vs_open", 0)))
        pe_min = min(pe_min, float(cfg.get("sr_pe_min_vs_open", 0)))
    if (
        brk.get("event") == "BREAKOUT"
        and side == "CE"
        and float(brk.get("strength") or 0) >= min_brk
        and cfg.get("soft_vs_open_on_strong_break", True)
    ):
        ce_min = min(ce_min, float(cfg.get("ce_min_vs_open_break", 50)))
    if (
        brk.get("event") == "BREAKDOWN"
        and side == "PE"
        and float(brk.get("strength") or 0) >= min_brk
        and cfg.get("soft_vs_open_on_strong_break", True)
    ):
        pe_min = min(pe_min, float(cfg.get("pe_min_vs_open_break", 40)))
    if side == "CE" and vs_open < ce_min and not (
        (shake.get("prefer_side") == "CE" and allow_shake_entry) or sr_ok
    ):
        allow = False
        tags.append(f"CE needs spot ≥+{ce_min:.0f} vs open")
    if side == "PE" and vs_open > -pe_min and not (
        (shake.get("prefer_side") == "PE" and allow_shake_entry) or sr_ok
    ):
        allow = False
        tags.append(f"PE needs spot ≤−{pe_min:.0f} vs open")

    # Train Feb/May/Sep 2026: PE into snapback bounce or late soft DOWN.
    # Train May 2026: PE after spent crash (day already −150 then bounce).
    if allow and side == "PE" and not sr_ok:
        bounce_n = int(cfg.get("pe_snapback_lookback", 5))
        bounce_pts = float(cfg.get("pe_snapback_bounce_pts", 18))
        if bounce_n >= 2 and len(closes) >= bounce_n + 1:
            bounce = float(closes[-1]) - float(closes[-(bounce_n + 1)])
            if bounce >= bounce_pts:
                allow = False
                tags.append(f"PE snapback bounce +{bounce:.0f}/{bounce_n}m")
        late_pe = int(cfg.get("pe_soft_no_entry_after_minutes", 200))
        if mins >= late_pe and vs_open > -hard:
            allow = False
            tags.append("late PE needs hard DOWN")
        spent = float(cfg.get("pe_spent_day_ext_pts", 140))
        off_low = float(cfg.get("pe_max_off_day_low_pts", 22))
        day_ext = day_open - day_lo if day_open and day_lo else 0.0
        if spent > 0 and day_ext >= spent and spot > day_lo + off_low:
            allow = False
            tags.append(f"PE spent dump −{day_ext:.0f} then off-low")
        # Train May 21/29: PE only while mom still accelerating down.
        pe_mom = float(cfg.get("pe_breakdown_min_mom", 0))
        if (
            pe_mom > 0
            and brk.get("event") == "BREAKDOWN"
            and mom > -pe_mom
        ):
            allow = False
            tags.append(f"PE breakdown needs mom ≤−{pe_mom:.0f}")

    # Train Jun 2026: CE breakout into falling 10m mom / soft mid chase.
    # Profit-learn 1yr: weak-mom ORB CE (Jun17/Jul29 +10) = time-stop bleed;
    # require absolute mom floor (Oct15 win was +50).
    if allow and side == "CE" and not sr_ok and brk.get("event") == "BREAKOUT":
        min_brk_mom = float(cfg.get("ce_breakout_min_mom", soft * 0.15))
        if cfg.get("block_ce_breakout_falling_mom", True) and mom < max(
            soft * 0.15, min_brk_mom
        ):
            allow = False
            tags.append(f"CE breakout needs mom ≥+{max(soft * 0.15, min_brk_mom):.0f}")

    # Sharp-fall / failed-rally train: never buy CE into dump or after big giveback.
    if allow and side == "CE" and cfg.get("block_ce_on_sharp_fall", True):
        day_dn = day_open - day_lo if day_open and day_lo else 0.0
        sharp_pts = float(cfg.get("sharp_fall_day_ext_pts", 120))
        giveback = day_hi - spot
        fail_rally = float(cfg.get("ce_fail_rally_giveback_pts", 80))
        if vs_open <= -hard or day_dn >= sharp_pts:
            allow = False
            tags.append("CE blocked on sharp-fall session")
        elif giveback >= fail_rally and vs_open < hard:
            allow = False
            tags.append(f"CE blocked after fail-rally −{giveback:.0f} from high")

    # Range-bound train: CE *breakouts* need clean path (SR reclaim may grind).
    if (
        allow
        and side == "CE"
        and not sr_ok
        and brk.get("event") == "BREAKOUT"
        and cfg.get("block_ce_low_efficiency", True)
        and len(closes) >= 40
    ):
        w = closes[-40:]
        span = max(w) - min(w)
        eff = abs(float(w[-1]) - float(w[0])) / max(span, 1.0)
        min_eff = float(cfg.get("ce_min_path_efficiency", 0.30))
        if span >= soft and eff < min_eff:
            allow = False
            tags.append(f"CE path efficiency {eff:.2f} range-bound")
        if mins >= 90 and (day_hi - day_lo) >= soft * 2:
            day_eff = abs(vs_open) / max(day_hi - day_lo, 1.0)
            if day_eff < float(cfg.get("ce_day_min_efficiency", 0.22)):
                allow = False
                tags.append(f"CE day efficiency {day_eff:.2f} range-bound")

    # Range-bound train: late CE SR on a still-flat day = fake reclaim (Apr/May/Jun).
    if allow and side == "CE" and sr_ok and cfg.get("block_late_sr_in_range", True):
        late_sr = int(cfg.get("sr_range_no_entry_after_minutes", 180))
        if mins >= late_sr and (day_hi - day_lo) >= soft * 2:
            day_eff = abs(vs_open) / max(day_hi - day_lo, 1.0)
            if day_eff < float(cfg.get("sr_range_min_day_efficiency", 0.35)):
                allow = False
                tags.append(f"late CE SR in range-bound day (eff {day_eff:.2f})")

    # Optional: only hard session trends (green-day selectivity)
    if cfg.get("require_hard_trend", False) and abs(vs_open) < hard:
        allow = False
        tags.append(f"need hard trend |vs_open|≥{hard:.0f}")

    if side == "CE" and cfg.get("allow_ce_entries", True) is False:
        allow = False
        tags.append("CE entries disabled")
    if side == "PE" and cfg.get("allow_pe_entries", True) is False:
        allow = False
        tags.append("PE entries disabled")

    event = (
        (sr.get("event") if sr_ok else None)
        or brk.get("event")
        or ("SHAKEOUT" if shake.get("active") else trend.get("bias"))
    )
    return {
        "side": side,
        "confidence": round(conf, 3),
        "reason": "; ".join(tags) if tags else "directional",
        "event": event,
        "allow_entry": allow,
        "margin": round(margin, 3),
        "vs_open": round(vs_open, 2),
        "momentum": round(mom, 2),
        "trend": trend,
        "break": brk,
        "shakeout": shake,
        "sr_range": sr,
    }


def pick_leg_for_side(
    legs: list[dict[str, Any]],
    side: str,
    *,
    premium_low: float = 125,
    premium_high: float = 145,
    prefer_mid: float | None = None,
) -> dict[str, Any] | None:
    """Pick one option on the chosen side inside (or nearest) premium band."""
    side = side.upper()
    mid = prefer_mid if prefer_mid is not None else (premium_low + premium_high) / 2
    cands = []
    for leg in legs:
        if str(leg.get("instrument_type") or leg.get("type") or "").upper() != side:
            sym = str(leg.get("tradingsymbol") or "")
            if not sym.endswith(side):
                continue
        else:
            sym = str(leg.get("tradingsymbol") or "")
        ltp = leg.get("ltp")
        if ltp is None:
            continue
        px = float(ltp)
        # Prefer inside band; allow slight overflow
        if px < premium_low - 15 or px > premium_high + 25:
            continue
        in_band = premium_low <= px <= premium_high
        cands.append((0 if in_band else 1, abs(px - mid), -px if in_band else abs(px - mid), leg))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    return cands[0][3]

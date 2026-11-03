"""
Daily price-action capture — try most sessions using momentum + local S/R.

Primary / micro / soft keep first claim. This path fires when the day is still
flat so we are not "biased idle" on big-range days that failed hard ORB gates.

Rules (causal, bar i only):
  • Side from signed momentum + soft vs-open bias
  • Enter on local support reclaim (CE) or resistance reject (PE)
  • Or trend pullback-resume if already biased
  • Bank small move (+spot target / first % target)

Loss lessons 2026-11-03 (30d PA 8L all PA_PB_RESUME):
  • PE at day-low (giveback≈0) = bounce knife — need real pullback off low
  • Early PE before ~10:30 underperformed
  • Wide OR (>90) + PB resume = spent morning
  • CE with large giveback from high / low path efficiency = chop reclaim
"""
from __future__ import annotations

from typing import Any


def daily_pa_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enable_daily_pa", False))


def daily_pa_exit_overlay(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    c = cfg or {}
    return {
        "use_percent_exits": True,
        "stop_loss_pct": float(c.get("daily_pa_stop_loss_pct", 2.5)),
        "target_pct_low": float(c.get("daily_pa_target_pct_low", 4.0)),
        "target_pct_high": float(c.get("daily_pa_target_pct_high", 10.0)),
        "lock_profit_pct": float(c.get("daily_pa_lock_profit_pct", 0.5)),
        "trail_giveback_pct": float(c.get("daily_pa_trail_giveback_pct", 1.5)),
        "time_stop_minutes": float(c.get("daily_pa_time_stop_minutes", 14)),
        "exit_at_first_target": True,
        "stop_grace_minutes": float(c.get("daily_pa_stop_grace_minutes", 2)),
        "stop_grace_pct": float(c.get("daily_pa_stop_grace_pct", 3.5)),
    }


def daily_pa_lots(cfg: dict[str, Any], primary_lots: int) -> int:
    cap = int(cfg.get("daily_pa_max_lots", 6))
    floor = int(cfg.get("daily_pa_min_lots", 4))
    n = min(primary_lots, cap) if primary_lots > 0 else cap
    return max(floor, n)


def spot_target_pts(cfg: dict[str, Any]) -> float:
    return float(cfg.get("daily_pa_spot_target_pts", 12))


def favorable_spot_move(side: str, entry_spot: float, spot: float) -> float:
    if side == "CE":
        return float(spot) - float(entry_spot)
    return float(entry_spot) - float(spot)


def spot_bank_hit(
    *,
    side: str,
    entry_spot: float,
    spot: float,
    entry_px: float,
    px: float,
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    tgt = spot_target_pts(cfg)
    fav = favorable_spot_move(side, entry_spot, spot)
    if fav >= tgt and float(px) >= float(entry_px):
        return {
            "action": "exit",
            "reason": f"daily_pa spot +{tgt:g} ({fav:.1f})",
            "fav_spot": fav,
        }
    return None


def _local_sr(closes: list[float], i: int, look: int) -> tuple[float, float]:
    w = closes[max(0, i - look) : i]
    if not w:
        return float(closes[i]), float(closes[i])
    return float(min(w)), float(max(w))


def detect_daily_pa(
    closes: list[float],
    cfg: dict[str, Any],
    *,
    bar_i: int | None = None,
) -> dict[str, Any]:
    """
    Returns {allow_entry, side, event, reason, confidence, vs_open, momentum}.
    """
    i = len(closes) - 1 if bar_i is None else int(bar_i)
    empty = {
        "allow_entry": False,
        "side": None,
        "event": None,
        "reason": "daily_pa: no signal",
        "confidence": 0.0,
        "vs_open": 0.0,
        "momentum": 0.0,
    }
    if not daily_pa_enabled(cfg) or i < 25 or len(closes) <= i:
        return empty

    after = int(cfg.get("daily_pa_entry_after_minutes", 45))
    until = int(cfg.get("daily_pa_no_entry_after_minutes", 270))
    if i < after or i >= until:
        return {**empty, "reason": f"daily_pa: outside {after}-{until}m"}

    mom_n = int(cfg.get("daily_pa_mom_minutes", 10))
    sr_look = int(cfg.get("daily_pa_sr_lookback", 25))
    prox = float(cfg.get("daily_pa_sr_proximity_pts", 12))
    min_mom = float(cfg.get("daily_pa_min_mom_pts", 8))
    min_vs = float(cfg.get("daily_pa_min_vs_open", 20))
    turn = int(cfg.get("daily_pa_turn_bars", 2))
    require_bias = bool(cfg.get("daily_pa_require_vs_bias", True))

    o = float(closes[0])
    spot = float(closes[i])
    vs = spot - o
    mom = spot - float(closes[i - mom_n]) if i >= mom_n else 0.0
    lo, hi = _local_sr(closes, i, sr_look)

    # Bias side from momentum first, vs-open as tie-break
    if mom >= min_mom and (not require_bias or vs >= min_vs * 0.5):
        side = "CE"
    elif mom <= -min_mom and (not require_bias or vs <= -min_vs * 0.5):
        side = "PE"
    elif abs(vs) >= min_vs and abs(mom) >= min_mom * 0.6:
        side = "CE" if vs > 0 else "PE"
    else:
        return {
            **empty,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: weak mom {mom:.0f} / vs {vs:.0f}",
        }

    # Need continuation bars
    if side == "CE":
        if turn >= 2 and not (
            spot > float(closes[i - 1]) and float(closes[i - 1]) >= float(closes[i - 2]) - 3
        ):
            return {
                **empty,
                "side": side,
                "vs_open": vs,
                "momentum": mom,
                "reason": "daily_pa: CE waiting turn-up",
            }
    else:
        if turn >= 2 and not (
            spot < float(closes[i - 1]) and float(closes[i - 1]) <= float(closes[i - 2]) + 3
        ):
            return {
                **empty,
                "side": side,
                "vs_open": vs,
                "momentum": mom,
                "reason": "daily_pa: PE waiting turn-down",
            }

    # S/R trigger: near local support + reclaim, or near resistance + reject
    event = None
    if side == "CE":
        near_sup = spot <= lo + prox
        reclaim = spot >= lo and float(closes[i - 1]) <= lo + prox * 0.8
        # pullback-resume: was below recent mid of range then resumes up
        mid = (lo + hi) / 2.0
        pb_resume = spot > mid and float(min(closes[i - 5 : i])) <= mid and mom >= min_mom
        allow_break = bool(cfg.get("daily_pa_allow_mom_break", True))
        allow_pb = bool(cfg.get("daily_pa_allow_pb_resume", True))
        if near_sup and reclaim and mom >= min_mom * 0.7:
            event = "PA_SR_RECLAIM"
        elif allow_pb and pb_resume:
            event = "PA_PB_RESUME"
        elif (
            allow_break
            and mom >= min_mom * 1.4
            and vs >= min_vs
            and spot >= hi
        ):
            event = "PA_MOM_BREAK"
    else:
        near_res = spot >= hi - prox
        reject = spot <= hi and float(closes[i - 1]) >= hi - prox * 0.8
        mid = (lo + hi) / 2.0
        pb_resume = spot < mid and float(max(closes[i - 5 : i])) >= mid and mom <= -min_mom
        allow_break = bool(cfg.get("daily_pa_allow_mom_break", True))
        allow_pb = bool(cfg.get("daily_pa_allow_pb_resume", True))
        if near_res and reject and mom <= -min_mom * 0.7:
            event = "PA_SR_REJECT"
        elif allow_pb and pb_resume:
            event = "PA_PB_RESUME"
        elif (
            allow_break
            and mom <= -min_mom * 1.4
            and vs <= -min_vs
            and spot <= lo
        ):
            event = "PA_MOM_BREAK"

    if not event:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: no S/R/PA trigger ({side} mom {mom:.0f})",
        }

    # Soft floor vs open (much softer than primary 65/85)
    pe_min_vs = float(cfg.get("daily_pa_pe_min_vs_open", max(min_vs, 35)))
    ce_min_vs = float(cfg.get("daily_pa_ce_min_vs_open", max(min_vs, 28)))
    if side == "CE" and vs < ce_min_vs:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: CE vs_open {vs:.0f}<{ce_min_vs:.0f}",
        }
    if side == "PE" and vs > -pe_min_vs:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: PE vs_open {vs:.0f}>-{-pe_min_vs:.0f}",
        }

    # --- Loss-learned quality gates (30d autopsy) ---
    day_hi = max(closes[: i + 1])
    day_lo = min(closes[: i + 1])
    give_ce = day_hi - spot
    give_pe = spot - day_lo
    orb_n = min(15, i)
    or_hi = max(closes[:orb_n]) if orb_n else spot
    or_lo = min(closes[:orb_n]) if orb_n else spot
    or_w = or_hi - or_lo
    w40 = closes[max(0, i - 39) : i + 1]
    span40 = max(w40) - min(w40)
    eff40 = abs(w40[-1] - w40[0]) / max(span40, 1.0)

    pe_after = int(cfg.get("daily_pa_pe_entry_after_minutes", 75))  # ~10:30
    if side == "PE" and i < pe_after:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: PE wait {i}/{pe_after}m (early PE knife)",
        }

    # eff / OR / PB-band gates validated weak on full — defaults off (0 / 999)
    min_eff = float(cfg.get("daily_pa_min_path_efficiency", 0.0))
    if min_eff > 0 and eff40 < min_eff:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: path eff {eff40:.2f}<{min_eff:.2f}",
        }

    max_or_w = float(cfg.get("daily_pa_max_orb_width_for_pb", 999))
    if event == "PA_PB_RESUME" and or_w > max_or_w:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: OR {or_w:.0f}>{max_or_w:.0f} spent for PB",
        }

    # Optional PB giveback band (off by default — hurt full-sample)
    pb_lo = float(cfg.get("daily_pa_pb_min_giveback", 0))
    pb_hi = float(cfg.get("daily_pa_pb_max_giveback", 999))
    if event == "PA_PB_RESUME" and (pb_lo > 0 or pb_hi < 900):
        if side == "PE" and not (pb_lo <= give_pe <= pb_hi):
            return {
                **empty,
                "side": side,
                "vs_open": vs,
                "momentum": mom,
                "reason": (
                    f"daily_pa: PE PB give {give_pe:.0f} not in "
                    f"{pb_lo:.0f}-{pb_hi:.0f} (avoid day-low knife)"
                ),
            }
        if side == "CE" and not (pb_lo <= give_ce <= pb_hi):
            return {
                **empty,
                "side": side,
                "vs_open": vs,
                "momentum": mom,
                "reason": (
                    f"daily_pa: CE PB give {give_ce:.0f} not in "
                    f"{pb_lo:.0f}-{pb_hi:.0f}"
                ),
            }

    # PE must bounce off day-low (3m: most PE losses were give_pe≈0 knives)
    pe_bounce = float(cfg.get("daily_pa_pe_min_giveback", 0))
    if side == "PE" and pe_bounce > 0 and give_pe < pe_bounce:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": (
                f"daily_pa: PE give {give_pe:.0f}<{pe_bounce:.0f} "
                f"(day-low knife)"
            ),
        }

    # CE reclaim/PB with huge giveback from high already spent
    max_ce_give = float(cfg.get("daily_pa_ce_max_giveback", 55))
    if side == "CE" and give_ce > max_ce_give:
        return {
            **empty,
            "side": side,
            "vs_open": vs,
            "momentum": mom,
            "reason": f"daily_pa: CE giveback {give_ce:.0f}>{max_ce_give:.0f}",
        }

    conf = min(0.82, 0.52 + abs(mom) / 80.0 + abs(vs) / 250.0)
    return {
        "allow_entry": True,
        "side": side,
        "event": event,
        "reason": (
            f"daily_pa {event} {side} mom {mom:+.0f} vs {vs:+.0f} "
            f"SR[{lo:.0f}-{hi:.0f}]"
        ),
        "confidence": conf,
        "vs_open": vs,
        "momentum": mom,
    }

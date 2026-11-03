"""Session market-movement bias — keep entries aligned with the day's trend."""

from __future__ import annotations

from typing import Any


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def day_open_from_minutes(closes: list[float], candles_1m: list[dict[str, Any]] | None = None) -> float | None:
    """Best available cash open: first 09:15 bar, else first close in path."""
    if candles_1m:
        for c in candles_1m:
            dt = c.get("date")
            if dt is None:
                continue
            try:
                h, m = dt.hour, dt.minute
            except Exception:
                continue
            if h == 9 and m == 15:
                return _f(c.get("open") or c.get("close"))
    if closes:
        return float(closes[0])
    return None


def compute_session_trend(
    *,
    spot: float | None,
    day_open: float | None = None,
    closes: list[float] | None = None,
    ema9: float | None = None,
    ema21: float | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Classify session direction from spot vs open + short momentum + EMA.

    Returns prefer_side CE|PE|None, bias UP|DOWN|FLAT, strength 0..1, and
    whether counter-trend single-leg entries should be blocked.
    """
    cfg = cfg or {}
    soft = float(cfg.get("trend_soft_pts", 35))
    hard = float(cfg.get("trend_hard_pts", 70))
    pullback_ok = float(cfg.get("trend_pullback_ok_pts", 25))
    lookback = int(cfg.get("trend_momentum_minutes", 10))
    confirm_m = int(cfg.get("trend_confirm_minutes", 20))

    closes = list(closes or [])
    px = _f(spot)
    if px is None and closes:
        px = float(closes[-1])
    open_px = _f(day_open)
    if open_px is None and closes:
        open_px = float(closes[0])

    vs_open = (px - open_px) if (px is not None and open_px is not None) else None
    vs_lb = None
    if px is not None and len(closes) > lookback:
        vs_lb = px - float(closes[-(lookback + 1)])
    mins = max(0, len(closes) - 1)
    # Sustained: last hold_n minutes still beyond soft threshold (anti stop-hunt)
    hold_n = min(8, max(3, len(closes) // 4))
    recent = closes[-hold_n:] if closes else []
    sustained_up = bool(recent) and open_px is not None and min(recent) >= open_px + soft * 0.45
    sustained_dn = bool(recent) and open_px is not None and max(recent) <= open_px - soft * 0.45

    ema_bull = ema9 is not None and ema21 is not None and ema9 > ema21
    ema_bear = ema9 is not None and ema21 is not None and ema9 < ema21

    bias = "FLAT"
    strength = 0.0
    prefer: str | None = None
    block_counter = False
    reasons: list[str] = []

    if vs_open is not None:
        if vs_open >= hard:
            bias = "UP"
            strength = min(1.0, 0.55 + (vs_open - hard) / max(hard, 1) * 0.35)
            prefer = "CE"
            reasons.append(f"spot +{vs_open:.0f} vs open (hard)")
            # Pullback inside a strong up day still prefers CE
            if vs_lb is not None and vs_lb <= -pullback_ok:
                reasons.append(f"dip {vs_lb:.0f}/{lookback}m — buy-dip CE not PE")
            # Early hard spikes need hold; else soft preference only
            if mins >= confirm_m or sustained_up:
                block_counter = True
            else:
                strength = min(strength, 0.4)
                reasons.append("early UP — wait hold before hard bias")
        elif vs_open <= -hard:
            bias = "DOWN"
            strength = min(1.0, 0.55 + (abs(vs_open) - hard) / max(hard, 1) * 0.35)
            prefer = "PE"
            reasons.append(f"spot {vs_open:.0f} vs open (hard)")
            if vs_lb is not None and vs_lb >= pullback_ok:
                reasons.append(f"bounce +{vs_lb:.0f}/{lookback}m — fade CE not CE chase")
            if mins >= confirm_m or sustained_dn:
                block_counter = True
            else:
                # Opening dump often reverses — do not force PE
                strength = min(strength, 0.35)
                prefer = None
                bias = "FLAT"
                block_counter = False
                reasons.append("early DOWN dump — treat as possible shakeout (no PE force)")
        elif vs_open >= soft:
            bias = "UP"
            strength = 0.35 + min(0.2, (vs_open - soft) / max(soft, 1) * 0.2)
            prefer = "CE"
            reasons.append(f"spot +{vs_open:.0f} vs open")
            if ema_bull:
                strength = min(1.0, strength + 0.15)
                reasons.append("EMA bull")
                block_counter = vs_open >= soft * 1.4 or ema_bull
            # Soft up + sharp 10m dump → only soft bias, allow PE if other signals strong
            if vs_lb is not None and vs_lb <= -pullback_ok * 1.2:
                strength = max(0.2, strength - 0.15)
                reasons.append(f"soft up but {lookback}m dump {vs_lb:.0f}")
                block_counter = False
        elif vs_open <= -soft:
            bias = "DOWN"
            strength = 0.35 + min(0.2, (abs(vs_open) - soft) / max(soft, 1) * 0.2)
            prefer = "PE"
            reasons.append(f"spot {vs_open:.0f} vs open")
            if ema_bear:
                strength = min(1.0, strength + 0.15)
                reasons.append("EMA bear")
                block_counter = vs_open <= -soft * 1.4 or ema_bear
            if vs_lb is not None and vs_lb >= pullback_ok * 1.2:
                strength = max(0.2, strength - 0.15)
                reasons.append(f"soft down but {lookback}m bounce +{vs_lb:.0f}")
                block_counter = False
        else:
            reasons.append(f"spot near open ({vs_open:+.0f})")
            if ema_bull and (vs_lb or 0) > 0:
                bias = "UP"
                prefer = "CE"
                strength = 0.25
                reasons.append("EMA+momentum soft UP")
            elif ema_bear and (vs_lb or 0) < 0:
                bias = "DOWN"
                prefer = "PE"
                strength = 0.25
                reasons.append("EMA+momentum soft DOWN")

    return {
        "bias": bias,
        "prefer_side": prefer,
        "strength": round(strength, 3),
        "block_counter_trend": block_counter,
        "vs_open": None if vs_open is None else round(vs_open, 2),
        "vs_momentum": None if vs_lb is None else round(vs_lb, 2),
        "day_open": open_px,
        "spot": px,
        "ema_bull": ema_bull,
        "ema_bear": ema_bear,
        "reason": "; ".join(reasons) if reasons else "flat/unclear",
    }


def apply_trend_to_scores(
    score_ce: float,
    score_pe: float,
    trend: dict[str, Any],
    *,
    weight: float = 1.0,
) -> tuple[float, float, list[str]]:
    """Push scores toward session trend; return updated scores + reason tags."""
    tags: list[str] = []
    prefer = trend.get("prefer_side")
    strength = float(trend.get("strength") or 0) * weight
    if not prefer or strength <= 0:
        return score_ce, score_pe, tags
    boost = 0.10 + 0.22 * strength
    if prefer == "CE":
        score_ce += boost
        score_pe -= boost * 0.55
        tags.append(f"session UP→CE ({trend.get('reason')})")
    else:
        score_pe += boost
        score_ce -= boost * 0.55
        tags.append(f"session DOWN→PE ({trend.get('reason')})")
    return score_ce, score_pe, tags


def counter_trend_blocked(side: str, trend: dict[str, Any]) -> bool:
    if not trend.get("block_counter_trend"):
        return False
    prefer = trend.get("prefer_side")
    if prefer is None:
        return False
    return side != prefer

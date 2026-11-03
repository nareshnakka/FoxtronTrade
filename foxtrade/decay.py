"""
Decay-day / early-bleed detector.

Avoid early entries when both premiums are likely to grind down (theta/range)
instead of producing a one-sided breakout from convergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class DecayState:
    observe_until: datetime | None = None
    observing: bool = True
    is_decay_day: bool = False
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    cleared_by_impulse: bool = False
    last_message: str = ""


class DecayGuard:
    def __init__(self) -> None:
        self.state = DecayState()

    def reset_for_session(self, observe_seconds: int = 480) -> None:
        now = datetime.now(IST)
        session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        base = session_open if now.date() == session_open.date() and now >= session_open else now
        # If we're already past open+observe on a weekday, don't block forever — score immediately
        until = base + timedelta(seconds=observe_seconds)
        observing = now < until
        self.state = DecayState(
            observe_until=until,
            observing=observing,
            last_message=(
                f"observing until {until.strftime('%H:%M:%S')}"
                if observing
                else "observation window elapsed — scoring"
            ),
        )

    def update(
        self,
        *,
        cfg: dict[str, Any],
        index: dict[str, Any],
        macro: dict[str, Any],
        ce_hist: list[float],
        pe_hist: list[float],
        spot_hist: list[float],
    ) -> DecayState:
        now = datetime.now(IST)
        if self.state.observe_until is None:
            self.reset_for_session(int(cfg.get("decay_observe_seconds", 480)))

        impulse = self._impulse(index, spot_hist, ce_hist, pe_hist)
        if impulse and self.state.is_decay_day:
            self.state.is_decay_day = False
            self.state.cleared_by_impulse = True
            self.state.reasons = [impulse]
            self.state.last_message = f"decay cleared: {impulse}"
            return self.state

        if self.state.observing and self.state.observe_until and now < self.state.observe_until:
            left = int((self.state.observe_until - now).total_seconds())
            self.state.last_message = f"early observe ({left}s left) — no entries"
            return self.state

        if self.state.observing:
            self.state.observing = False

        self._score(cfg, index, macro, ce_hist, pe_hist, spot_hist)
        return self.state

    def block_entries(self) -> bool:
        if self.state.observing:
            return True
        return self.state.is_decay_day and not self.state.cleared_by_impulse

    def _score(
        self,
        cfg: dict[str, Any],
        index: dict[str, Any],
        macro: dict[str, Any],
        ce_hist: list[float],
        pe_hist: list[float],
        spot_hist: list[float],
    ) -> None:
        score = 0.0
        reasons: list[str] = []
        threshold = float(cfg.get("decay_score_threshold", 0.55))

        vix = macro.get("vix")
        vix_chg = macro.get("vix_change_pct")
        if vix is not None and vix < float(cfg.get("decay_vix_low", 13.5)):
            score += 0.22
            reasons.append(f"VIX low {vix:.1f}")
        if vix_chg is not None and vix_chg < -3:
            score += 0.12
            reasons.append(f"VIX falling {vix_chg:.1f}%")

        atr_v = index.get("atr14")
        day_range = index.get("day_range")
        if atr_v and day_range is not None and atr_v > 0:
            compression = day_range / atr_v
            if compression < 0.45:
                score += 0.2
                reasons.append(f"range/ATR {compression:.2f}")

        ema9, ema21 = index.get("ema9"), index.get("ema21")
        if ema9 is not None and ema21 is not None and abs(ema9 - ema21) < 15:
            score += 0.1
            reasons.append("flat EMAs")

        rsi_v = index.get("rsi14")
        if rsi_v is not None and 45 <= rsi_v <= 55:
            score += 0.08
            reasons.append("RSI mid")

        if len(ce_hist) >= 60 and len(pe_hist) >= 60:
            ce_d = ce_hist[-1] - ce_hist[-60]
            pe_d = pe_hist[-1] - pe_hist[-60]
            if ce_d <= -4 and pe_d <= -4 and abs(ce_d - pe_d) <= 3:
                score += 0.25
                reasons.append(f"sym bleed CE{ce_d:.1f}/PE{pe_d:.1f}")

        if len(spot_hist) >= 60:
            spot_move = abs(spot_hist[-1] - spot_hist[0])
            if spot_move < 25:
                score += 0.12
                reasons.append(f"tiny spot impulse {spot_move:.0f}")

        if macro.get("regime") == "risk_off":
            score -= 0.15
            reasons.append("risk_off dampens decay")

        self.state.score = max(0.0, min(1.0, score))
        self.state.reasons = reasons
        self.state.is_decay_day = self.state.score >= threshold and not self.state.cleared_by_impulse
        if self.state.is_decay_day:
            self.state.last_message = f"DECAY DAY score={self.state.score:.2f} ({'; '.join(reasons)})"
        else:
            self.state.last_message = (
                f"tradeable score={self.state.score:.2f} ({'; '.join(reasons) or 'ok'})"
            )

    def _impulse(
        self,
        index: dict[str, Any],
        spot_hist: list[float],
        ce_hist: list[float],
        pe_hist: list[float],
    ) -> str | None:
        if len(spot_hist) >= 30:
            move = spot_hist[-1] - spot_hist[-30]
            if abs(move) >= 40:
                return f"spot impulse {move:+.0f}"
        if len(ce_hist) >= 30 and len(pe_hist) >= 30:
            ce_d = ce_hist[-1] - ce_hist[-30]
            pe_d = pe_hist[-1] - pe_hist[-30]
            if abs(ce_d - pe_d) >= 10 and max(ce_d, pe_d) >= 6:
                return f"premium skew CE{ce_d:+.1f}/PE{pe_d:+.1f}"
        patterns = index.get("patterns") or {}
        if any(patterns.get(k) for k in ("shooting_star", "hammer", "bearish_engulf", "bullish_engulf")):
            return "reversal candle impulse"
        return None

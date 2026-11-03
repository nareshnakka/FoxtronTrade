"""
Persistent CE/PE decay memory + post-decay entry signals.

Stores per-session premium OHLC (and spot) so we can detect:
  - CE day crush ≥50% → next session prefer CE bounce
  - PE multi-day / week crush → prefer PE bounce
  - ≥3 consecutive down closes → prefer bounce side

Used by the engine to WAIT through decay, then allow/boost entries after.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from datetime import timezone, timedelta
from zoneinfo import ZoneInfo

try:
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001 — Windows without tzdata
    IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PATH = DATA / "decay_memory.json"
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _load() -> dict[str, Any]:
    with _lock:
        if not PATH.exists():
            return {"version": 1, "days": {}, "week": {}, "signals": []}
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "days": {}, "signals": []}


def _save(payload: dict[str, Any]) -> None:
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


@dataclass
class PostDecaySignal:
    action: str = "NONE"  # NONE | WAIT | ENTER_CE | ENTER_PE
    side: str | None = None
    setup: str = ""
    confidence: float = 0.0
    expected_profit_pct: float = 0.0  # estimated bounce upside %
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "side": self.side,
            "setup": self.setup,
            "confidence": round(self.confidence, 3),
            "expected_profit_pct": round(self.expected_profit_pct, 1),
            "reason": self.reason,
            "details": self.details,
        }


class DecayMemory:
    """Live store + analyzer for premium decay / post-decay entries."""

    def __init__(
        self,
        *,
        persist: bool = True,
        seed_days: dict[str, Any] | None = None,
    ) -> None:
        self.persist = persist
        if seed_days is not None:
            self.db: dict[str, Any] = {
                "version": 1,
                "days": dict(seed_days),
                "signals": [],
            }
        elif persist:
            self.db = _load()
        else:
            self.db = {"version": 1, "days": {}, "signals": []}
        self.last_signal = PostDecaySignal()
        self.last_message = ""
        self._last_save_ts: float = 0.0
        self._dirty: bool = False
        self.as_of: str | None = None  # backtest override for "today"

    def set_as_of(self, day: str | None) -> None:
        self.as_of = day

    def _day_key(self, day: str | None = None) -> str:
        return day or self.as_of or _today()

    def reload(self) -> None:
        if self.persist:
            self.db = _load()

    def _maybe_save(self, *, force: bool = False) -> None:
        if not self.persist:
            self._dirty = False
            return
        import time as _time

        now = _time.time()
        if force or (self._dirty and now - self._last_save_ts >= 15.0):
            _save(self.db)
            self._last_save_ts = now
            self._dirty = False

    def update_live(
        self,
        *,
        ce: float | None,
        pe: float | None,
        spot: float | None,
        strike: float | None = None,
        expiry: str | None = None,
        symbol_ce: str | None = None,
        symbol_pe: str | None = None,
        day: str | None = None,
        source: str = "live",
        spot_open: float | None = None,
        spot_high: float | None = None,
        spot_low: float | None = None,
    ) -> None:
        """Update day's running OHLC from quotes (call every tick)."""
        if ce is None and pe is None and spot is None:
            return
        day = self._day_key(day)
        days: dict[str, Any] = self.db.setdefault("days", {})
        row = days.get(day) or {
            "date": day,
            "finalized": False,
            "strike": strike,
            "expiry": expiry,
            "symbol_ce": symbol_ce,
            "symbol_pe": symbol_pe,
            "ce_open": None,
            "ce_high": None,
            "ce_low": None,
            "ce_close": None,
            "pe_open": None,
            "pe_high": None,
            "pe_low": None,
            "pe_close": None,
            "spot_open": None,
            "spot_high": None,
            "spot_low": None,
            "spot_close": None,
            "updated_at": None,
        }
        if strike is not None:
            row["strike"] = strike
        if expiry:
            row["expiry"] = expiry
        if symbol_ce:
            row["symbol_ce"] = symbol_ce
        if symbol_pe:
            row["symbol_pe"] = symbol_pe

        def _ohlc(prefix: str, px: float | None) -> None:
            if px is None:
                return
            px = float(px)
            if row.get(f"{prefix}_open") is None:
                row[f"{prefix}_open"] = px
            row[f"{prefix}_high"] = max(float(row.get(f"{prefix}_high") or px), px)
            row[f"{prefix}_low"] = min(float(row.get(f"{prefix}_low") or px), px)
            row[f"{prefix}_close"] = px

        _ohlc("ce", ce)
        _ohlc("pe", pe)
        # Spot: prefer true session open + bar high/low (lesson 2026-10-26 —
        # using first close as open made a dump look like a green bounce).
        if spot_open is not None and row.get("spot_open") is None:
            row["spot_open"] = float(spot_open)
        if spot is not None:
            px = float(spot)
            if row.get("spot_open") is None:
                row["spot_open"] = float(spot_open) if spot_open is not None else px
            hi = float(spot_high) if spot_high is not None else px
            lo = float(spot_low) if spot_low is not None else px
            row["spot_high"] = max(float(row.get("spot_high") or hi), hi, px)
            row["spot_low"] = min(float(row.get("spot_low") or lo), lo, px)
            row["spot_close"] = px
        row["updated_at"] = datetime.now(IST).isoformat()
        row["finalized"] = False
        row["source"] = source
        days[day] = row
        # trim history (~120 sessions)
        if len(days) > 130:
            for k in sorted(days.keys())[:-120]:
                days.pop(k, None)
        self.db["days"] = days
        self._dirty = True
        self._maybe_save()

    def finalize_day(self, day: str | None = None) -> None:
        day = self._day_key(day)
        row = (self.db.get("days") or {}).get(day)
        if not row:
            return
        row["finalized"] = True
        row["finalized_at"] = datetime.now(IST).isoformat()
        self.db.setdefault("days", {})[day] = row
        self._dirty = True
        self._maybe_save(force=True)

    def _sorted_days(self, *, exclude_today: bool = False) -> list[dict[str, Any]]:
        today = self._day_key()
        rows = []
        for k in sorted((self.db.get("days") or {}).keys()):
            if exclude_today and k == today:
                continue
            # Causal: never use future sessions vs as_of
            if k > today:
                continue
            row = self.db["days"][k]
            if row.get("ce_close") is None and row.get("pe_close") is None:
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _est_bounce_pct(*, entry: float, ref_high: float, already_mult: float, reclaim: float) -> float:
        """
        Estimate remaining profit % if premium partially reclaims toward ref_high.
        reclaim=0.45 means expect bounce to reclaim 45% of the crush gap.
        Caps residual upside after any bounce already taken.
        """
        if entry <= 0 or ref_high <= entry:
            return 0.0
        target = entry + (ref_high - entry) * max(0.05, min(0.95, reclaim))
        raw = (target / entry - 1.0) * 100.0
        # haircut if bounce already started
        if already_mult > 1.0:
            raw *= max(0.15, 1.0 / already_mult)
        return max(0.0, raw)

    def analyze(self, cfg: dict[str, Any] | None = None) -> PostDecaySignal:
        """
        Produce WAIT / ENTER_CE / ENTER_PE from stored history + today's path.
        When several setups fire, pick the side/setup with highest expected profit %.
        """
        cfg = cfg or {}
        if not cfg.get("post_decay_entry_enabled", True):
            self.last_signal = PostDecaySignal(reason="post_decay_entry disabled")
            return self.last_signal

        half_pct = float(cfg.get("post_decay_ce_half_pct", 50.0)) / 100.0
        pe_crush_pct = float(cfg.get("post_decay_pe_crush_pct", 60.0)) / 100.0
        pe_low_max = float(cfg.get("post_decay_pe_low_max", 150.0))
        streak_n = int(cfg.get("post_decay_streak_days", 3))
        min_conf = float(cfg.get("post_decay_min_confidence", 0.62))
        already_cap = float(cfg.get("post_decay_already_ran_mult", 1.8))
        # Partial reclaim of crush gap used as profit estimate (from crush-bounce study)
        reclaim_ce = float(cfg.get("post_decay_reclaim_ce", 0.55))
        reclaim_pe = float(cfg.get("post_decay_reclaim_pe", 0.50))
        reclaim_streak = float(cfg.get("post_decay_reclaim_streak", 0.40))

        hist = self._sorted_days(exclude_today=False)
        today = self._day_key()
        today_row = (self.db.get("days") or {}).get(today) or {}
        closed = [r for r in hist if r.get("date") != today]

        candidates: list[PostDecaySignal] = []

        # --- H2: CE day crush ≥ half_pct ---
        if len(closed) >= 2:
            prev, yday = closed[-2], closed[-1]
            y_ce = float(yday.get("ce_close") or 0)
            p_ce = float(prev.get("ce_close") or 0)
            if p_ce > 20 and y_ce > 0 and y_ce <= p_ce * (1.0 - half_pct):
                t_ce = float(today_row.get("ce_close") or y_ce)
                bounce_so_far = t_ce / max(y_ce, 1.0)
                if bounce_so_far < already_cap:
                    crush = 1.0 - y_ce / p_ce
                    exp = self._est_bounce_pct(
                        entry=t_ce, ref_high=p_ce, already_mult=bounce_so_far, reclaim=reclaim_ce
                    )
                    conf = min(0.92, 0.55 + crush * 0.5 + (0.15 if bounce_so_far < 1.2 else 0))
                    candidates.append(
                        PostDecaySignal(
                            action="ENTER_CE",
                            side="CE",
                            setup="ce_half_day_crush",
                            confidence=max(conf, min_conf),
                            expected_profit_pct=exp,
                            reason=(
                                f"CE crushed {100*crush:.0f}% ({p_ce:.0f}→{y_ce:.0f}) "
                                f"— est +{exp:.0f}% bounce"
                            ),
                            details={
                                "prev_ce": p_ce,
                                "yday_ce": y_ce,
                                "today_ce": t_ce,
                                "bounce_so_far": round(bounce_so_far, 2),
                                "est_profit_pct": round(exp, 1),
                            },
                        )
                    )

        # --- H1: multi-day / week crush (CE or PE) ---
        # Thesis: premium decayed from ~400–500 zone over 3–5 sessions into ~100–130
        # ("blockbuster" bounce often +50% to +200%).
        high_min = float(cfg.get("post_decay_high_min", 350.0))
        entry_lo = float(cfg.get("post_decay_entry_low", 90.0))
        entry_hi = float(cfg.get("post_decay_entry_high", 145.0))
        lookback = max(3, min(5, int(cfg.get("post_decay_crush_lookback_days", 5))))
        if len(closed) >= 2:
            window = closed[-lookback:]
            for side, high_key, low_key, close_key, reclaim in (
                ("PE", "pe_high", "pe_low", "pe_close", reclaim_pe),
                ("CE", "ce_high", "ce_low", "ce_close", reclaim_ce),
            ):
                side_high = max(
                    float(r.get(high_key) or r.get(close_key) or r.get(f"{side.lower()}_open") or 0)
                    for r in window
                )
                y = closed[-1]
                side_low = float(y.get(low_key) or y.get(close_key) or 0)
                side_close = float(y.get(close_key) or 0)
                low = min(side_low, side_close) if side_close else side_low
                if (
                    side_high >= high_min
                    and low > 0
                    and low <= side_high * (1.0 - pe_crush_pct)
                    and low <= pe_low_max
                ):
                    t_px = float(today_row.get(close_key) or low)
                    # Prefer buy when premium is in the crushed band (~100–130)
                    in_band = entry_lo <= t_px <= entry_hi
                    if not in_band and t_px > entry_hi:
                        # already bounced out of buy zone — skip
                        continue
                    bounce_so_far = t_px / max(low, 1.0)
                    if bounce_so_far < already_cap:
                        crush = 1.0 - low / side_high
                        exp = self._est_bounce_pct(
                            entry=t_px,
                            ref_high=side_high,
                            already_mult=bounce_so_far,
                            reclaim=reclaim,
                        )
                        conf = min(0.92, 0.58 + crush * 0.35 + (0.06 if in_band else 0))
                        setup = (
                            "pe_multi_day_crush"
                            if side == "PE"
                            else "ce_multi_day_crush"
                        )
                        candidates.append(
                            PostDecaySignal(
                                action=f"ENTER_{side}",
                                side=side,
                                setup=setup,
                                confidence=max(conf, min_conf),
                                expected_profit_pct=exp,
                                reason=(
                                    f"{side} crushed {100 * crush:.0f}% "
                                    f"(high {side_high:.0f}→low {low:.0f}) "
                                    f"in ~{lookback}d — buy zone @{t_px:.0f} "
                                    f"est +{exp:.0f}% blockbuster"
                                ),
                                details={
                                    f"{side.lower()}_high": side_high,
                                    f"{side.lower()}_low": low,
                                    f"today_{side.lower()}": t_px,
                                    "in_buy_band": in_band,
                                    "buy_band": [entry_lo, entry_hi],
                                    "lookback_days": lookback,
                                    "est_profit_pct": round(exp, 1),
                                },
                            )
                        )

        # --- H3: 3+ consecutive down closes (score both CE and PE) ---
        if len(closed) >= streak_n:
            for side, key in (("CE", "ce_close"), ("PE", "pe_close")):
                vals = [float(r.get(key) or 0) for r in closed[-(streak_n + 1) :]]
                if len(vals) < streak_n + 1 or min(vals) <= 0:
                    continue
                downs = all(vals[i] < vals[i - 1] for i in range(1, len(vals)))
                if not downs:
                    continue
                last = vals[-1]
                t_px = float(today_row.get(key) or last)
                bounce_so_far = t_px / max(last, 1.0)
                if bounce_so_far >= already_cap:
                    continue
                ref = vals[0]
                exp = self._est_bounce_pct(
                    entry=t_px, ref_high=ref, already_mult=bounce_so_far, reclaim=reclaim_streak
                )
                candidates.append(
                    PostDecaySignal(
                        action=f"ENTER_{side}",
                        side=side,
                        setup="three_day_fall",
                        confidence=max(0.64, min_conf),
                        expected_profit_pct=exp,
                        reason=(
                            f"{side} fell {streak_n}+ sessions ({vals[0]:.0f}→{last:.0f}) "
                            f"— est +{exp:.0f}% bounce"
                        ),
                        details={
                            "closes": vals,
                            "today": t_px,
                            "est_profit_pct": round(exp, 1),
                        },
                    )
                )

        deny = {str(x) for x in (cfg.get("post_decay_deny_setups") or [])}
        allow_list = cfg.get("post_decay_allow_setups")
        allow = {str(x) for x in allow_list} if allow_list is not None else None
        if deny:
            candidates = [c for c in candidates if c.setup not in deny]
        if allow is not None:
            candidates = [c for c in candidates if c.setup in allow]

        if not candidates:
            signal = PostDecaySignal(action="NONE", reason="no decay setup")
        else:
            # Prefer higher expected profit %; tie-break on confidence
            candidates.sort(
                key=lambda s: (s.expected_profit_pct, s.confidence),
                reverse=True,
            )
            signal = candidates[0]
            if len(candidates) > 1:
                alts = ", ".join(
                    f"{c.side}/{c.setup} +{c.expected_profit_pct:.0f}%" for c in candidates[1:3]
                )
                signal.details = {
                    **signal.details,
                    "picked_for": "max_expected_profit_pct",
                    "alternatives": alts,
                }
                signal.reason = (
                    f"{signal.reason} [best of {len(candidates)}: beat {alts}]"
                )

        # --- WAIT: today still crushing hard vs open (don't catch falling knife) ---
        if signal.action.startswith("ENTER_"):
            side = signal.side
            if side == "CE":
                o = float(today_row.get("ce_open") or 0)
                c = float(today_row.get("ce_close") or 0)
            else:
                o = float(today_row.get("pe_open") or 0)
                c = float(today_row.get("pe_close") or 0)
            knife = float(cfg.get("post_decay_knife_drop_pct", 25.0)) / 100.0
            if o > 0 and c > 0 and c <= o * (1.0 - knife):
                signal = PostDecaySignal(
                    action="WAIT",
                    side=side,
                    setup=signal.setup,
                    confidence=signal.confidence,
                    expected_profit_pct=signal.expected_profit_pct,
                    reason=f"{side} still decaying today ({o:.0f}→{c:.0f}) — wait stabilization",
                    details={**signal.details, "open": o, "last": c},
                )

        # --- WAIT: spot must agree with bounce side (lesson 2026-11-02) ---
        # PE bounce fails on UP days; CE bounce fails on DOWN days. Applies any day.
        # Also: once the session already printed an UP (DOWN) swing, do not fade with
        # PE (CE) while spot is still on that side of open — shallow pullbacks ≠ reverse.
        if (
            signal.action.startswith("ENTER_")
            and cfg.get("post_decay_require_spot_align", True)
        ):
            spot_o = float(today_row.get("spot_open") or 0)
            spot_c = float(today_row.get("spot_close") or 0)
            spot_hi = float(today_row.get("spot_high") or spot_c or 0)
            spot_lo = float(today_row.get("spot_low") or spot_c or 0)
            align_pts = float(
                cfg.get("post_decay_spot_align_pts")
                or cfg.get("trend_soft_pts")
                or 35.0
            )
            if spot_o > 0 and spot_c > 0 and align_pts > 0:
                vs = spot_c - spot_o
                up_extent = spot_hi - spot_o
                dn_extent = spot_o - spot_lo
                side = signal.side
                conflict = False
                why = ""
                if side == "PE":
                    if vs >= align_pts:
                        conflict, why = True, f"spot {vs:+.0f} vs open"
                    elif up_extent >= align_pts and vs > 0:
                        conflict, why = (
                            True,
                            f"UP-day ({up_extent:+.0f} high) still above open ({vs:+.0f})",
                        )
                elif side == "CE":
                    # Lesson 2026-10-26: weak-est "partial reclaim" under open is a
                    # dead-cat knife (09:16 −14 vs open, est +75% → −35k). Deep
                    # crush bounces (Oct22 −118) may still be valid — only block
                    # shallow under-open reclaims when est is weak.
                    strong_est = float(cfg.get("post_decay_strong_est_pct", 200.0))
                    est = float(signal.expected_profit_pct or 0)
                    if (
                        est < strong_est
                        and vs < 0
                        and abs(vs) < align_pts
                    ):
                        conflict, why = (
                            True,
                            f"weak-est +{est:.0f}% partial reclaim "
                            f"({vs:+.0f} vs open) — wait open reclaim",
                        )
                    elif vs <= -float(
                        cfg.get("post_decay_ce_max_under_open", 150.0)
                    ):
                        # Extremely deep dump — don't catch with CE bounce
                        conflict, why = True, f"spot {vs:+.0f} vs open (too deep)"
                    elif (
                        est < strong_est
                        and dn_extent >= align_pts
                        and 0 > vs > -align_pts
                    ):
                        conflict, why = (
                            True,
                            f"DOWN-day partial reclaim ({vs:+.0f}) weak-est",
                        )
                if conflict:
                    signal = PostDecaySignal(
                        action="WAIT",
                        side=side,
                        setup=signal.setup,
                        confidence=signal.confidence,
                        expected_profit_pct=signal.expected_profit_pct,
                        reason=f"{why} blocks post-decay {side}",
                        details={
                            **signal.details,
                            "spot_open": spot_o,
                            "spot_last": spot_c,
                            "spot_high": spot_hi,
                            "spot_low": spot_lo,
                            "vs_open": round(vs, 1),
                            "up_extent": round(up_extent, 1),
                            "dn_extent": round(dn_extent, 1),
                            "align_pts": align_pts,
                            "blocked_by": "spot_align",
                        },
                    )

        self.last_signal = signal
        self.last_message = signal.reason or signal.action
        log = self.db.setdefault("signals", [])
        if signal.action != "NONE":
            entry = {
                "ts": datetime.now(IST).isoformat(),
                "date": today,
                **signal.as_dict(),
            }
            if not log or log[-1].get("reason") != entry["reason"]:
                log.append(entry)
                self.db["signals"] = log[-200:]
                self._dirty = True
                self._maybe_save(force=True)
        return signal

    def snapshot(self) -> dict[str, Any]:
        days = self._sorted_days()
        return {
            "days_stored": len(days),
            "today": (self.db.get("days") or {}).get(_today()),
            "signal": self.last_signal.as_dict(),
            "message": self.last_message,
            "recent_days": days[-8:],
        }

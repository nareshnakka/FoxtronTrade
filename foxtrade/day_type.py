"""
Causal day-type trainer for Algo-CW dual (70–77).

Learned from walk-forward last sessions:
  • ~1 stretch/3× · ~2 fallen-reverse · ~1 breakout-2× · ~1 decay / week
  • Early tells (first ~45–60m after entry) switch exit mode:

    DECAY           → LOCK_145 (book @1.45×, no trail, skip 2nd)
    BREAKOUT / STRETCH → TRAIL (arm ~1.30–1.45, giveback, allow 2×/3×)
    FALLEN_REVERSE  → FALLEN_WATCH (book winner @1.45+, hold/watch fallen bounce)
    NORMAL          → LOCK_145 until a stronger tell prints

No look-ahead: only uses CE/PE multiples observed so far since entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DayTypeSnapshot:
    day_type: str = "UNKNOWN"
    exit_mode: str = "LOCK_145"
    winner: str | None = None
    fallen: str | None = None
    lead_x: float = 1.0
    lag_x: float = 1.0
    lead_flips: int = 0
    samples: int = 0
    minutes_open: float = 0.0
    wait_retrace: bool = False
    pair_exit_mult: float | None = None  # override if set
    trail_arm_mult: float | None = None
    trail_giveback_pct: float | None = None
    hold_fallen: bool = False
    skip_second: bool = False
    advice: str = ""
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day_type": self.day_type,
            "exit_mode": self.exit_mode,
            "winner": self.winner,
            "fallen": self.fallen,
            "lead_x": round(self.lead_x, 3),
            "lag_x": round(self.lag_x, 3),
            "lead_flips": self.lead_flips,
            "samples": self.samples,
            "minutes_open": round(self.minutes_open, 1),
            "wait_retrace": self.wait_retrace,
            "pair_exit_mult": self.pair_exit_mult,
            "trail_arm_mult": self.trail_arm_mult,
            "trail_giveback_pct": self.trail_giveback_pct,
            "hold_fallen": self.hold_fallen,
            "skip_second": self.skip_second,
            "advice": self.advice,
            "reasons": list(self.reasons),
        }


@dataclass
class DayTypeTracker:
    """Update each tick after dual entry with CE/PE LTPs vs entry."""

    ce_entry: float = 0.0
    pe_entry: float = 0.0
    opened_ts: float | None = None
    samples: int = 0
    lead_flips: int = 0
    last_lead: str | None = None
    peak_ce: float = 0.0
    peak_pe: float = 0.0
    trough_ce: float = 0.0
    trough_pe: float = 0.0
    max_lead_x: float = 1.0
    min_lag_x: float = 1.0
    locked_type: str | None = None  # once STRETCH/BREAKOUT firm, keep until flat
    snap: DayTypeSnapshot = field(default_factory=DayTypeSnapshot)

    def reset(self, *, ce_entry: float, pe_entry: float, ts: float | None = None) -> None:
        self.ce_entry = float(ce_entry)
        self.pe_entry = float(pe_entry)
        self.opened_ts = ts
        self.samples = 0
        self.lead_flips = 0
        self.last_lead = None
        self.peak_ce = float(ce_entry)
        self.peak_pe = float(pe_entry)
        self.trough_ce = float(ce_entry)
        self.trough_pe = float(pe_entry)
        self.max_lead_x = 1.0
        self.min_lag_x = 1.0
        self.locked_type = None
        self.snap = DayTypeSnapshot()

    def update(
        self,
        *,
        ce_ltp: float,
        pe_ltp: float,
        ts: float | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> DayTypeSnapshot:
        cfg = cfg or {}
        if self.ce_entry <= 0 or self.pe_entry <= 0:
            return self.snap

        self.samples += 1
        self.peak_ce = max(self.peak_ce, ce_ltp)
        self.peak_pe = max(self.peak_pe, pe_ltp)
        self.trough_ce = min(self.trough_ce, ce_ltp)
        self.trough_pe = min(self.trough_pe, pe_ltp)

        ce_x = ce_ltp / self.ce_entry
        pe_x = pe_ltp / self.pe_entry
        lead = "CE" if ce_x >= pe_x else "PE"
        if self.last_lead and lead != self.last_lead:
            self.lead_flips += 1
        self.last_lead = lead
        lead_x = ce_x if lead == "CE" else pe_x
        lag_x = pe_x if lead == "CE" else ce_x
        self.max_lead_x = max(self.max_lead_x, lead_x)
        self.min_lag_x = min(self.min_lag_x, lag_x)

        minutes = 0.0
        if self.opened_ts is not None and ts is not None and ts >= self.opened_ts:
            minutes = (ts - self.opened_ts) / 60.0

        early_min = float(cfg.get("day_type_early_minutes", 50) or 50)
        decay_lead = float(cfg.get("day_type_decay_lead_max", 1.20) or 1.20)
        decay_flips = int(cfg.get("day_type_decay_flips_min", 4) or 4)
        trend_lead = float(cfg.get("day_type_trend_lead_min", 1.35) or 1.35)
        stretch_lead = float(cfg.get("day_type_stretch_lead_min", 1.60) or 1.60)
        split_lag = float(cfg.get("day_type_split_lag_max", 0.75) or 0.75)
        split_lead = float(cfg.get("day_type_split_lead_min", 1.25) or 1.25)
        breakout_x = float(cfg.get("day_type_breakout_x", 2.0) or 2.0)
        stretch_x = float(cfg.get("day_type_stretch_x", 3.0) or 3.0)
        base_exit = float(cfg.get("algo_cw_pair_exit_mult", 1.45) or 1.45)

        reasons: list[str] = []
        day_type = self.locked_type or "NORMAL"

        # Realized stretch / breakout (hard locks)
        if self.max_lead_x >= stretch_x:
            day_type = "STRETCH_3X"
            reasons.append(f"lead peaked {self.max_lead_x:.2f}× (≥{stretch_x:g}×)")
            self.locked_type = day_type
        elif self.max_lead_x >= breakout_x:
            day_type = "BREAKOUT_2X"
            reasons.append(f"lead peaked {self.max_lead_x:.2f}× (≥{breakout_x:g}×)")
            self.locked_type = day_type
        elif self.min_lag_x <= split_lag and self.max_lead_x >= split_lead:
            day_type = "FALLEN_REVERSE"
            reasons.append(
                f"SPLIT lag≤{split_lag:g}× lead≥{split_lead:g}× "
                f"(now lag {lag_x:.2f}× lead {lead_x:.2f}×)"
            )
        elif minutes >= early_min * 0.7 and self.max_lead_x < decay_lead and self.lead_flips >= decay_flips:
            day_type = "DECAY"
            reasons.append(
                f"early chop lead<{decay_lead:g}× flips={self.lead_flips} (≥{decay_flips})"
            )
        elif minutes >= early_min and self.max_lead_x < 1.25 and lead_x < 1.20:
            day_type = "DECAY"
            reasons.append(f"after {minutes:.0f}m still soft lead {lead_x:.2f}×")
        elif self.max_lead_x >= stretch_lead and self.lead_flips <= 2:
            day_type = "BREAKOUT_2X"
            reasons.append(f"EARLY_STRETCH lead≥{stretch_lead:g}× clean flips={self.lead_flips}")
            self.locked_type = "BREAKOUT_2X"
        elif self.max_lead_x >= trend_lead and self.lead_flips <= 3:
            day_type = "BREAKOUT_2X"
            reasons.append(f"EARLY_TREND lead≥{trend_lead:g}× flips={self.lead_flips}")
        else:
            if not reasons:
                reasons.append("NORMAL — wait for clearer tell")

        # Exit mode mapping (trained policy)
        if day_type == "DECAY":
            exit_mode = "LOCK_145"
            wait_retrace = False
            pair_exit = base_exit
            trail_arm = None
            giveback = None
            hold_fallen = False
            skip_second = True
            advice = "DECAY: book @1.45×, no trail, skip further setups"
        elif day_type == "STRETCH_3X":
            exit_mode = "TRAIL"
            wait_retrace = True
            pair_exit = base_exit
            trail_arm = float(cfg.get("day_type_trail_arm_mult", 1.30) or 1.30)
            giveback = float(cfg.get("day_type_trail_giveback_pct", 12) or 12)
            hold_fallen = True
            skip_second = bool(cfg.get("day_type_skip_second_on_stretch", False))
            advice = "STRETCH: trail for 2–3×; hold fallen bounce watch"
        elif day_type == "BREAKOUT_2X":
            exit_mode = "TRAIL"
            wait_retrace = True
            pair_exit = base_exit
            trail_arm = float(cfg.get("day_type_trail_arm_mult", 1.30) or 1.30)
            giveback = float(cfg.get("day_type_trail_giveback_pct", 10) or 10)
            hold_fallen = True
            skip_second = False
            advice = "BREAKOUT: trail after arm; allow 2× runner"
        elif day_type == "FALLEN_REVERSE":
            exit_mode = "FALLEN_WATCH"
            wait_retrace = False  # book winner at 1.45; manage fallen separately
            pair_exit = base_exit
            trail_arm = None
            giveback = None
            hold_fallen = True
            skip_second = True  # lock green after first winner book
            advice = "FALLEN_REVERSE: book winner @1.45×; watch fallen bounce for add — no knife catch"
        else:
            exit_mode = "LOCK_145"
            wait_retrace = False
            pair_exit = base_exit
            trail_arm = None
            giveback = None
            hold_fallen = False
            skip_second = bool(cfg.get("algo_cw_skip_second_if_day_pnl_ge", 1) or 0) > 0
            advice = "NORMAL: default lock @1.45 until day type clarifies"

        self.snap = DayTypeSnapshot(
            day_type=day_type,
            exit_mode=exit_mode,
            winner=lead,
            fallen="PE" if lead == "CE" else "CE",
            lead_x=lead_x,
            lag_x=lag_x,
            lead_flips=self.lead_flips,
            samples=self.samples,
            minutes_open=minutes,
            wait_retrace=wait_retrace,
            pair_exit_mult=pair_exit,
            trail_arm_mult=trail_arm,
            trail_giveback_pct=giveback,
            hold_fallen=hold_fallen,
            skip_second=skip_second,
            advice=advice,
            reasons=reasons,
        )
        return self.snap


def apply_day_type_to_cfg(cfg: dict[str, Any], snap: DayTypeSnapshot) -> dict[str, Any]:
    """Return a shallow-copied cfg with exit knobs overridden by day type."""
    if not bool(cfg.get("day_type_adaptive", True)):
        return cfg
    out = dict(cfg)
    if snap.pair_exit_mult is not None:
        out["algo_cw_pair_exit_mult"] = float(snap.pair_exit_mult)
    out["algo_cw_pair_exit_wait_retrace"] = bool(snap.wait_retrace)
    if snap.trail_giveback_pct is not None:
        out["algo_cw_pair_exit_retrace_min_pct"] = max(5.0, float(snap.trail_giveback_pct) - 2)
        out["algo_cw_pair_exit_retrace_max_pct"] = float(snap.trail_giveback_pct) + 5
    if snap.hold_fallen:
        out["algo_cw_pair_hold_fallen_below"] = float(
            cfg.get("algo_cw_pair_hold_fallen_below", 45) or 45
        )
    if snap.skip_second:
        # any green / any first setup complete → skip (trainer preference)
        out["algo_cw_skip_second_if_day_pnl_ge"] = min(
            float(cfg.get("algo_cw_skip_second_if_day_pnl_ge") or 1),
            1.0,
        )
    out["_day_type"] = snap.as_dict()
    return out

"""
Algo1 current-week dual convergence (70–77 @ 1.45×) + trained day-type exits.

Lives only on Algo1 — does not modify Algo-CW / Algo-NW.
Subclass of AlgoCwDual for shared merge/manage plumbing; overrides cfg + day-type.
"""
from __future__ import annotations

import time
from typing import Any

from foxtrade.algo_cw_dual import AlgoCwDual
from foxtrade.day_type import DayTypeTracker, apply_day_type_to_cfg


class Algo1ConvergenceDual(AlgoCwDual):
    """Algo1-only dual book with causal day-type adaptive exits."""

    def __init__(self) -> None:
        super().__init__(
            algo_id="algo1",
            display_name="Algo1 Convergence Dual",
            week_mode="current",
        )
        self.day_type = DayTypeTracker()

    def resolve_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        out = dict(cfg)
        # Parent gates on algo_cw_enabled — map Algo1 enable here
        out["algo_cw_enabled"] = bool(cfg.get("algo1_enabled", False)) or bool(
            cfg.get("algo1_convergence_dual", False)
        )
        out["premium_low"] = float(cfg.get("algo1_premium_low", cfg.get("premium_low", 70)) or 70)
        out["premium_high"] = float(cfg.get("algo1_premium_high", cfg.get("premium_high", 77)) or 77)
        out["algo_cw_pair_exit_mult"] = float(cfg.get("algo1_pair_exit_mult", 1.45) or 1.45)
        out["algo_cw_max_setups_per_day"] = int(cfg.get("algo1_max_setups_per_day", 2) or 2)
        out["algo_cw_second_entry_after"] = str(cfg.get("algo1_second_entry_after", "11:00") or "11:00")
        out["algo_cw_last_entry_before"] = str(cfg.get("algo1_last_entry_before", "13:00") or "13:00")
        out["algo_cw_skip_second_if_day_pnl_ge"] = float(
            cfg.get("algo1_skip_second_if_day_pnl_ge", 1) or 1
        )
        out["algo_cw_merge_after"] = str(cfg.get("algo1_merge_after", "09:20") or "09:20")
        out["algo_cw_pair_exit_wait_retrace"] = bool(cfg.get("algo1_pair_exit_wait_retrace", False))
        out["algo_cw_reentry_cooldown_minutes"] = int(cfg.get("algo1_reentry_cooldown_minutes", 1) or 1)
        out["algo_cw_dual_size_from_capital"] = bool(cfg.get("algo1_dual_size_from_capital", True))
        if cfg.get("algo1_lots") is not None:
            out["algo_cw_lots"] = int(cfg.get("algo1_lots") or 1)
        cap = cfg.get("algo1_capital_inr")
        if cap is not None:
            out["capital_inr"] = float(cap)
        dep = cfg.get("algo1_deploy_pct")
        if dep is not None:
            out["deploy_pct_of_balance"] = float(dep)
        # Day-type knobs (Algo1-trained)
        out["day_type_adaptive"] = bool(cfg.get("algo1_day_type_adaptive", True))
        for k in (
            "day_type_early_minutes",
            "day_type_decay_lead_max",
            "day_type_decay_flips_min",
            "day_type_trend_lead_min",
            "day_type_stretch_lead_min",
            "day_type_split_lag_max",
            "day_type_split_lead_min",
            "day_type_breakout_x",
            "day_type_stretch_x",
            "day_type_trail_arm_mult",
            "day_type_trail_giveback_pct",
        ):
            ak = f"algo1_{k}"
            if cfg.get(ak) is not None:
                out[k] = cfg.get(ak)
            elif cfg.get(k) is not None:
                out[k] = cfg.get(k)
        return out

    def runtime_snapshot(self) -> dict[str, Any]:
        snap = super().runtime_snapshot()
        snap["day_type"] = self.day_type.snap.as_dict()
        return snap

    def _enter_dual(self, *args: Any, **kwargs: Any) -> None:
        super()._enter_dual(*args, **kwargs)
        if self.state.phase == "DUAL_OPEN" and self.state.ce_entry and self.state.pe_entry:
            self.day_type.reset(
                ce_entry=float(self.state.ce_entry),
                pe_entry=float(self.state.pe_entry),
                ts=float(time.time()),
            )

    def _manage(
        self,
        cfg: dict[str, Any],
        ce_ltp: float,
        pe_ltp: float,
        index: dict[str, Any],
        release: Any,
        pair_regime: Any = None,
        ctx: dict[str, Any] | None = None,
    ) -> None:
        if self.state.phase == "DUAL_OPEN" and self.state.ce_entry and self.state.pe_entry:
            if self.day_type.ce_entry <= 0:
                self.day_type.reset(
                    ce_entry=float(self.state.ce_entry),
                    pe_entry=float(self.state.pe_entry),
                    ts=float(time.time()),
                )
            try:
                dt_snap = self.day_type.update(
                    ce_ltp=float(ce_ltp),
                    pe_ltp=float(pe_ltp),
                    ts=float(time.time()),
                    cfg=cfg,
                )
                cfg = apply_day_type_to_cfg(cfg, dt_snap)
                if dt_snap.day_type not in ("UNKNOWN", "NORMAL") or self.day_type.samples % 30 == 1:
                    self.state.last_message = (
                        f"A1 day:{dt_snap.day_type}/{dt_snap.exit_mode} "
                        f"lead {dt_snap.lead_x:.2f}× · {dt_snap.advice}"
                    )
            except Exception:
                pass
        super()._manage(cfg, ce_ltp, pe_ltp, index, release, pair_regime, ctx)

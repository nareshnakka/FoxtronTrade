"""
Underlying profiles for dual live book (Nifty FastTrack + Sensex FastTrackSensex).
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from foxtrade.sensex_calendar import (
    SENSEX_CONDITIONAL_WEEKDAYS,
    SENSEX_EXPIRY_WEEKDAY,
    SENSEX_TRADE_WEEKDAYS,
    is_sensex_trade_day,
)

ROOT = Path(__file__).resolve().parents[1]
SENSEX_OVERRIDES = ROOT / "data" / "sensex" / "fasttrack_sensex_overrides.json"
NIFTY_OVERRIDES = ROOT / "data" / "nifty" / "fasttrack_profit_overrides.json"

# Spot / options venue metadata
NIFTY: dict[str, Any] = {
    "id": "NIFTY",
    "algo_id": "algo1",
    "display": "Nifty FastTrack",
    "spot_symbol": "NSE:NIFTY 50",
    "spot_token": 256265,
    "opt_exchange": "NFO",  # kite.instruments + LTP prefix
    "opt_name": "NIFTY",
    "ltp_prefix": "NFO",
    "lot_size": 75,
    "strike_step": 50,
    "strike_window": 600,
    "premium_low": 125,
    "premium_high": 145,
    "wide_low": 40,
    "wide_high": 220,
    "expiry_weekday": 1,  # Tuesday
}

SENSEX: dict[str, Any] = {
    "id": "SENSEX",
    "algo_id": "algo1_sx",
    "display": "Sensex FastTrackSensex",
    "spot_symbol": "BSE:SENSEX",
    "spot_token": 265,
    "opt_exchange": "BFO",
    "opt_name": "SENSEX",
    "ltp_prefix": "BFO",
    "lot_size": 20,
    "strike_step": 100,
    "strike_window": 2000,
    "premium_low": 180,
    "premium_high": 280,
    "wide_low": 50,
    "wide_high": 450,
    "expiry_weekday": 1,  # Tuesday (BSE Sensex weekly)
}

BY_ID = {"NIFTY": NIFTY, "SENSEX": SENSEX}


def list_underlyings(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    c = cfg or {}
    ids = c.get("dual_underlyings") or ["NIFTY", "SENSEX"]
    out = []
    for i in ids:
        u = BY_ID.get(str(i).upper())
        if u:
            out.append(dict(u))
    return out or [dict(NIFTY)]


def load_sensex_overrides() -> dict[str, Any]:
    if not SENSEX_OVERRIDES.exists():
        return {
            "profile_name": "FastTrackSensex",
            "algo_name": "FastTrackSensex",
            "underlying": "SENSEX",
            "lot_size": 20,
            "enable_daily_pa": False,
            "enable_micro_scalp": True,
            "enable_daily_soft_scalp": False,
            "enable_sr_reclaim": True,
            "enable_sr_reject": False,
            "enable_orb_breakout": False,
            "enable_orb_breakdown": False,
            "hedge_mode": False,
            "post_decay_entry_enabled": False,
            "post_decay_standalone_entry": False,
        }
    return json.loads(SENSEX_OVERRIDES.read_text(encoding="utf-8"))


def load_nifty_overrides() -> dict[str, Any]:
    if not NIFTY_OVERRIDES.exists():
        return {}
    try:
        return json.loads(NIFTY_OVERRIDES.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cfg_for_underlying(base: dict[str, Any], underlying_id: str) -> dict[str, Any]:
    """Merge base live config with underlying-specific FastTrack / Sensex gates."""
    uid = str(underlying_id).upper()
    spec = BY_ID.get(uid) or NIFTY
    cfg = deepcopy(base)
    cfg["underlying"] = uid
    cfg["lot_size"] = int(spec["lot_size"])
    cfg["premium_low"] = float(spec["premium_low"])
    cfg["premium_high"] = float(spec["premium_high"])
    # Live capital / deploy always win over backtest override files
    live_capital = float(base.get("capital_inr") or 100_000)
    live_deploy = float(base.get("deploy_pct_of_balance") or 1.0)
    live_max_lots = int(base.get("max_lots_per_trade") or 15)
    if uid == "SENSEX":
        cfg.update(load_sensex_overrides())
        cfg["underlying"] = "SENSEX"
        cfg["lot_size"] = int(spec["lot_size"])
        cfg["premium_low"] = float(cfg.get("premium_low") or spec["premium_low"])
        cfg["premium_high"] = float(cfg.get("premium_high") or spec["premium_high"])
        cfg["profile_name"] = "FastTrackSensex"
        cfg["algo_name"] = "FastTrackSensex"
        # Calendar: Tue/Wed always; Thu = gamma-only; Mon = day-before-expiry cutoff
        cfg.setdefault("sensex_expiry_weekday", int(spec.get("expiry_weekday", SENSEX_EXPIRY_WEEKDAY)))
        cfg.setdefault("sensex_trade_weekdays", list(SENSEX_TRADE_WEEKDAYS))
        cfg.setdefault("sensex_conditional_weekdays", list(SENSEX_CONDITIONAL_WEEKDAYS))
        cfg.setdefault("sensex_thursday_gamma_enabled", True)
        cfg.setdefault("sensex_thursday_gamma_only", True)
        cfg.setdefault("sensex_gamma_detect_enabled", True)
        cfg.setdefault("sensex_block_pre_expiry", True)
        cfg.setdefault("sensex_pre_expiry_weekday", 0)
        cfg["sensex_trade_today"] = is_sensex_trade_day(cfg=cfg)
    else:
        nov = load_nifty_overrides()
        if nov:
            cfg.update(nov)
        cfg["underlying"] = "NIFTY"
        cfg["lot_size"] = int(spec["lot_size"])
        cfg["premium_low"] = float(cfg.get("premium_low") or spec["premium_low"])
        cfg["premium_high"] = float(cfg.get("premium_high") or spec["premium_high"])
        cfg["profile_name"] = "FastTrack"
        cfg["algo_name"] = "FastTrack"
        cfg["hedge_mode"] = False
    cfg["capital_inr"] = live_capital
    cfg["deploy_pct_of_balance"] = live_deploy
    # Cap lots for live 1L book (override files may carry backtest 150/75)
    cfg["max_lots_per_trade"] = min(int(cfg.get("max_lots_per_trade") or live_max_lots), live_max_lots)
    cfg["min_lots_per_trade"] = min(int(cfg.get("min_lots_per_trade") or 1), cfg["max_lots_per_trade"])

    # Live loose_entry_mode must beat backtest override JSONs (they re-lock
    # conf 0.7 / vs-open 100+ and were why dual never entered on big days).
    if base.get("loose_entry_mode"):
        for k in (
            "loose_entry_mode",
            "algo1_enabled",
            "algo_cw_enabled",
            "directional_min_confidence",
            "directional_min_margin",
            "algo1_min_confidence",
            "require_structure_event",
            "require_momentum_align",
            "require_premium_confirm",
            "require_session_extreme",
            "require_hard_trend",
            "stability_mode",
            "block_orb_chase_without_retest",
            "block_orb_after_failed_break",
            "block_structure_in_chop",
            "block_ce_on_sharp_fall",
            "block_ce_low_efficiency",
            "block_late_sr_in_range",
            "block_ce_mid_chase",
            "block_ce_breakout_falling_mom",
            "trend_block_counter",
            "post_decay_require_spot_align",
            "signal_persist_minutes",
            "entry_cooldown_minutes",
            "min_vs_open_entry",
            "ce_min_vs_open",
            "pe_min_vs_open",
            "ce_min_vs_open_break",
            "pe_min_vs_open_break",
            "min_path_efficiency",
            "ce_min_path_efficiency",
            "ce_day_min_efficiency",
            "min_break_strength",
            "trend_soft_pts",
            "trend_hard_pts",
            "trend_confirm_minutes",
            "enable_orb_breakout",
            "enable_orb_breakdown",
            "enable_trend_follow",
            "enable_sr_reclaim",
            "enable_sr_reject",
            "enable_micro_scalp",
            "enable_daily_soft_scalp",
            "enable_daily_pa",
            "daily_pa_only_if_flat",
            "daily_soft_only_if_flat",
            "micro_scalp_only_if_flat",
            "daily_pa_allow_mom_break",
            "micro_scalp_both_sides",
            "allow_shakeout_entry",
            "breakout_max_extension_pts",
            "pe_breakdown_min_mom",
            "ce_breakout_min_mom",
            "max_trades_per_day",
            "max_losses_before_halt",
            "bank_after_win",
            "fast_open_mode",
            "impulse_entry_enabled",
            "pre_event_entry_enabled",
            "post_decay_entry_enabled",
            "atm_channel_enabled",
            "atm_channel_entries",
        ):
            if k in base:
                cfg[k] = base[k]
    # Settings toggles always beat FastTrack / Sensex override files.
    for k in (
        "algo1_enabled",
        "algo_cw_enabled",
        "enable_orb_breakout",
        "enable_orb_breakdown",
        "enable_trend_follow",
        "enable_sr_reclaim",
        "enable_sr_reject",
        "enable_daily_pa",
        "enable_daily_soft_scalp",
        "enable_micro_scalp",
        "allow_shakeout_entry",
        "impulse_entry_enabled",
        "pre_event_entry_enabled",
        "post_decay_entry_enabled",
        "post_decay_standalone_entry",
        "atm_channel_enabled",
        "atm_channel_entries",
        "atm_channel_allow_edge",
        "pe_range_enabled",
        "pe_range_entries",
        "pe_range_allow_edge",
        "pe_range_allow_break",
        "fii_oi_scan_enabled",
    ):
        if k in base:
            cfg[k] = base[k]
    return cfg

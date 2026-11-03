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
    return cfg

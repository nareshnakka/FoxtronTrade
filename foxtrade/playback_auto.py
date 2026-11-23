"""
Auto-replay: always run the *current* algo config over option_chain_1m days.

Playback APIs call this so foxtron never needs a manual tools_playback_week.py
after algo changes.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from foxtrade.config_store import load_config
from foxtrade.playback_sim import simulate_day

ROOT = Path(__file__).resolve().parent.parent
CHAIN = ROOT / "data" / "option_chain_1m"
OUT = ROOT / "data" / "backtests" / "playback"

_lock = threading.Lock()
# day -> (cfg_fp, sim)
_cache: dict[str, tuple[str, dict[str, Any]]] = {}
_week_fp: str | None = None
_week_summary: dict[str, Any] | None = None


def _cfg_fingerprint(cfg: dict[str, Any]) -> str:
    """Hash algo-relevant knobs (stable JSON)."""
    keys = sorted(
        k
        for k in cfg
        if k.startswith(
            (
                "algo_cw_",
                "early_winner_",
                "convergence_",
                "breakout_",
                "premium_",
                "fallen_",
                "crush_",
                "expand_",
                "atm_diag_",
                "lot_",
            )
        )
        or k
        in (
            "lot_size",
            "algo_cw_lots",
            "algo_cw_enabled",
            "algo_cw_breakout_only",
            "algo_cw_breakout_hard_gap",
            "live_trading",
            "playback_chart_atm",
            "playback_chart_ce_strike",
            "playback_chart_pe_strike",
        )
    )
    payload = {k: cfg.get(k) for k in keys}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def available_chain_days() -> list[str]:
    if not CHAIN.exists():
        return []
    days = []
    for p in sorted(CHAIN.iterdir()):
        if p.is_dir() and (p / "viewer.json").exists():
            days.append(p.name)
    return days


def simulate_day_live(day: str, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run simulate_day with current config; cache until config fingerprint changes."""
    cfg = cfg or load_config()
    fp = _cfg_fingerprint(cfg)
    with _lock:
        hit = _cache.get(day)
        if hit and hit[0] == fp:
            return hit[1]
    chain = CHAIN / day / "viewer.json"
    if not chain.exists():
        return {"day": day, "error": f"no chain data for {day}", "frames": [], "trades": []}
    sim = simulate_day(day, cfg)
    sim["auto_replay"] = True
    sim["algo_fp"] = fp
    # Attach same-day live/paper Algo-CW fills for side-by-side review
    try:
        trades_path = ROOT / "data" / "trades.json"
        if trades_path.exists():
            all_tr = json.loads(trades_path.read_text(encoding="utf-8"))
            merge_after = str(cfg.get("algo_cw_merge_after") or "09:20")
            live = [
                t
                for t in all_tr
                if isinstance(t, dict)
                and str(t.get("opened_at") or "").startswith(day)
                and t.get("algo") == "algo_cw"
            ]
            rows = []
            for t in live:
                entry_t = str(t.get("opened_at") or "")[11:16]
                pre = bool(entry_t and entry_t < merge_after)
                rows.append(
                    {
                        "side": t.get("side"),
                        "role": "live_paper_pre_0920" if pre else "live_paper",
                        "symbol": t.get("symbol"),
                        "strike": t.get("strike"),
                        "entry_t": entry_t,
                        "exit_t": str(t.get("closed_at") or "")[11:16],
                        "entry": t.get("entry_price"),
                        "exit": t.get("exit_price"),
                        "pnl": t.get("pnl_points"),
                        "reason": (
                            f"BEFORE {merge_after} (ignored vs playback) · "
                            f"{t.get('exit_reason') or ''}"
                            if pre
                            else t.get("exit_reason")
                        ),
                        "net": t.get("net_pnl_rupees"),
                    }
                )
            sim["live_paper_trades"] = rows
    except Exception:
        sim["live_paper_trades"] = []
    # Persist for reports / offline open
    ddir = OUT / day
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "sim.json").write_text(json.dumps(sim), encoding="utf-8")
    with _lock:
        _cache[day] = (fp, sim)
    return sim


def week_summary_live(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resim all chain days with current algo; refresh week_summary.json."""
    global _week_fp, _week_summary
    cfg = cfg or load_config()
    fp = _cfg_fingerprint(cfg)
    days = available_chain_days()
    with _lock:
        if _week_summary is not None and _week_fp == fp and _week_summary.get("available_days") == days:
            return _week_summary

    mode = "breakout_only" if cfg.get("algo_cw_breakout_only", True) else "dual"
    week: dict[str, Any] = {
        "mode": mode,
        "algo": "algo_cw",
        "algo_fp": fp,
        "auto_replay": True,
        "days": [],
        "net": 0.0,
        "gross": 0.0,
        "charges": 0.0,
        "available_days": days,
    }
    for day in days:
        sim = simulate_day_live(day, cfg=cfg)
        s = sim.get("summary") or {}
        week["days"].append(s)
        week["net"] += float(s.get("net") or 0)
        week["gross"] += float(s.get("gross") or 0)
        week["charges"] += float(s.get("charges") or 0)
    week["net"] = round(week["net"], 2)
    week["gross"] = round(week["gross"], 2)
    week["charges"] = round(week["charges"], 2)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "week_summary.json").write_text(json.dumps(week, indent=2), encoding="utf-8")

    with _lock:
        _week_fp = fp
        _week_summary = week
    return week


def invalidate_cache() -> None:
    global _week_fp, _week_summary
    with _lock:
        _cache.clear()
        _week_fp = None
        _week_summary = None

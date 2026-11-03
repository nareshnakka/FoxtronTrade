"""
Calibrate directional algo on last-3-months spot minutes (offline).

Uses a synthetic ATM-premium path (delta≈0.5 + mild theta) so we can
score entry/exit rules across ~82 days without expired option chains.

Writes:
  data/backtests/calibration_report.json
  updates config.json with best params when --apply
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.config_store import load_config, save_config, CONFIG_PATH, _DEFAULTS
from foxtrade.expert_filters import (
    daily_loss_halted,
    manage_percent_exit,
    size_lots,
    stop_price,
    target_price,
)
from foxtrade.market_direction import analyze_market

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "expiries" / "last_3_months"
OUT = ROOT / "data" / "backtests"


@dataclass
class Trade:
    side: str
    entry_i: int
    entry_px: float
    entry_spot: float
    exit_i: int | None = None
    exit_px: float | None = None
    reason: str = ""
    qty: int = 75
    peak: float = 0.0
    stop_px: float = 0.0
    target_px: float = 0.0

    @property
    def pnl(self) -> float:
        if self.exit_px is None:
            return 0.0
        return (self.exit_px - self.entry_px) * self.qty


@dataclass
class DayResult:
    day: str
    pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    setups: int = 0
    trades: list[dict[str, Any]] = field(default_factory=list)
    spot_chg: float = 0.0
    skipped: str = ""


def _load_days() -> list[dict[str, Any]]:
    meta = json.loads((DATA / "all_expiries_every_day.json").read_text(encoding="utf-8"))
    days: list[dict[str, Any]] = []
    for exp in meta.get("expiries") or []:
        for d in exp.get("days") or []:
            if not d.get("session"):
                continue
            mf = d.get("minutes_file")
            if not mf:
                continue
            path = DATA / mf
            if not path.exists():
                continue
            days.append(
                {
                    "date": d["date"],
                    "is_expiry_day": bool(d.get("is_expiry_day")),
                    "expiry_week": d.get("expiry_week"),
                    "minutes_path": path,
                    "spot": d.get("spot") or {},
                }
            )
    days.sort(key=lambda x: x["date"])
    return days


def _load_closes(path: Path) -> list[float]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [float(r["close"]) for r in rows]


def _synth_premium(
    spot_path: list[float],
    side: str,
    entry_i: int,
    entry_prem: float,
    *,
    delta: float = 0.50,
    theta_per_min: float = 0.015,
) -> list[float]:
    """
    Causal synthetic premium from entry bar onward.
    CE: +delta * Δspot − theta; PE: −delta * Δspot − theta.
    Floored at 5.
    """
    s0 = spot_path[entry_i]
    out: list[float] = []
    for i in range(entry_i, len(spot_path)):
        ds = float(spot_path[i]) - s0
        held = i - entry_i
        if side == "CE":
            px = entry_prem + delta * ds - theta_per_min * held
        else:
            px = entry_prem - delta * ds - theta_per_min * held
        out.append(max(5.0, px))
    return out


def _session_end_idx(cfg: dict[str, Any], n: int) -> int:
    # minutes from 09:15; default session_end 13:30 → 255 bars
    start = str(cfg.get("session_start", "09:15"))
    end = str(cfg.get("session_end", "13:30"))
    sh, sm = map(int, start.split(":")[:2])
    eh, em = map(int, end.split(":")[:2])
    mins = (eh * 60 + em) - (sh * 60 + sm)
    return min(n - 1, max(30, mins))


def simulate_day(
    closes: list[float],
    cfg: dict[str, Any],
    *,
    is_expiry_day: bool = False,
    day: str = "",
) -> DayResult:
    res = DayResult(day=day, spot_chg=(closes[-1] - closes[0]) if closes else 0.0)
    if len(closes) < 40:
        res.skipped = "too few bars"
        return res

    end_i = _session_end_idx(cfg, len(closes))
    entry_prem = (
        float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))
    ) / 2.0
    lot = int(cfg.get("lot_size", 75))
    max_setups = int(cfg.get("max_trades_per_day", 4))
    cooldown = int(cfg.get("entry_cooldown_minutes", 25))
    delta = float(cfg.get("synth_delta", 0.50))
    theta = float(cfg.get("synth_theta_per_min", 0.015))

    open_tr: Trade | None = None
    last_exit_i = -10_000
    losses = 0
    wins = 0
    day_pnl = 0.0

    for i in range(1, len(closes)):
        path = closes[: i + 1]

        # manage open
        if open_tr is not None:
            prem_path = _synth_premium(
                closes, open_tr.side, open_tr.entry_i, open_tr.entry_px, delta=delta, theta_per_min=theta
            )
            cur = prem_path[i - open_tr.entry_i]
            held = float(i - open_tr.entry_i)
            dec = manage_percent_exit(
                entry=open_tr.entry_px,
                px=cur,
                peak=open_tr.peak,
                cfg=cfg,
                held_minutes=held,
            )
            open_tr.peak = float(dec.get("peak") or open_tr.peak)
            open_tr.stop_px = float(dec.get("stop_px") or open_tr.stop_px)
            open_tr.target_px = float(dec.get("target_px") or open_tr.target_px)
            force_eod = i >= end_i
            if dec.get("action") == "exit" or force_eod:
                open_tr.exit_i = i
                open_tr.exit_px = cur
                open_tr.reason = "session end" if force_eod and dec.get("action") != "exit" else str(dec.get("reason"))
                pnl = open_tr.pnl
                day_pnl += pnl
                if pnl > 0:
                    wins += 1
                elif pnl < 0:
                    losses += 1
                res.trades.append(
                    {
                        "side": open_tr.side,
                        "entry_i": open_tr.entry_i,
                        "exit_i": open_tr.exit_i,
                        "entry_px": round(open_tr.entry_px, 2),
                        "exit_px": round(open_tr.exit_px, 2),
                        "pnl": round(pnl, 2),
                        "reason": open_tr.reason,
                        "mult": round(open_tr.exit_px / open_tr.entry_px, 3),
                    }
                )
                last_exit_i = i
                open_tr = None
                if force_eod:
                    break
            continue

        if i > end_i:
            break
        if res.setups >= max_setups:
            continue
        if i - last_exit_i < cooldown:
            continue
        if daily_loss_halted(day_pnl, cfg):
            continue
        if losses >= int(cfg.get("max_losses_before_halt", 1)):
            continue
        if cfg.get("bank_after_win", True) and day_pnl > 0 and wins >= 1:
            continue  # one green and done

        analysis = analyze_market(path, cfg=cfg, is_expiry_day=is_expiry_day)
        if not analysis.get("allow_entry"):
            continue
        side = analysis.get("side")
        if side not in ("CE", "PE"):
            continue

        # After a green day trade, require fresh breakout strength
        if cfg.get("bank_after_win", True) and day_pnl > 0 and wins >= 1:
            br = analysis.get("break") or {}
            if float(br.get("strength") or 0) < 0.65:
                continue

        lots = size_lots(cfg, entry=entry_prem)
        if losses >= 2:
            lots = max(1, lots // 2)
        open_tr = Trade(
            side=side,
            entry_i=i,
            entry_px=entry_prem,
            entry_spot=closes[i],
            qty=lots * lot,
            peak=entry_prem,
            stop_px=stop_price(entry_prem, cfg),
            target_px=target_price(entry_prem, cfg, trail=True),
        )
        res.setups += 1

    res.pnl = round(day_pnl, 2)
    res.wins = wins
    res.losses = losses
    return res


def run_period(cfg: dict[str, Any], days: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    days = days or _load_days()
    results: list[DayResult] = []
    for d in days:
        closes = _load_closes(d["minutes_path"])
        # Clip to session_end window length if longer full day file
        end_i = _session_end_idx(cfg, len(closes))
        closes = closes[: end_i + 1]
        r = simulate_day(
            closes,
            cfg,
            is_expiry_day=bool(d.get("is_expiry_day")),
            day=d["date"],
        )
        results.append(r)

    traded = [r for r in results if r.setups > 0]
    green = [r for r in results if r.pnl > 0]
    red = [r for r in results if r.pnl < 0]
    flat = [r for r in results if r.pnl == 0]
    pnls = [r.pnl for r in results]
    total = sum(pnls)
    # Max drawdown on cumulative
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    win_trades = sum(r.wins for r in results)
    loss_trades = sum(r.losses for r in results)
    trade_n = win_trades + loss_trades
    green_days = len(green)
    active_days = len([r for r in results if r.setups > 0 or r.pnl != 0])

    return {
        "days": len(results),
        "traded_days": len(traded),
        "green_days": green_days,
        "red_days": len(red),
        "flat_days": len(flat),
        "green_day_pct": round(100.0 * green_days / max(len(traded), 1), 1),
        "pnl_total": round(total, 2),
        "avg_day_pnl": round(total / max(len(results), 1), 2),
        "max_dd": round(max_dd, 2),
        "wins": win_trades,
        "losses": loss_trades,
        "trade_win_pct": round(100.0 * win_trades / max(trade_n, 1), 1),
        "setups": sum(r.setups for r in results),
        "per_day": [
            {
                "date": r.day,
                "pnl": r.pnl,
                "wins": r.wins,
                "losses": r.losses,
                "setups": r.setups,
                "spot_chg": round(r.spot_chg, 1),
                "trades": r.trades,
            }
            for r in results
        ],
    }


def _score(rep: dict[str, Any]) -> float:
    """
    Heavily weight green-day rate & low red days; still need positive PnL.
    Reject configs that barely trade or lose money overall.
    """
    traded = int(rep.get("traded_days") or 0)
    green_pct = float(rep.get("green_day_pct") or 0) / 100.0
    win_pct = float(rep.get("trade_win_pct") or 0) / 100.0
    pnl = float(rep.get("pnl_total") or 0)
    dd = abs(float(rep.get("max_dd") or 0))
    red = int(rep.get("red_days") or 0)
    green = int(rep.get("green_days") or 0)
    if pnl <= 0 or traded < 15:
        return -1e6 + pnl
    # Almost-every-traded-day green is the primary goal
    return (
        green_pct * 12000.0
        + green * 40.0
        + win_pct * 3500.0
        + pnl * 0.08
        - dd * 0.45
        - red * 220.0
        + min(traded, 50) * 8.0
    )


def sweep(base: dict[str, Any], days: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    # Focused grid: green-day rate + shallow DD
    for orb_n in (30, 45, 60):
        for min_rng in (30, 40, 55):
            for max_rng in (120, 160):
                for buf in (8, 12):
                    for exit_first in (True, False):
                        for tgt_lo in (5.0, 8.0, 12.0):
                            for giveback in (5.0, 8.0):
                                for min_brk in (0.25, 0.40):
                                    candidates.append(
                                        {
                                            "entry_style": "orb",
                                            "orb_minutes": orb_n,
                                            "orb_min_range_pts": min_rng,
                                            "orb_max_range_pts": max_rng,
                                            "allow_shakeout_entry": False,
                                            "directional_min_confidence": 0.70,
                                            "directional_min_margin": 0.22,
                                            "entry_cooldown_minutes": 60,
                                            "trail_giveback_pct": giveback,
                                            "lock_profit_pct": 1.5,
                                            "max_trades_per_day": 1,
                                            "exit_at_first_target": exit_first,
                                            "target_pct_low": tgt_lo,
                                            "min_break_strength": min_brk,
                                            "max_losses_before_halt": 1,
                                            "bank_after_win": True,
                                            "require_structure_event": True,
                                            "require_momentum_align": True,
                                            "require_3bar_continue": True,
                                            "time_stop_minutes": 25,
                                            "prefer_entry_after_minutes": orb_n + 5,
                                            "entry_delay_minutes": orb_n,
                                            "break_buffer_pts": buf,
                                            "break_hold_minutes": 4,
                                            "extreme_proximity_pts": 15,
                                            "chop_score_threshold": 0.42,
                                            "trend_soft_pts": 40,
                                            "trend_hard_pts": 85,
                                            "max_daily_loss_pct": 1.5,
                                        }
                                    )
    if len(candidates) > 320:
        step = max(1, len(candidates) // 280)
        candidates = candidates[::step]

    print(f"Sweeping {len(candidates)} configs on {len(days)} days…")
    best = None
    best_score = -1e18
    ranked: list[dict[str, Any]] = []
    for i, upd in enumerate(candidates):
        cfg = deepcopy(base)
        cfg.update(upd)
        # Always percent risk envelope
        cfg.update(
            {
                "use_percent_exits": True,
                "stop_loss_pct": 5.0,
                "target_pct_low": 5.0,
                "target_pct_high": 50.0,
                "synth_delta": 0.50,
                "synth_theta_per_min": 0.015,
            }
        )
        rep = run_period(cfg, days)
        sc = _score(rep)
        row = {
            "score": round(sc, 1),
            "params": upd,
            "pnl_total": rep["pnl_total"],
            "green_day_pct": rep["green_day_pct"],
            "trade_win_pct": rep["trade_win_pct"],
            "red_days": rep["red_days"],
            "traded_days": rep["traded_days"],
            "max_dd": rep["max_dd"],
            "setups": rep["setups"],
        }
        ranked.append(row)
        if sc > best_score:
            best_score = sc
            best = {"report": rep, "params": {**cfg}, "score": sc, "summary": row}
        if (i + 1) % 40 == 0:
            print(f"  …{i+1}/{len(candidates)}  best_pnl={best['summary']['pnl_total'] if best else 0} "
                  f"green%={best['summary']['green_day_pct'] if best else 0}")

    ranked.sort(key=lambda x: -x["score"])
    return {"best": best, "top10": ranked[:10], "n_tried": len(candidates)}


def _apply_best(params: dict[str, Any]) -> None:
    """Persist calibrated keys into config.json (extend defaults if needed)."""
    # Ensure new keys exist in defaults so save_config keeps them
    keep = {
        "use_percent_exits",
        "stop_loss_pct",
        "target_pct_low",
        "target_pct_high",
        "lock_profit_pct",
        "trail_giveback_pct",
        "exit_at_first_target",
        "require_structure_event",
        "require_momentum_align",
        "require_3bar_continue",
        "no_entry_after_minutes",
        "expiry_no_entry_after_minutes",
        "range_expand_mult",
        "bank_after_win",
        "directional_min_confidence",
        "directional_min_margin",
        "entry_cooldown_minutes",
        "max_trades_per_day",
        "max_losses_before_halt",
        "time_stop_minutes",
        "prefer_entry_after_minutes",
        "entry_delay_minutes",
        "break_buffer_pts",
        "break_hold_minutes",
        "extreme_proximity_pts",
        "chop_score_threshold",
        "require_session_extreme",
        "max_daily_loss_pct",
        "risk_per_trade_pct",
        "max_lots_per_trade",
        "stop_loss_points",
        "target_points",
        "stop_loss",
        "target_low",
        "target_high",
        "entry_style",
        "orb_minutes",
        "orb_min_range_pts",
        "orb_max_range_pts",
        "allow_shakeout_entry",
        "min_break_strength",
        "min_vs_open_entry",
        "trend_soft_pts",
        "trend_hard_pts",
    }
    # Monkey-patch defaults for new keys
    for k in keep:
        if k not in _DEFAULTS and k in params:
            _DEFAULTS[k] = params[k]
    for k, v in {
        "use_percent_exits": True,
        "stop_loss_pct": 5.0,
        "target_pct_low": 5.0,
        "target_pct_high": 50.0,
        "no_entry_after_minutes": 195,
        "expiry_no_entry_after_minutes": 150,
        "range_expand_mult": 1.35,
        "require_structure_event": True,
        "require_momentum_align": True,
        "require_session_extreme": True,
        "bank_after_win": True,
        "max_daily_loss_pct": 3.0,  # allow room under 5% trade stops
        "risk_per_trade_pct": 1.0,
    }.items():
        _DEFAULTS[k] = v

    updates = {k: params[k] for k in keep if k in params}
    updates.update(
        {
            "use_percent_exits": True,
            "stop_loss_pct": 5.0,
            "target_pct_low": 5.0,
            "target_pct_high": 50.0,
            "require_structure_event": True,
            "require_momentum_align": True,
            "require_session_extreme": True,
            "no_entry_after_minutes": 195,
            "expiry_no_entry_after_minutes": 150,
            "directional_mode": True,
            "algo2_enabled": False,
            "algo1_enabled": True,
        }
    )
    # Point floors must not fight percent mode — loosen absolute bands
    updates["stop_loss"] = 50
    updates["stop_loss_points"] = 7
    updates["target_low"] = 100
    updates["target_high"] = 220
    updates["target_points"] = 7

    # Direct write merge (save_config filters unknown keys)
    data = load_config()
    data.update(updates)
    # Also force-write any new keys
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Applied calibrated config → {CONFIG_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write best params to config.json")
    ap.add_argument("--quick", action="store_true", help="Single baseline run, no sweep")
    ap.add_argument("--last", type=int, default=0, help="Only last N days")
    args = ap.parse_args()

    base = load_config()
    base.update(
        {
            "use_percent_exits": True,
            "stop_loss_pct": 5.0,
            "target_pct_low": 5.0,
            "target_pct_high": 50.0,
            "lock_profit_pct": 1.0,
            "trail_giveback_pct": 8.0,
            "exit_at_first_target": False,
            "require_structure_event": True,
            "require_momentum_align": True,
            "bank_after_win": True,
            "max_trades_per_day": 3,
            "entry_cooldown_minutes": 30,
            "directional_min_confidence": 0.68,
            "directional_min_margin": 0.22,
            "time_stop_minutes": 22,
            "prefer_entry_after_minutes": 25,
            "chop_score_threshold": 0.45,
            "break_buffer_pts": 10,
            "extreme_proximity_pts": 12,
            "synth_delta": 0.50,
            "synth_theta_per_min": 0.015,
        }
    )

    days = _load_days()
    if args.last > 0:
        days = days[-args.last :]
    print(f"Loaded {len(days)} session days from {DATA}")

    OUT.mkdir(parents=True, exist_ok=True)

    if args.quick:
        rep = run_period(base, days)
        payload = {"mode": "quick", "report": rep, "params": base}
        path = OUT / "calibration_report.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(
            f"PnL ₹{rep['pnl_total']:,.0f} | green-days {rep['green_day_pct']}% "
            f"of {rep['traded_days']} traded | win% {rep['trade_win_pct']} | "
            f"red_days {rep['red_days']} | DD ₹{rep['max_dd']:,.0f}"
        )
        print(f"Wrote {path}")
        return

    swept = sweep(base, days)
    best = swept["best"]
    assert best
    payload = {
        "mode": "sweep",
        "n_tried": swept["n_tried"],
        "top10": swept["top10"],
        "best_summary": best["summary"],
        "best_report": {
            k: best["report"][k]
            for k in best["report"]
            if k != "per_day"
        },
        "best_per_day": best["report"]["per_day"],
        "best_params": {
            k: best["params"][k]
            for k in (
                "directional_min_confidence",
                "directional_min_margin",
                "entry_cooldown_minutes",
                "trail_giveback_pct",
                "lock_profit_pct",
                "max_trades_per_day",
                "exit_at_first_target",
                "bank_after_win",
                "time_stop_minutes",
                "prefer_entry_after_minutes",
                "break_buffer_pts",
                "extreme_proximity_pts",
                "chop_score_threshold",
                "use_percent_exits",
                "stop_loss_pct",
                "target_pct_low",
                "target_pct_high",
                "require_structure_event",
                "require_momentum_align",
            )
            if k in best["params"]
        },
    }
    path = OUT / "calibration_report.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    s = best["summary"]
    print("\n=== BEST ===")
    print(
        f"PnL ₹{s['pnl_total']:,.0f} | green-day {s['green_day_pct']}% "
        f"| trade-win {s['trade_win_pct']}% | red_days {s['red_days']} "
        f"| traded {s['traded_days']} | DD ₹{s['max_dd']:,.0f} | setups {s['setups']}"
    )
    print("params:", json.dumps(payload["best_params"], indent=2))
    print(f"Wrote {path}")

    if args.apply:
        _apply_best(best["params"])


if __name__ == "__main__":
    main()

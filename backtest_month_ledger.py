"""
Causal 1-month ledger backtest (no lookahead).

Rules (this run):
  - Start capital ₹1,00,000
  - Size from 90% of current balance
  - Up to 15 entries/day
  - Zerodha-style NFO charges on every round trip
  - Halt if equity ≤ 30% of starting capital
  - Minute-by-minute ORB directional sim (same as live gates)

Usage:
  .venv\\Scripts\\python.exe backtest_month_ledger.py
  .venv\\Scripts\\python.exe backtest_month_ledger.py --days 22
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.charges import estimate_option_charges
from foxtrade.config_store import load_config
from foxtrade.expert_filters import (
    daily_loss_halted,
    manage_percent_exit,
    monthly_target_reached,
    period_loss_halted,
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
class OpenTrade:
    side: str
    entry_i: int
    entry_px: float
    entry_spot: float
    lots: int
    qty: int
    peak: float
    stop_px: float
    target_px: float
    note: str = ""
    deployable_at_entry: float = 0.0
    balance_at_entry: float = 0.0
    invested: float = 0.0


@dataclass
class LedgerState:
    initial: float
    balance: float
    deploy_pct: float = 0.50  # income profile: at most 50% of free cash per trade
    deploy_cap: float = 0.0  # 0 = no absolute cap beyond deploy_pct
    halt_pct: float = 0.30
    halted: bool = False
    halt_reason: str = ""
    ledger: list[dict[str, Any]] = field(default_factory=list)
    week_key: str = ""
    week_pnl: float = 0.0
    month_key: str = ""
    month_pnl: float = 0.0

    def roll_period(self, day: str) -> None:
        """Reset week/month buckets when calendar period changes (day = YYYY-MM-DD)."""
        from datetime import date as _date

        d = _date.fromisoformat(day)
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        mk = f"{d.year:04d}-{d.month:02d}"
        if wk != self.week_key:
            self.week_key = wk
            self.week_pnl = 0.0
        if mk != self.month_key:
            self.month_key = mk
            self.month_pnl = 0.0

    @property
    def halt_floor(self) -> float:
        return self.initial * self.halt_pct

    @property
    def deployable(self) -> float:
        """Budget = deploy_pct × free cash (default 60%), optional absolute cap."""
        bud = max(0.0, self.balance * self.deploy_pct)
        if self.deploy_cap and self.deploy_cap > 0:
            bud = min(bud, self.deploy_cap)
        return bud

    def check_halt(self) -> bool:
        if self.balance <= self.halt_floor:
            self.halted = True
            self.halt_reason = (
                f"equity ₹{self.balance:,.0f} ≤ 30% floor ₹{self.halt_floor:,.0f}"
            )
            return True
        return False


def _load_month_days(
    as_of: date | None = None,
    calendar_days: int = 31,
    *,
    all_available: bool = False,
) -> list[dict[str, Any]]:
    meta_path = DATA / "all_expiries_every_day.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    as_of = as_of or date.fromisoformat(str(meta.get("as_of") or date.today().isoformat()))
    if all_available and meta.get("range_start"):
        start = date.fromisoformat(str(meta["range_start"]))
    else:
        start = as_of - timedelta(days=calendar_days - 1)

    by_date: dict[str, dict[str, Any]] = {}
    for exp in meta.get("expiries") or []:
        for d in exp.get("days") or []:
            if not d.get("session"):
                continue
            dd = date.fromisoformat(d["date"])
            if dd < start or dd > as_of:
                continue
            path = DATA / d["minutes_file"]
            if not path.exists():
                continue
            # Nifty weekly options expire Tuesday — trust index flag OR weekday.
            exp_flag = bool(d.get("is_expiry_day")) or dd.weekday() == 1
            by_date[d["date"]] = {
                "date": d["date"],
                "is_expiry_day": exp_flag,
                "minutes_path": path,
                "spot": d.get("spot") or {},
            }

    # Also pick up minute files in range (e.g. post-expiry sessions not in index)
    minutes_dir = DATA / "minutes"
    if minutes_dir.exists():
        for path in sorted(minutes_dir.glob("nifty_*.json")):
            try:
                dd = date.fromisoformat(path.stem.replace("nifty_", ""))
            except ValueError:
                continue
            if dd < start or dd > as_of:
                continue
            key = dd.isoformat()
            if key in by_date:
                # Upgrade expiry flag if index missed Tuesday
                if dd.weekday() == 1:
                    by_date[key]["is_expiry_day"] = True
                continue
            by_date[key] = {
                "date": key,
                "is_expiry_day": dd.weekday() == 1,
                "minutes_path": path,
                "spot": {},
            }

    return [by_date[k] for k in sorted(by_date)]


def _load_closes(path: Path) -> list[float]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [float(r["close"]) for r in rows]


def _session_end_idx(cfg: dict[str, Any], n: int) -> int:
    start = str(cfg.get("session_start", "09:15"))
    end = str(cfg.get("session_end", "13:30"))
    sh, sm = map(int, start.split(":")[:2])
    eh, em = map(int, end.split(":")[:2])
    mins = (eh * 60 + em) - (sh * 60 + sm)
    return min(n - 1, max(30, mins))


def _synth_premium(
    spot_path: list[float],
    side: str,
    entry_i: int,
    entry_prem: float,
    *,
    delta: float = 0.50,
    theta_per_min: float = 0.015,
) -> list[float]:
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


def _hhmm_from_i(i: int, cfg: dict[str, Any]) -> str:
    sh, sm = map(int, str(cfg.get("session_start", "09:15")).split(":")[:2])
    total = sh * 60 + sm + i
    return f"{total // 60:02d}:{total % 60:02d}"


def _lots_for_balance(
    cfg: dict[str, Any],
    *,
    entry: float,
    available: float,
    deploy_pct: float = 0.90,
    deploy_cap: float = 90_000.0,
) -> int:
    """
    Lots such that invested = entry×lot_size×lots never exceeds free cash.

    Budget = min(available, deploy_cap, available × deploy_pct).
    Skip if that cannot fund min_lots_per_trade.
    """
    lot_size = int(cfg.get("lot_size", 75))
    min_lots = int(cfg.get("min_lots_per_trade", 4))
    max_lots = int(cfg.get("max_lots_per_trade", 10))
    prem_cost = max(float(entry) * lot_size, 1.0)
    cash = max(0.0, float(available))
    # Hard rule: cannot spend more than free balance; budget ≤ deploy_pct of cash
    pct_bud = cash * float(deploy_pct)
    budget = min(cash, pct_bud)
    if deploy_cap and float(deploy_cap) > 0:
        budget = min(budget, float(deploy_cap))
    max_by_budget = int(budget // prem_cost)
    max_by_cash = int(cash // prem_cost)  # absolute ceiling
    max_affordable = min(max_by_budget, max_by_cash, max_lots)
    if max_affordable < min_lots:
        return 0
    lots = max_affordable  # fill budget, already ≥ min_lots
    # Final safety: invested must be ≤ cash
    while lots >= min_lots and lots * prem_cost > cash + 1e-6:
        lots -= 1
    return lots if lots >= min_lots else 0


def _clamp_lots_to_cash(
    lots: int,
    *,
    entry: float,
    available: float,
    cfg: dict[str, Any],
) -> int:
    """After any lot tweak, never allow invested > available cash."""
    lot_size = int(cfg.get("lot_size", 75))
    min_lots = int(cfg.get("min_lots_per_trade", 4))
    prem_cost = max(float(entry) * lot_size, 1.0)
    cash = max(0.0, float(available))
    lots = min(int(lots), int(cash // prem_cost))
    return lots if lots >= min_lots else 0


def simulate_day_ledger(
    closes: list[float],
    cfg: dict[str, Any],
    state: LedgerState,
    *,
    is_expiry_day: bool = False,
    day: str = "",
) -> dict[str, Any]:
    """Causal minute walk for one session; mutates ledger state."""
    spot_chg = (closes[-1] - closes[0]) if closes else 0.0
    day_row: dict[str, Any] = {
        "date": day,
        "balance_open": round(state.balance, 2),
        "spot_chg": round(spot_chg, 1),
        "is_expiry_day": is_expiry_day,
        "trades": [],
        "setups": 0,
        "lots_total": 0,
        "invested_total": 0.0,
        "gross": 0.0,
        "charges": 0.0,
        "net": 0.0,
        "wins": 0,
        "losses": 0,
        "halted": False,
        "skip": "",
    }
    if day:
        state.roll_period(day)
    if state.halted or state.check_halt():
        day_row["skip"] = state.halt_reason or "capital halt"
        day_row["halted"] = True
        day_row["balance_close"] = round(state.balance, 2)
        return day_row
    if period_loss_halted(state.week_pnl, cfg, pct_key="max_weekly_loss_pct"):
        day_row["skip"] = f"weekly loss circuit (₹{state.week_pnl:,.0f})"
        day_row["balance_close"] = round(state.balance, 2)
        return day_row
    if period_loss_halted(state.month_pnl, cfg, pct_key="max_monthly_loss_pct"):
        day_row["skip"] = f"monthly loss circuit (₹{state.month_pnl:,.0f})"
        day_row["balance_close"] = round(state.balance, 2)
        return day_row
    if monthly_target_reached(state.month_pnl, cfg):
        day_row["skip"] = f"month target banked (₹{state.month_pnl:,.0f})"
        day_row["balance_close"] = round(state.balance, 2)
        return day_row
    if len(closes) < 40:
        day_row["skip"] = "too few bars"
        day_row["balance_close"] = round(state.balance, 2)
        return day_row

    end_i = _session_end_idx(cfg, len(closes))
    closes = closes[: end_i + 1]
    entry_prem = (
        float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))
    ) / 2.0
    lot_size = int(cfg.get("lot_size", 75))
    max_setups = int(cfg.get("max_trades_per_day", 15))
    cooldown = int(cfg.get("entry_cooldown_minutes", 12))
    delta = float(cfg.get("synth_delta", 0.50))
    theta = float(cfg.get("synth_theta_per_min", 0.015))

    open_tr: OpenTrade | None = None
    last_exit_i = -10_000
    losses = 0
    wins = 0
    day_net = 0.0
    persist_need = int(cfg.get("signal_persist_minutes", 0))
    persist_side: str | None = None
    persist_n = 0

    for i in range(1, len(closes)):
        if state.halted:
            break
        path = closes[: i + 1]

        if open_tr is not None:
            prem_path = _synth_premium(
                closes,
                open_tr.side,
                open_tr.entry_i,
                open_tr.entry_px,
                delta=delta,
                theta_per_min=theta,
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
                reason = (
                    "session end"
                    if force_eod and dec.get("action") != "exit"
                    else str(dec.get("reason"))
                )
                gross = (cur - open_tr.entry_px) * open_tr.qty
                chg = estimate_option_charges(open_tr.entry_px, cur, open_tr.qty, cfg)
                charges = float(chg["charges"])
                net = gross - charges
                day_net += net
                state.week_pnl = round(state.week_pnl + net, 2)
                state.month_pnl = round(state.month_pnl + net, 2)
                if net > 0:
                    wins += 1
                elif net < 0:
                    losses += 1
                invested = float(open_tr.invested or round(open_tr.entry_px * open_tr.qty, 2))
                deployable = float(open_tr.deployable_at_entry or 0)
                bal_entry = float(open_tr.balance_at_entry or 0)
                # Premium was deducted at entry; credit sale proceeds, debit charges
                proceeds = float(cur) * open_tr.qty
                state.balance = round(state.balance + proceeds - charges, 2)
                trade = {
                    "day": day,
                    "side": open_tr.side,
                    "entry_time": _hhmm_from_i(open_tr.entry_i, cfg),
                    "exit_time": _hhmm_from_i(i, cfg),
                    "entry_px": round(open_tr.entry_px, 2),
                    "exit_px": round(cur, 2),
                    "lots": open_tr.lots,
                    "qty": open_tr.qty,
                    "invested": invested,
                    "balance_at_entry": round(bal_entry, 2),
                    "invested_pct_of_balance": round(
                        100.0 * invested / max(bal_entry, 1.0), 1
                    ),
                    "invested_pct_of_deployable": round(
                        100.0 * invested / max(deployable, 1.0), 1
                    ),
                    "gross": round(gross, 2),
                    "charges": charges,
                    "charges_detail": chg,
                    "net": round(net, 2),
                    "mult": round(cur / open_tr.entry_px, 3),
                    "reason": reason,
                    "note": open_tr.note,
                    "balance_after": round(state.balance, 2),
                    "deployable_at_entry": round(deployable, 2),
                }
                day_row["trades"].append(trade)
                state.ledger.append(trade)
                day_row["gross"] += gross
                day_row["charges"] += charges
                day_row["net"] += net
                day_row["lots_total"] += open_tr.lots
                last_exit_i = i
                open_tr = None
                if state.check_halt():
                    day_row["halted"] = True
                    break
                if force_eod:
                    break
            continue

        if i > end_i:
            break
        if day_row["setups"] >= max_setups:
            continue
        if i - last_exit_i < cooldown:
            continue
        if daily_loss_halted(day_net, cfg):
            continue
        if losses >= int(cfg.get("max_losses_before_halt", 8)):
            continue
        if cfg.get("bank_after_win") and day_net > 0 and wins >= 1:
            continue

        analysis = analyze_market(path, cfg=cfg, is_expiry_day=is_expiry_day)
        if not analysis.get("allow_entry"):
            persist_side, persist_n = None, 0
            continue
        side = analysis.get("side")
        event = str(analysis.get("event") or "")
        if side not in ("CE", "PE"):
            persist_side, persist_n = None, 0
            continue
        # Require signal to persist N minutes (filters 1–3m fake breaks)
        # SR reclaim fast/slow persist (see backtest_detailed profit-learn)
        bar_persist = persist_need
        if event in ("SR_RECLAIM", "SR_REJECT") and bar_persist > 0:
            sr_vs = abs(float(analysis.get("vs_open") or 0))
            sr_mom = abs(float(analysis.get("momentum") or 0))
            hard_pts = float(cfg.get("trend_hard_pts", 70))
            mid_vs = float(cfg.get("sr_fast_persist_min_vs", 50))
            fast_mom = float(cfg.get("sr_fast_persist_min_mom", 18))
            fast = (sr_vs >= hard_pts and sr_mom >= fast_mom) or (
                mid_vs <= sr_vs < hard_pts
            )
            if fast:
                bar_persist = int(cfg.get("sr_signal_persist_minutes", 1))
            else:
                bar_persist = max(bar_persist, 2)
        if bar_persist > 0:
            if side == persist_side:
                persist_n += 1
            else:
                persist_side, persist_n = side, 1
            if persist_n < bar_persist:
                continue
        else:
            persist_side, persist_n = side, 1

        lots = _lots_for_balance(
            cfg,
            entry=entry_prem,
            available=state.balance,
            deploy_pct=state.deploy_pct,
            deploy_cap=state.deploy_cap,
        )
        if losses >= 2 and lots > 0:
            lots = _clamp_lots_to_cash(
                max(int(cfg.get("min_lots_per_trade", 4)), lots // 2),
                entry=entry_prem,
                available=state.balance,
                cfg=cfg,
            )
        if lots < int(cfg.get("min_lots_per_trade", 4)):
            continue

        invested = round(entry_prem * lots * lot_size, 2)
        if invested > state.balance + 1e-6:
            continue  # hard stop — never overspend free cash

        bal_before = float(state.balance)
        deployable = float(state.deployable)
        state.balance = round(state.balance - invested, 2)  # lock premium

        open_tr = OpenTrade(
            side=side,
            entry_i=i,
            entry_px=entry_prem,
            entry_spot=closes[i],
            lots=lots,
            qty=lots * lot_size,
            peak=entry_prem,
            stop_px=stop_price(entry_prem, cfg),
            target_px=target_price(entry_prem, cfg, trail=True),
            note=(
                f"{analysis.get('event')} conf={float(analysis.get('confidence') or 0):.0%} "
                f"| {analysis.get('reason')}"
            ),
            deployable_at_entry=deployable,
            balance_at_entry=bal_before,
            invested=invested,
        )
        day_row["setups"] += 1
        day_row["invested_total"] = round(
            float(day_row.get("invested_total") or 0) + invested, 2
        )

    day_row["gross"] = round(day_row["gross"], 2)
    day_row["charges"] = round(day_row["charges"], 2)
    day_row["net"] = round(day_row["net"], 2)
    day_row["wins"] = wins
    day_row["losses"] = losses
    day_row["balance_close"] = round(state.balance, 2)
    return day_row


def month_cfg(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Paper / research profile for ₹2L — NOT a salary machine.
      - Trail runners + ≤2 trades/day; halt after 1 loss
      - Day / week / month loss circuits (capital protection)
      - live_trading forced False until real fills match sim
      - ₹50–100k/mo on ₹2L is NOT a reliable target
    """
    cfg = deepcopy(base or load_config())
    cfg.update(
        {
            "capital_inr": 200_000,
            "risk_per_trade_pct": 0.75,
            "max_daily_loss_pct": 2.0,  # ₹4,000
            "max_weekly_loss_pct": 5.0,  # ₹10,000
            "max_monthly_loss_pct": 10.0,  # ₹20,000 — stop month
            "bank_month_at_target": False,
            "monthly_profit_target_inr": 50_000,
            "max_lots_per_trade": 6,
            "min_lots_per_trade": 2,
            "lot_size": 75,
            "max_trades_per_day": 2,
            "max_losses_before_halt": 1,
            "bank_after_win": False,
            "entry_cooldown_minutes": 25,
            "signal_persist_minutes": 2,
            "use_percent_exits": True,
            "stop_loss_pct": 5.0,
            "stop_grace_minutes": 0,
            "stop_grace_pct": 8.0,
            "target_pct_low": 5.0,
            "target_pct_high": 50.0,
            "lock_profit_pct": 1.5,
            "trail_giveback_pct": 5.0,
            "exit_at_first_target": False,
            "time_stop_minutes": 20,
            "post_decay_confirm_minutes": 0,
            "post_decay_min_bar": 0,
            "post_decay_respect_session_delay": False,
            "entry_style": "orb",
            "orb_minutes": 30,
            "orb_min_range_pts": 42,
            "orb_max_range_pts": 200,
            "allow_shakeout_entry": False,
            "min_break_strength": 0.35,
            "min_vs_open_entry": 50,
            "ce_min_vs_open": 65,
            "pe_min_vs_open": 50,
            "block_ce_mid_chase": True,
            "ce_mid_chase_low": 12,
            "ce_mid_chase_high": 40,
            "require_hard_trend": False,
            "require_structure_event": True,
            "require_momentum_align": True,
            "require_3bar_continue": True,
            "continue_bars": 3,
            "post_break_confirm_minutes": 1,
            "stability_mode": True,
            "min_path_efficiency": 0.22,
            "directional_min_confidence": 0.70,
            "directional_min_margin": 0.20,
            "entry_delay_minutes": 30,
            "prefer_entry_after_minutes": 32,
            "no_entry_after_minutes": 195,
            "break_buffer_pts": 8,
            "break_hold_minutes": 3,
            "extreme_proximity_pts": 18,
            "chop_score_threshold": 0.58,
            "trend_soft_pts": 35,
            "trend_hard_pts": 70,
            "require_session_extreme": True,
            "synth_delta": 0.50,
            "synth_theta_per_min": 0.015,
            "session_start": "09:15",
            "session_end": "13:30",
            "live_trading": False,
            "profile_name": "paper_2l_protected",
            "deploy_pct_of_balance": 0.50,
        }
    )
    return cfg


def run_month(
    *,
    initial: float = 100_000.0,
    deploy_pct: float = 0.90,
    deploy_cap: float = 90_000.0,
    halt_pct: float = 0.30,
    calendar_days: int = 31,
    all_available: bool = False,
) -> dict[str, Any]:
    cfg = month_cfg()
    days = _load_month_days(calendar_days=calendar_days, all_available=all_available)
    # Default income: 50% of free cash, no hard ₹ cap
    if deploy_pct == 0.90:
        deploy_pct = 0.50
    if deploy_cap == 90_000.0:
        deploy_cap = 0.0
    state = LedgerState(
        initial=initial,
        balance=initial,
        deploy_pct=deploy_pct,
        deploy_cap=deploy_cap,
        halt_pct=halt_pct,
    )
    daily: list[dict[str, Any]] = []
    for d in days:
        closes = _load_closes(d["minutes_path"])
        row = simulate_day_ledger(
            closes,
            cfg,
            state,
            is_expiry_day=bool(d.get("is_expiry_day")),
            day=d["date"],
        )
        daily.append(row)
        if state.halted:
            # still record remaining days as skipped
            continue

    traded_days = [r for r in daily if r["setups"] > 0]
    total_gross = sum(r["gross"] for r in daily)
    total_charges = sum(r["charges"] for r in daily)
    total_net = sum(r["net"] for r in daily)
    return {
        "title": "1-Month Causal Ledger Backtest",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_minute_no_lookahead",
        "premium_model": "synthetic ATM (delta≈0.5 + theta) — expired option chains unavailable",
        "capital": {
            "initial": initial,
            "deploy_pct": deploy_pct,
            "deploy_cap": deploy_cap,
            "halt_pct": halt_pct,
            "halt_floor": initial * halt_pct,
            "final_balance": round(state.balance, 2),
            "net_pnl": round(state.balance - initial, 2),
            "return_pct": round(100.0 * (state.balance / initial - 1.0), 2),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "totals": {
            "session_days": len(daily),
            "traded_days": len(traded_days),
            "setups": sum(r["setups"] for r in daily),
            "lots": sum(r["lots_total"] for r in daily),
            "wins": sum(r["wins"] for r in daily),
            "losses": sum(r["losses"] for r in daily),
            "gross": round(total_gross, 2),
            "charges": round(total_charges, 2),
            "net": round(total_net, 2),
            "green_days": sum(1 for r in daily if r["net"] > 0),
            "red_days": sum(1 for r in daily if r["net"] < 0),
        },
        "config_snapshot": {
            "max_trades_per_day": cfg["max_trades_per_day"],
            "entry_cooldown_minutes": cfg["entry_cooldown_minutes"],
            "stop_loss_pct": cfg["stop_loss_pct"],
            "target_pct_low": cfg["target_pct_low"],
            "target_pct_high": cfg["target_pct_high"],
            "entry_style": cfg["entry_style"],
            "orb_minutes": cfg["orb_minutes"],
            "risk_per_trade_pct": cfg["risk_per_trade_pct"],
            "max_lots_per_trade": cfg["max_lots_per_trade"],
            "min_lots_per_trade": cfg["min_lots_per_trade"],
            "lot_size": cfg["lot_size"],
            "session": f"{cfg['session_start']}–{cfg['session_end']}",
        },
        "daily": daily,
        "ledger": state.ledger,
        "range": {
            "start": days[0]["date"] if days else None,
            "end": days[-1]["date"] if days else None,
        },
    }


def _inr(n: float | None, signed: bool = True) -> str:
    if n is None:
        return "—"
    if not signed:
        return f"₹{abs(n):,.0f}"
    if n > 0:
        return f"+₹{n:,.0f}"
    if n < 0:
        return f"−₹{abs(n):,.0f}"
    return "₹0"


def write_html(report: dict[str, Any], path: Path) -> None:
    cap = report["capital"]
    tot = report["totals"]
    daily = report["daily"]
    ledger = report["ledger"]
    cfg = report.get("config_snapshot") or {}
    rng = report.get("range") or {}

    # Equity curve from day closes
    eq = [(r["date"][5:], float(r["balance_close"])) for r in daily]
    if not eq:
        eq = [("start", float(cap["initial"]))]
    ys = [v for _, v in eq]
    y_min, y_max = min(ys + [cap["halt_floor"], cap["initial"]]), max(ys + [cap["initial"]])
    pad = max((y_max - y_min) * 0.08, 1000)
    y0, y1 = y_min - pad, y_max + pad
    W, H, L, R, T, B = 960, 280, 70, 30, 24, 40
    pw, ph = W - L - R, H - T - B

    def x_at(i: int) -> float:
        n = max(len(eq) - 1, 1)
        return L + i * pw / n

    def y_at(v: float) -> float:
        return T + (1 - (v - y0) / max(y1 - y0, 1)) * ph

    poly = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, (_, v) in enumerate(eq))
    halt_y = y_at(float(cap["halt_floor"]))
    init_y = y_at(float(cap["initial"]))
    area = f"{L:.1f},{y_at(eq[0][1]):.1f} " + poly + f" {x_at(len(eq)-1):.1f},{T+ph:.1f} {L:.1f},{T+ph:.1f}"

    day_rows = []
    for r in daily:
        net = float(r.get("net") or 0)
        cls = "g" if net > 0 else ("r" if net < 0 else "f")
        pnl_cls = "pnl-pos" if net > 0 else ("pnl-neg" if net < 0 else "pnl-flat")
        skip = r.get("skip") or ""
        trades = r.get("trades") or []
        invs = [float(t.get("invested") or 0) for t in trades]
        # One position at a time: max locked in a single trade (never > bal @ entry)
        invested_max = max(invs) if invs else 0.0
        # Sum of sequential deployments (cash recycled) — can exceed open bal
        invested_turnover = float(r.get("invested_total") or 0) or sum(invs)
        day_rows.append(
            f"""<tr class="{cls}">
            <td>{r['date']}</td>
            <td>{'EXP' if r.get('is_expiry_day') else ''}</td>
            <td>{r['spot_chg']:+.0f}</td>
            <td>{r['setups']}</td>
            <td>{r['lots_total']}</td>
            <td class="num"><b>₹{invested_max:,.0f}</b></td>
            <td class="num muted">₹{invested_turnover:,.0f}</td>
            <td>{r['wins']}/{r['losses']}</td>
            <td class="num {pnl_cls}">{_inr(r['gross'])}</td>
            <td class="num">₹{r['charges']:,.0f}</td>
            <td class="num {pnl_cls}"><b>{_inr(r['net'])}</b></td>
            <td class="num">₹{r['balance_open']:,.0f}</td>
            <td class="num">₹{r['balance_close']:,.0f}</td>
            <td class="muted">{skip}</td>
            </tr>"""
        )

    trade_rows = []
    for i, t in enumerate(ledger, 1):
        net = float(t.get("net") or 0)
        cls = "g" if net > 0 else ("r" if net < 0 else "f")
        pnl_cls = "pnl-pos" if net > 0 else ("pnl-neg" if net < 0 else "pnl-flat")
        invested = float(t.get("invested") or (t.get("entry_px", 0) * t.get("qty", 0)))
        bal_entry = float(t.get("balance_at_entry") or 0)
        deployable = float(t.get("deployable_at_entry") or 0)
        inv_pct_bal = t.get("invested_pct_of_balance")
        if inv_pct_bal is None and bal_entry > 0:
            inv_pct_bal = round(100.0 * invested / bal_entry, 1)
        over = invested > bal_entry + 0.01 if bal_entry else False
        trade_rows.append(
            f"""<tr class="{cls}{' overspend' if over else ''}">
            <td>{i}</td>
            <td>{t['day']}</td>
            <td>{t['side']}</td>
            <td>{t['entry_time']}→{t['exit_time']}</td>
            <td>{t['lots']}</td>
            <td class="num"><b>₹{invested:,.0f}</b></td>
            <td class="num">₹{bal_entry:,.0f}</td>
            <td class="num muted">₹{deployable:,.0f}{f' ({inv_pct_bal}% bal)' if inv_pct_bal is not None else ''}</td>
            <td>{t['entry_px']:.1f}→{t['exit_px']:.1f}</td>
            <td>{t['mult']:.2f}×</td>
            <td class="num {pnl_cls}">{_inr(t['gross'])}</td>
            <td class="num">₹{t['charges']:,.0f}</td>
            <td class="num {pnl_cls}"><b>{_inr(t['net'])}</b></td>
            <td class="num">₹{t['balance_after']:,.0f}</td>
            <td class="muted">{'⚠ overspend · ' if over else ''}{t.get('reason','')}</td>
            </tr>"""
        )

    final_cls = "good" if cap["net_pnl"] >= 0 else "bad"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FoxtronTrade — 1M Ledger Backtest</title>
<style>
:root {{
  --bg:#0f1419; --card:#171e27; --line:#243041; --text:#e7eef7; --muted:#8fa0b8;
  --good:#3dbea0; --bad:#e57373; --accent:#5b9fd4; --warn:#e0b35a;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:"Segoe UI", system-ui, sans-serif;
  background:radial-gradient(1200px 600px at 10% -10%, #1a2a3a 0%, var(--bg) 55%);
  color:var(--text); line-height:1.45;
}}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 18px 60px; }}
h1 {{ font-size:1.55rem; margin:0 0 6px; letter-spacing:-0.02em; }}
.sub {{ color:var(--muted); font-size:0.92rem; margin-bottom:22px; }}
.grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px; }}
@media (max-width:800px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} }}
.card {{
  background:linear-gradient(180deg,#1b2430,#151c25); border:1px solid var(--line);
  border-radius:14px; padding:14px 16px;
}}
.card .lbl {{ color:var(--muted); font-size:0.75rem; text-transform:uppercase; letter-spacing:0.06em; }}
.card .val {{ font-size:1.35rem; font-weight:700; margin-top:4px; }}
.card .val.good {{ color:var(--good); }}
.card .val.bad {{ color:var(--bad); }}
.panel {{
  background:var(--card); border:1px solid var(--line); border-radius:16px;
  padding:16px 18px; margin-top:16px;
}}
.panel h2 {{ margin:0 0 12px; font-size:1.05rem; }}
table {{ width:100%; border-collapse:collapse; font-size:0.86rem; }}
th, td {{ padding:8px 8px; border-bottom:1px solid #223044; text-align:left; }}
th {{ color:var(--muted); font-weight:600; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; }}
td.num {{ font-variant-numeric:tabular-nums; text-align:right; }}
.pnl-pos {{ color:var(--good) !important; font-weight:700; }}
.pnl-neg {{ color:var(--bad) !important; font-weight:700; }}
.pnl-flat {{ color:var(--muted); }}
tr.g td {{ background:rgba(61,190,160,0.14); }}
tr.r td {{ background:rgba(229,115,115,0.14); }}
tr.f td {{ opacity:0.85; }}
tr.g td:first-child {{ box-shadow: inset 4px 0 0 var(--good); }}
tr.r td:first-child {{ box-shadow: inset 4px 0 0 var(--bad); }}
tr.overspend td {{ outline: 1px solid var(--warn); }}
.muted {{ color:var(--muted); font-size:0.78rem; }}
.note {{
  margin-top:14px; padding:12px 14px; border-radius:12px;
  background:#1a2230; border:1px solid var(--line); color:var(--muted); font-size:0.85rem;
}}
.badge {{
  display:inline-block; padding:3px 8px; border-radius:999px; font-size:0.72rem;
  background:#243246; color:var(--accent); margin-right:6px;
}}
.badge.warn {{ color:var(--warn); background:#2a2618; }}
.badge.bad {{ color:var(--bad); background:#2a1a1a; }}
svg {{ width:100%; height:auto; display:block; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>FoxtronTrade — 1-Month Ledger</h1>
  <div class="sub">
    <span class="badge">Causal · no lookahead</span>
    <span class="badge">₹1,00,000 start · min 4 lots · ≤₹90k / 90% bal</span>
    <span class="badge">Max 15 entries/day</span>
    <span class="badge {'warn' if not cap['halted'] else 'bad'}">Halt @ 30% (₹{cap['halt_floor']:,.0f})</span>
    <br/>Range {rng.get('start')} → {rng.get('end')} · Generated {report.get('generated_at','')[:19]}
  </div>

  <div class="grid">
    <div class="card"><div class="lbl">Final balance</div><div class="val">{_inr(cap['final_balance'], False)}</div></div>
    <div class="card"><div class="lbl">Net P&amp;L</div><div class="val {final_cls}">{_inr(cap['net_pnl'])} ({cap['return_pct']:+.1f}%)</div></div>
    <div class="card"><div class="lbl">Charges paid</div><div class="val">₹{tot['charges']:,.0f}</div></div>
    <div class="card"><div class="lbl">Trades / Lots</div><div class="val">{tot['setups']} / {tot['lots']}</div></div>
    <div class="card"><div class="lbl">Total invested (sum)</div><div class="val">₹{float(tot.get('invested_total') or sum(float(t.get('invested') or 0) for t in ledger)):,.0f}</div></div>
    <div class="card"><div class="lbl">Gross P&amp;L</div><div class="val">{_inr(tot['gross'])}</div></div>
    <div class="card"><div class="lbl">Win / Loss</div><div class="val">{tot['wins']} / {tot['losses']}</div></div>
    <div class="card"><div class="lbl">Green / Red days</div><div class="val">{tot['green_days']} / {tot['red_days']}</div></div>
    <div class="card"><div class="lbl">Sessions traded</div><div class="val">{tot['traded_days']} / {tot['session_days']}</div></div>
  </div>

  <div class="panel">
    <h2>Equity ledger curve</h2>
    <svg viewBox="0 0 {W} {H}" role="img" aria-label="Equity curve">
      <line x1="{L}" y1="{init_y:.1f}" x2="{W-R}" y2="{init_y:.1f}" stroke="#3a4a5e" stroke-dasharray="4 4"/>
      <line x1="{L}" y1="{halt_y:.1f}" x2="{W-R}" y2="{halt_y:.1f}" stroke="#e57373" stroke-dasharray="5 4" opacity="0.7"/>
      <text x="{L+4}" y="{halt_y-6:.1f}" fill="#e57373" font-size="11">30% halt</text>
      <text x="{L+4}" y="{init_y-6:.1f}" fill="#8fa0b8" font-size="11">start</text>
      <polygon points="{area}" fill="rgba(91,159,212,0.12)"/>
      <polyline points="{poly}" fill="none" stroke="#5b9fd4" stroke-width="2.5"/>
      {''.join(f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3" fill="{"#3dbea0" if v>=cap["initial"] else "#e57373"}"/>' for i,(_,v) in enumerate(eq))}
      <text x="{L}" y="{H-12}" fill="#8fa0b8" font-size="11">{eq[0][0]}</text>
      <text x="{W-R}" y="{H-12}" fill="#8fa0b8" font-size="11" text-anchor="end">{eq[-1][0]}</text>
    </svg>
  </div>

  <div class="panel">
    <h2>Daily P&amp;L · invested · lots · balance</h2>
    <p class="muted" style="margin:0 0 10px">
      <b>Max invested</b> = largest single trade that day (≤ cash at entry).
      <b>Turnover</b> = sum of all trades’ premiums that day (cash recycled after each exit — can exceed open bal).
    </p>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Date</th><th></th><th>Spot Δ</th><th>Setups</th><th>Lots</th>
          <th>Max invested</th><th>Turnover</th><th>W/L</th>
          <th>Gross</th><th>Charges</th><th>Net</th><th>Open bal</th><th>Close bal</th><th>Note</th>
        </tr>
      </thead>
      <tbody>
        {''.join(day_rows)}
      </tbody>
    </table>
    </div>
  </div>

  <div class="panel">
    <h2>Trade ledger (every fill) — invested = premium × qty</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Day</th><th>Side</th><th>Time</th><th>Lots</th>
          <th>Invested</th><th>Bal @ entry</th><th>Budget</th><th>Entry→Exit</th><th>Mult</th>
          <th>Gross</th><th>Charges</th><th>Net</th><th>Bal after</th><th>Exit</th>
        </tr>
      </thead>
      <tbody>
        {''.join(trade_rows) if trade_rows else '<tr><td colspan="15" class="muted">No trades</td></tr>'}
      </tbody>
    </table>
    </div>
  </div>

  <div class="note">
    <b>Rules:</b> start ₹{cap['initial']:,.0f}; budget min(₹90k, {int(cap['deploy_pct']*100)}% equity); fill max affordable lots (≥4);
    max {cfg.get('max_trades_per_day')} entries/day; cooldown {cfg.get('entry_cooldown_minutes')}m;
    SL {cfg.get('stop_loss_pct')}% / trail target {cfg.get('target_pct_low')}–{cfg.get('target_pct_high')}%;
    ORB {cfg.get('orb_minutes')}m; session {cfg.get('session')}.
    Charges = Zerodha-style NFO round-trip (brokerage, exchange, STT on sell, stamp on buy, GST, SEBI).
    {'<br/><b>HALTED:</b> '+cap['halt_reason'] if cap.get('halted') else ''}
    <br/><b>Model note:</b> {report.get('premium_model')}
  </div>
</div>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=31, help="Calendar-day lookback (~1 month)")
    ap.add_argument("--months", type=int, default=0, help="If set, lookback ≈ months*31 days")
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--tag", type=str, default="", help="Output filename tag (auto if empty)")
    args = ap.parse_args()

    calendar_days = args.months * 31 if args.months > 0 else args.days
    use_all = args.months >= 3
    tag = args.tag.strip() or ("quarter" if use_all or args.months >= 3 else ("month" if calendar_days <= 35 else f"d{calendar_days}"))
    label = "3-month" if use_all or args.months >= 3 or calendar_days >= 90 else ("1-month" if calendar_days <= 35 else f"{calendar_days}d")

    print(f"Running causal {label} ledger backtest (capital ₹{args.capital:,.0f}, ≥4 lots, ≤₹90k/90%)…")
    report = run_month(
        initial=args.capital,
        calendar_days=calendar_days,
        all_available=use_all,
    )
    report["title"] = f"{label.title()} Causal Ledger Backtest"
    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / f"{tag}_ledger_report.json"
    html_path = OUT / f"{tag}_ledger_report.html"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_html(report, html_path)

    cap = report["capital"]
    tot = report["totals"]
    print(
        f"Range {report['range']['start']} → {report['range']['end']}\n"
        f"Final ₹{cap['final_balance']:,.0f}  net {_inr(cap['net_pnl'])} ({cap['return_pct']:+.1f}%)\n"
        f"Gross {_inr(tot['gross'])}  charges ₹{tot['charges']:,.0f}  "
        f"trades {tot['setups']} lots {tot['lots']}  "
        f"green/red days {tot['green_days']}/{tot['red_days']}\n"
        f"Halted: {cap['halted']} {cap['halt_reason']}\n"
        f"HTML  {html_path}\n"
        f"JSON  {json_path}"
    )


if __name__ == "__main__":
    main()

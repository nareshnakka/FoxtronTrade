"""
Detailed minute-by-minute causal backtest of the live directional algorithm.

Each minute (no lookahead):
  1. Append only that bar's close to the spot path
  2. If in a trade → reprice premium from spot Δ, run manage_percent_exit
  3. If flat → analyze_market() with the same gates as live algo1
  4. Size ≥4 lots from min(₹90k, 90% equity); charges on exit; halt @ 30%

Usage:
  .venv\\Scripts\\python.exe backtest_detailed.py
  .venv\\Scripts\\python.exe backtest_detailed.py --months 1
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

from foxtrade.charges import estimate_option_charges
from foxtrade.expert_filters import (
    daily_loss_halted,
    manage_percent_exit,
    stop_price,
    target_price,
)
from foxtrade.daily_soft import (
    merge_soft_cfg,
    soft_enabled,
    soft_exit_overlay,
    soft_lots,
    tag_soft_event,
)
from foxtrade.micro_scalp import (
    detect_pullback_resume,
    micro_enabled,
    micro_exit_overlay,
    micro_lots,
    spot_bank_hit as micro_spot_bank_hit,
)
from foxtrade.daily_pa import (
    daily_pa_enabled,
    daily_pa_exit_overlay,
    daily_pa_lots,
    detect_daily_pa,
    spot_bank_hit as pa_spot_bank_hit,
)
from foxtrade.balanced import opposite_side, split_hedge_lots
from foxtrade.decay_memory import DecayMemory
from foxtrade.market_direction import analyze_market
from backtest_month_ledger import (
    OUT,
    LedgerState,
    _clamp_lots_to_cash,
    _load_month_days,
    _lots_for_balance,
    _session_end_idx,
    month_cfg,
    write_html as write_ledger_html,
    _inr,
)

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent


@dataclass
class OpenPos:
    side: str
    entry_i: int
    entry_px: float
    entry_spot: float
    lots: int
    qty: int
    peak: float
    stop_px: float
    target_px: float
    note: str
    conf: float = 0.0
    event: str = ""
    deployable_at_entry: float = 0.0
    balance_at_entry: float = 0.0
    invested: float = 0.0
    soft: bool = False
    micro: bool = False
    daily_pa: bool = False
    # Balanced 70/30 hedge (optional opposite ATM leg)
    hedge_side: str | None = None
    hedge_lots: int = 0
    hedge_qty: int = 0
    hedge_entry_px: float = 0.0
    hedge_peak: float = 0.0
    hedge_invested: float = 0.0


def _load_bars(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        out.append(
            {
                "ts": r.get("ts"),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
        )
    return out


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _hhmm(bars: list[dict[str, Any]], i: int, cfg: dict[str, Any]) -> str:
    ts = bars[i].get("ts")
    if isinstance(ts, str) and len(ts) >= 16:
        return ts[11:16]
    sh, sm = map(int, str(cfg.get("session_start", "09:15")).split(":")[:2])
    total = sh * 60 + sm + i
    return f"{total // 60:02d}:{total % 60:02d}"


def _synth_px(
    closes: list[float],
    side: str,
    entry_i: int,
    entry_prem: float,
    i: int,
    *,
    delta: float,
    theta: float,
) -> float:
    """Premium at bar i given entry at entry_i — uses only closes[entry_i..i]."""
    ds = float(closes[i]) - float(closes[entry_i])
    held = i - entry_i
    if side == "CE":
        px = entry_prem + delta * ds - theta * held
    else:
        px = entry_prem - delta * ds - theta * held
    return max(5.0, px)


def simulate_day_detailed(
    bars: list[dict[str, Any]],
    cfg: dict[str, Any],
    state: LedgerState,
    *,
    day: str,
    is_expiry_day: bool = False,
    decay_mem: DecayMemory | None = None,
    ce_open: float | None = None,
    pe_open: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Full minute walk mirroring live algo1 directional path.
    Records timeline of ENTRY/EXIT/WATCH/SKIP with reasons.
    Optional decay_mem: wait-for-crush then enter highest est-profit % side.
    """
    day_row: dict[str, Any] = {
        "date": day,
        "balance_open": round(state.balance, 2),
        "is_expiry_day": is_expiry_day,
        "spot_open": bars[0]["close"] if bars else 0.0,
        "spot_close": bars[-1]["close"] if bars else 0.0,
        "trades": [],
        "timeline": [],
        "setups": 0,
        "lots_total": 0,
        "gross": 0.0,
        "charges": 0.0,
        "net": 0.0,
        "wins": 0,
        "losses": 0,
        "minutes": 0,
        "analyses": 0,
        "allow_true": 0,
        "post_decay_entries": 0,
        "halted": False,
        "skip": "",
        "post_decay": {},
    }
    if state.halted or state.check_halt():
        day_row["skip"] = state.halt_reason or "capital halt"
        day_row["halted"] = True
        day_row["balance_close"] = round(state.balance, 2)
        day_row["spot_chg"] = 0.0
        return day_row
    if len(bars) < 40:
        day_row["skip"] = "too few bars"
        day_row["balance_close"] = round(state.balance, 2)
        day_row["spot_chg"] = 0.0
        return day_row

    end_i = _session_end_idx(cfg, len(bars))
    bars = bars[: end_i + 1]
    day_row["spot_close"] = bars[-1]["close"]
    day_row["spot_chg"] = round(bars[-1]["close"] - bars[0]["close"], 1)
    day_row["minutes"] = len(bars)
    # Expiry session flags for entry window + fast exits
    if is_expiry_day:
        cfg = {**cfg, "treat_as_expiry": True, "is_expiry_session": True}

    entry_prem = (
        float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))
    ) / 2.0
    lot_size = int(cfg.get("lot_size", 75))
    max_setups = int(cfg.get("max_trades_per_day", 15))
    cooldown = int(cfg.get("entry_cooldown_minutes", 12))
    delta = float(cfg.get("synth_delta", 0.50))
    theta = float(cfg.get("synth_theta_per_min", 0.015))
    min_lots = int(cfg.get("min_lots_per_trade", 4))

    closes: list[float] = []
    open_pos: OpenPos | None = None
    last_exit_i = -10_000
    persist_side: str | None = None
    persist_n = 0
    losses = 0
    wins = 0
    primary_wins = 0  # bank_after_win ignores micro/soft/PA scalps
    primary_losses = 0  # loss-halt ignores scalp scratches
    day_net = 0.0
    day_net_primary = 0.0  # daily-loss circuit ignores scalp scratches
    last_skip = ""
    last_watch: dict[str, Any] | None = None
    # Synth CE/PE open for decay memory path (ATM mid-band if not provided)
    if ce_open is None:
        ce_open = entry_prem
    if pe_open is None:
        pe_open = entry_prem
    spot0 = float(bars[0]["close"])
    if decay_mem is not None:
        decay_mem.set_as_of(day)

    def push_ev(i: int, event: str, **extra: Any) -> None:
        row = {
            "i": i,
            "t": _hhmm(bars, i, cfg),
            "spot": round(closes[-1], 2) if closes else None,
            "event": event,
            **extra,
        }
        day_row["timeline"].append(row)

    for i, bar in enumerate(bars):
        if state.halted:
            break

        # ---- causal: only this bar and past ----
        closes.append(float(bar["close"]))
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)

        # ---- decay memory: synth CE/PE OHLC for this bar ----
        pd_sig: dict[str, Any] = {}
        if decay_mem is not None and cfg.get("post_decay_entry_enabled", True):
            ds = closes[-1] - spot0
            held = float(i)
            ce_px = max(5.0, float(ce_open) + delta * ds - theta * held)
            pe_px = max(5.0, float(pe_open) - delta * ds - theta * held)
            decay_mem.update_live(
                ce=ce_px,
                pe=pe_px,
                spot=closes[-1],
                day=day,
                source="backtest",
                spot_open=float(bars[0].get("open") or bars[0]["close"]),
                spot_high=float(bar.get("high") or closes[-1]),
                spot_low=float(bar.get("low") or closes[-1]),
            )
            pd_sig = decay_mem.analyze(cfg).as_dict()
            day_row["post_decay"] = pd_sig

        # ---- manage open every minute ----
        if open_pos is not None:
            cur = _synth_px(
                closes, open_pos.side, open_pos.entry_i, open_pos.entry_px, i,
                delta=delta, theta=theta,
            )
            hedge_cur = 0.0
            if open_pos.hedge_lots > 0 and open_pos.hedge_side:
                hedge_cur = _synth_px(
                    closes,
                    open_pos.hedge_side,
                    open_pos.entry_i,
                    open_pos.hedge_entry_px,
                    i,
                    delta=delta,
                    theta=theta,
                )
                open_pos.hedge_peak = max(open_pos.hedge_peak, hedge_cur)
            held = float(i - open_pos.entry_i)
            if open_pos.daily_pa:
                exit_cfg = {**cfg, **daily_pa_exit_overlay(cfg)}
            elif open_pos.micro:
                exit_cfg = {**cfg, **micro_exit_overlay(cfg)}
            elif open_pos.soft:
                exit_cfg = {**cfg, **soft_exit_overlay(cfg)}
            else:
                exit_cfg = cfg
            # Exit driven by analyzed (primary) leg — same FastTrack rules
            dec = manage_percent_exit(
                entry=open_pos.entry_px,
                px=cur,
                peak=open_pos.peak,
                cfg=exit_cfg,
                held_minutes=held,
            )
            if dec.get("action") != "exit":
                bank = None
                if open_pos.micro:
                    bank = micro_spot_bank_hit(
                        side=open_pos.side,
                        entry_spot=open_pos.entry_spot,
                        spot=closes[-1],
                        entry_px=open_pos.entry_px,
                        px=cur,
                        cfg=cfg,
                    )
                elif open_pos.daily_pa:
                    bank = pa_spot_bank_hit(
                        side=open_pos.side,
                        entry_spot=open_pos.entry_spot,
                        spot=closes[-1],
                        entry_px=open_pos.entry_px,
                        px=cur,
                        cfg=cfg,
                    )
                if bank:
                    dec = {**dec, **bank}
            open_pos.peak = float(dec.get("peak") or open_pos.peak)
            open_pos.stop_px = float(dec.get("stop_px") or open_pos.stop_px)
            open_pos.target_px = float(dec.get("target_px") or open_pos.target_px)
            force_eod = i >= end_i
            if dec.get("action") == "exit" or force_eod:
                reason = (
                    "session end"
                    if force_eod and dec.get("action") != "exit"
                    else str(dec.get("reason"))
                )
                gross = (cur - open_pos.entry_px) * open_pos.qty
                chg = estimate_option_charges(open_pos.entry_px, cur, open_pos.qty, cfg)
                charges = float(chg["charges"])
                hedge_gross = 0.0
                hedge_charges = 0.0
                if open_pos.hedge_lots > 0 and open_pos.hedge_side:
                    hedge_gross = (hedge_cur - open_pos.hedge_entry_px) * open_pos.hedge_qty
                    hchg = estimate_option_charges(
                        open_pos.hedge_entry_px, hedge_cur, open_pos.hedge_qty, cfg
                    )
                    hedge_charges = float(hchg["charges"])
                gross_all = gross + hedge_gross
                charges_all = charges + hedge_charges
                net = gross_all - charges_all
                day_net += net
                is_scalp = bool(open_pos.soft or open_pos.micro or open_pos.daily_pa)
                if not is_scalp:
                    day_net_primary += net
                if net > 0:
                    wins += 1
                    if not is_scalp:
                        primary_wins += 1
                elif net < 0:
                    losses += 1
                    if not is_scalp:
                        primary_losses += 1
                invested = float(
                    open_pos.invested
                    or round(
                        open_pos.entry_px * open_pos.qty
                        + open_pos.hedge_entry_px * open_pos.hedge_qty,
                        2,
                    )
                )
                # Premium already deducted at entry — credit sale, debit charges
                proceeds = float(cur) * open_pos.qty + float(hedge_cur) * open_pos.hedge_qty
                state.balance = round(state.balance + proceeds - charges_all, 2)
                lots_all = open_pos.lots + open_pos.hedge_lots
                trade = {
                    "day": day,
                    "side": open_pos.side,
                    "entry_time": _hhmm(bars, open_pos.entry_i, cfg),
                    "exit_time": _hhmm(bars, i, cfg),
                    "entry_i": open_pos.entry_i,
                    "exit_i": i,
                    "held_m": i - open_pos.entry_i,
                    "entry_px": round(open_pos.entry_px, 2),
                    "exit_px": round(cur, 2),
                    "peak_px": round(open_pos.peak, 2),
                    "lots": lots_all,
                    "primary_lots": open_pos.lots,
                    "qty": open_pos.qty + open_pos.hedge_qty,
                    "invested": invested,
                    "balance_at_entry": round(open_pos.balance_at_entry, 2),
                    "invested_pct_of_balance": round(
                        100.0 * invested / max(open_pos.balance_at_entry, 1.0), 1
                    ),
                    "invested_pct_of_deployable": round(
                        100.0 * invested / max(open_pos.deployable_at_entry, 1.0), 1
                    ),
                    "gross": round(gross_all, 2),
                    "charges": round(charges_all, 2),
                    "net": round(net, 2),
                    "primary_net": round(gross - charges, 2),
                    "hedge_net": round(hedge_gross - hedge_charges, 2),
                    "hedge_side": open_pos.hedge_side,
                    "hedge_lots": open_pos.hedge_lots,
                    "hedge_entry_px": round(open_pos.hedge_entry_px, 2) if open_pos.hedge_lots else None,
                    "hedge_exit_px": round(hedge_cur, 2) if open_pos.hedge_lots else None,
                    "mult": round(cur / open_pos.entry_px, 3),
                    "reason": reason,
                    "note": open_pos.note,
                    "event": open_pos.event,
                    "conf": open_pos.conf,
                    "soft": bool(open_pos.soft),
                    "micro": bool(open_pos.micro),
                    "daily_pa": bool(open_pos.daily_pa),
                    "hedged": bool(open_pos.hedge_lots > 0),
                    "balance_after": round(state.balance, 2),
                    "deployable_at_entry": round(open_pos.deployable_at_entry, 2),
                    "entry_spot": round(open_pos.entry_spot, 2),
                    "exit_spot": round(closes[-1], 2),
                }
                day_row["trades"].append(trade)
                state.ledger.append(trade)
                if verbose:
                    hedge_tag = (
                        f"+{open_pos.hedge_side}{open_pos.hedge_lots}L"
                        if open_pos.hedge_lots
                        else ""
                    )
                    print(
                        f"      {day} {_hhmm(bars, open_pos.entry_i, cfg)}→{_hhmm(bars, i, cfg)} "
                        f"{open_pos.side}{open_pos.lots}L{hedge_tag} invested=₹{invested:,.0f} "
                        f"of bal ₹{open_pos.balance_at_entry:,.0f} "
                        f"({trade['invested_pct_of_balance']}%) "
                        f"net={net:+,.0f} free=₹{state.balance:,.0f}",
                        flush=True,
                    )
                day_row["gross"] += gross_all
                day_row["charges"] += charges_all
                day_row["net"] += net
                day_row["lots_total"] += lots_all
                push_ev(
                    i,
                    "EXIT",
                    side=open_pos.side,
                    px=round(cur, 2),
                    net=round(net, 2),
                    lots=lots_all,
                    hedge_side=open_pos.hedge_side,
                    hedge_lots=open_pos.hedge_lots,
                    reason=reason,
                    mult=round(cur / open_pos.entry_px, 3),
                    bal=round(state.balance, 2),
                )
                last_exit_i = i
                open_pos = None
                if state.check_halt():
                    day_row["halted"] = True
                    push_ev(i, "HALT", reason=state.halt_reason)
                    break
                if force_eod:
                    break
            else:
                # mark mark-to-market pulse every 15m while open
                if (i - open_pos.entry_i) % 15 == 0 and i > open_pos.entry_i:
                    push_ev(
                        i,
                        "MTM",
                        side=open_pos.side,
                        px=round(cur, 2),
                        sl=open_pos.stop_px,
                        tg=open_pos.target_px,
                        mult=round(cur / open_pos.entry_px, 3),
                        peak=round(open_pos.peak, 2),
                        hedge_px=round(hedge_cur, 2) if open_pos.hedge_lots else None,
                    )
            continue

        # ---- flat: live-like entry scan every minute ----
        if i >= end_i:
            break
        if day_row["setups"] >= max_setups:
            skip = f"day setup cap {max_setups}"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue
        if i - last_exit_i < cooldown:
            skip = f"cooldown {cooldown - (i - last_exit_i)}m"
            if skip != last_skip and (i - last_exit_i) == 0:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue
        # Scalp scratches must not trip the day circuit (Nov2 PA −3k blocked +12k DIR).
        if daily_loss_halted(day_net_primary, cfg):
            skip = "daily loss circuit"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue
        # Scalp (PA/micro/soft) scratches must not halt the primary path.
        if primary_losses >= int(cfg.get("max_losses_before_halt", 8)):
            skip = f"loss halt {primary_losses}"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue
        # Bank only after a primary (DIR/POST) win — micro/soft scalps must not
        # lock out the day's main SR/ORB (Nov2 micro@10:21 stole +6k CE@12:01).
        if cfg.get("bank_after_win") and day_net > 0 and primary_wins >= 1:
            skip = "bank after win"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue

        # Sensex Thursday: flat until gamma blast arms (then expiry-style playbook)
        if str(cfg.get("underlying") or "").upper() == "SENSEX":
            try:
                from datetime import date as _date

                from foxtrade.sensex_calendar import is_sensex_gamma_conditional_day
                from foxtrade.sensex_gamma import detect_sensex_gamma_blast

                _dd = _date.fromisoformat(str(day)[:10])
                if is_sensex_gamma_conditional_day(_dd, cfg):
                    blast = detect_sensex_gamma_blast(closes, cfg=cfg)
                    if not blast.get("allow"):
                        skip = f"thu gamma watch: {blast.get('reason')}"
                        if skip != last_skip and i % 15 == 0:
                            push_ev(
                                i,
                                "WATCH",
                                reason=str(blast.get("reason") or "thu gamma")[:160],
                            )
                            last_skip = skip
                        persist_side, persist_n = None, 0
                        continue
                    if not is_expiry_day:
                        is_expiry_day = True
                        day_row["is_expiry_day"] = True
                        day_row["thu_gamma_armed"] = True
                        cfg = {
                            **cfg,
                            "treat_as_expiry": True,
                            "is_expiry_session": True,
                        }
                        push_ev(
                            i,
                            "GAMMA_ARM",
                            side=blast.get("side"),
                            reason=str(blast.get("reason") or "")[:160],
                        )
            except Exception:
                pass

        analysis = analyze_market(
            closes,
            cfg=cfg,
            ema9=ema9,
            ema21=ema21,
            is_expiry_day=is_expiry_day,
        )
        day_row["analyses"] += 1
        side = analysis.get("side")
        conf = float(analysis.get("confidence") or 0)
        reason = str(analysis.get("reason") or "")
        allow = bool(analysis.get("allow_entry")) and side in ("CE", "PE")
        event = str(analysis.get("event") or "")
        is_post_decay = False

        # Post-decay: WAIT / boost / standalone enter (max est profit %)
        # Aggressive profile can set post_decay_respect_session_delay=false (pre-overnight ~100%).
        entry_delay = int(cfg.get("entry_delay_minutes", 12))
        prefer_after = int(cfg.get("prefer_entry_after_minutes", entry_delay))
        if cfg.get("post_decay_respect_session_delay", True):
            pd_ready_i = max(entry_delay, prefer_after, int(cfg.get("post_decay_min_bar", 20)))
        else:
            pd_ready_i = int(cfg.get("post_decay_min_bar", 0))
        # Dedicated post-decay confirm streak (survives mkt "warming up" bars).
        # Train: Sep 29 expiry knife dies <3m; Oct 13 crush holds → enter.
        if cfg.get("post_decay_entry_enabled", True) and pd_sig:
            pd_action = str(pd_sig.get("action") or "NONE")
            pd_side = pd_sig.get("side")
            pd_conf = float(pd_sig.get("confidence") or 0)
            pd_min = float(cfg.get("post_decay_min_confidence", 0.62))
            if pd_action == "WAIT" and (not side or side == pd_side):
                skip = f"post-decay WAIT: {pd_sig.get('reason')}"
                if skip != last_skip:
                    push_ev(i, "WAIT_DECAY", side=pd_side, reason=str(pd_sig.get("reason") or "")[:160])
                    last_skip = skip
                persist_side, persist_n = None, 0
                day_row["_pd_persist_n"] = 0
                day_row["_pd_persist_side"] = None
                continue
            pd_est = float(pd_sig.get("expected_profit_pct") or 0)
            # Train Sep 29 vs Oct 13: weak expiry crush knives; monster est keeps.
            expiry_min_est = float(cfg.get("post_decay_expiry_min_est_pct", 300))
            if (
                is_expiry_day
                and pd_action in ("ENTER_CE", "ENTER_PE")
                and pd_est < expiry_min_est
            ):
                skip = f"post-decay expiry est +{pd_est:.0f}% < {expiry_min_est:.0f}%"
                if skip != last_skip:
                    push_ev(i, "WAIT_DECAY", side=pd_side, reason=skip)
                    last_skip = skip
                day_row["_pd_persist_n"] = 0
                day_row["_pd_persist_side"] = None
                persist_side, persist_n = None, 0
                continue
            pd_enter_ok = (
                pd_action in ("ENTER_CE", "ENTER_PE")
                and pd_side in ("CE", "PE")
                and pd_conf >= pd_min
                and i >= pd_ready_i
            )
            if pd_enter_ok:
                if day_row.get("_pd_persist_side") == pd_side:
                    day_row["_pd_persist_n"] = int(day_row.get("_pd_persist_n") or 0) + 1
                else:
                    day_row["_pd_persist_side"] = pd_side
                    day_row["_pd_persist_n"] = 1
                pd_need = int(cfg.get("post_decay_confirm_minutes", 0))
                if is_expiry_day:
                    pd_need = max(
                        pd_need,
                        int(cfg.get("post_decay_expiry_confirm_minutes", 3)),
                    )
                # Monster crush (Oct 13): fast confirm — don't miss while warming up
                fast_est = float(cfg.get("post_decay_fast_confirm_est_pct", 400))
                if pd_est >= fast_est:
                    pd_need = min(pd_need, 1) if pd_need > 0 else 0
                # Lesson 2026-10-26: weak est needs longer confirm (knife bounce)
                strong_est = float(cfg.get("post_decay_strong_est_pct", 200))
                if 0 < pd_est < strong_est:
                    pd_need = max(
                        pd_need,
                        int(cfg.get("post_decay_weak_est_confirm_minutes", 5)),
                    )
                if cfg.get("post_decay_respect_session_delay", True) and pd_need <= 0:
                    pd_need = 3
                # Apply boost / standalone once confirm streak ready (or need=0).
                if pd_need <= 0 or int(day_row.get("_pd_persist_n") or 0) >= pd_need:
                    if allow and side == pd_side:
                        conf = max(conf, pd_conf)
                        reason = f"{reason} | +post-decay {pd_sig.get('setup')}"
                        event = f"{event}+post_decay"
                        is_post_decay = True
                    elif allow and side and side != pd_side and cfg.get(
                        "post_decay_prefer_over_mkt", False
                    ):
                        side = pd_side
                        conf = pd_conf
                        reason = f"post-decay override {pd_sig.get('setup')}: {pd_sig.get('reason')}"
                        event = f"post_decay_{pd_sig.get('setup')}"
                        is_post_decay = True
                    elif (not allow or side not in ("CE", "PE")) and cfg.get(
                        "post_decay_standalone_entry", True
                    ):
                        side = pd_side
                        conf = pd_conf
                        reason = f"post-decay {pd_sig.get('setup')}: {pd_sig.get('reason')}"
                        event = f"post_decay_{pd_sig.get('setup')}"
                        allow = True
                        is_post_decay = True
                else:
                    skip = (
                        f"post-decay confirm "
                        f"{day_row.get('_pd_persist_n')}/{pd_need}m"
                    )
                    if skip != last_skip:
                        push_ev(i, "WAIT_DECAY", side=pd_side, reason=skip)
                        last_skip = skip
            else:
                if i < pd_ready_i and pd_action in ("ENTER_CE", "ENTER_PE"):
                    skip = f"post-decay wait bar {i}/{pd_ready_i}"
                    if skip != last_skip:
                        push_ev(i, "WAIT_DECAY", side=pd_side, reason=skip)
                        last_skip = skip
                day_row["_pd_persist_n"] = 0
                day_row["_pd_persist_side"] = None

        watch = {
            "side": side,
            "conf": conf,
            "event": event,
            "allow": allow,
            "reason": reason[:180],
            "vs_open": analysis.get("vs_open"),
            "mom": analysis.get("momentum"),
            "post_decay": is_post_decay,
        }
        # Log when bias / allow flips (minute-to-minute state change)
        if last_watch is None or (
            last_watch.get("side") != watch["side"]
            or last_watch.get("allow") != watch["allow"]
            or last_watch.get("event") != watch["event"]
        ):
            push_ev(
                i,
                "WATCH" if not allow else "SIGNAL",
                side=side,
                conf=watch["conf"],
                mkt_event=watch["event"],
                allow=allow,
                reason=watch["reason"],
                vs=watch["vs_open"],
                mom=watch["mom"],
                est_pct=pd_sig.get("expected_profit_pct") if is_post_decay else None,
            )
            last_watch = watch
            last_skip = ""

        # Optional: only take crush→bounce (blockbuster) setups
        if allow and cfg.get("post_decay_only_entries") and not is_post_decay:
            allow = False
            skip = "post-decay-only: skip directional"
            if skip != last_skip:
                push_ev(i, "SKIP", side=side, reason=skip)
                last_skip = skip
            persist_side, persist_n = None, 0
            continue

        is_soft = False
        is_micro = False
        is_pa = False
        soft_analysis: dict[str, Any] | None = None
        day_still_flat = int(day_row.get("setups") or 0) == 0

        # Micro 10–15pt pullback-resume: morning window only (before soft PE).
        if (
            not allow
            and micro_enabled(cfg)
            and (not cfg.get("micro_scalp_only_if_flat", True) or day_still_flat)
            and not is_post_decay
        ):
            ms = detect_pullback_resume(closes, cfg, bar_i=i)
            if bool(ms.get("allow_entry")) and ms.get("side") in ("CE", "PE"):
                allow = True
                is_micro = True
                side = str(ms["side"])
                conf = float(ms.get("confidence") or 0)
                reason = str(ms.get("reason") or "micro scalp")
                event = str(ms.get("event") or "MICRO_PB_RESUME")
                analysis = ms
                day_row["micro_signals"] = int(day_row.get("micro_signals") or 0) + 1

        # Daily soft PE scalp: only when primary flat and soft enabled.
        # Wait past daily_soft_entry_after_minutes so primary morning SR/ORB
        # keeps first claim (Oct28 soft@10:41 stole +6k CE@11:02).
        soft_after = int(cfg.get("daily_soft_entry_after_minutes", 150))
        if (
            not allow
            and soft_enabled(cfg)
            and i >= soft_after
            and (not cfg.get("daily_soft_only_if_flat", True) or day_still_flat)
            and not is_post_decay
        ):
            scfg = merge_soft_cfg(cfg)
            soft_analysis = analyze_market(
                closes,
                cfg=scfg,
                ema9=ema9,
                ema21=ema21,
                is_expiry_day=is_expiry_day,
            )
            if (
                bool(soft_analysis.get("allow_entry"))
                and soft_analysis.get("side") == "PE"
            ):
                allow = True
                is_soft = True
                side = "PE"
                conf = float(soft_analysis.get("confidence") or 0)
                reason = f"daily_soft: {soft_analysis.get('reason') or ''}"
                event = tag_soft_event(str(soft_analysis.get("event") or "PE"))
                analysis = soft_analysis
                day_row["soft_signals"] = int(day_row.get("soft_signals") or 0) + 1

        # Daily PA / momentum / local S-R — try most flat sessions
        if (
            not allow
            and daily_pa_enabled(cfg)
            and (not cfg.get("daily_pa_only_if_flat", True) or day_still_flat)
            and not is_post_decay
        ):
            pa = detect_daily_pa(closes, cfg, bar_i=i)
            if bool(pa.get("allow_entry")) and pa.get("side") in ("CE", "PE"):
                allow = True
                is_pa = True
                side = str(pa["side"])
                conf = float(pa.get("confidence") or 0)
                reason = str(pa.get("reason") or "daily_pa")
                event = str(pa.get("event") or "DAILY_PA")
                analysis = pa
                day_row["pa_signals"] = int(day_row.get("pa_signals") or 0) + 1

        if not allow:
            persist_side, persist_n = None, 0
            continue
        # Signal must persist N minutes (anti fake-break)
        persist_need = int(cfg.get("signal_persist_minutes", 0))
        if is_pa:
            persist_need = int(cfg.get("daily_pa_persist_minutes", 1))
        elif is_micro:
            persist_need = int(cfg.get("micro_scalp_confirm_bars", 1))
        elif is_soft:
            persist_need = int(cfg.get("daily_soft_persist_minutes", 1))
        if is_post_decay:
            # Confirm already enforced via _pd_persist_n streak above.
            persist_need = 0
        # Profit-learn SR persist:
        #  hard+strong mom → 1 bar (Jul24); mid vs 50–hard → 1 bar (Apr2);
        #  else 2 bars (Oct28 soft). Hard+weak mom stays 2 → skips Oct27 knife.
        if (
            event in ("SR_RECLAIM", "SR_REJECT")
            and persist_need > 0
            and not is_soft
            and not is_micro
            and not is_pa
        ):
            sr_vs = abs(float(analysis.get("vs_open") or 0))
            sr_mom = abs(float(analysis.get("momentum") or 0))
            hard_pts = float(cfg.get("trend_hard_pts", 70))
            mid_vs = float(cfg.get("sr_fast_persist_min_vs", 50))
            fast_mom = float(cfg.get("sr_fast_persist_min_mom", 18))
            fast = (sr_vs >= hard_pts and sr_mom >= fast_mom) or (
                mid_vs <= sr_vs < hard_pts
            )
            if fast:
                persist_need = int(cfg.get("sr_signal_persist_minutes", 1))
            else:
                persist_need = max(persist_need, 2)
        if persist_need > 0:
            if side == persist_side:
                persist_n += 1
            else:
                persist_side, persist_n = side, 1
            if persist_n < persist_need:
                continue
        else:
            persist_side, persist_n = side, 1

        day_row["allow_true"] += 1
        lots = _lots_for_balance(
            cfg,
            entry=entry_prem,
            available=state.balance,
            deploy_pct=state.deploy_pct,
            deploy_cap=state.deploy_cap,
        )
        if losses >= 2 and lots > 0:
            lots = _clamp_lots_to_cash(
                max(min_lots, lots // 2),
                entry=entry_prem,
                available=state.balance,
                cfg=cfg,
            )
        if is_pa:
            lots = daily_pa_lots(cfg, lots)
            lots = _clamp_lots_to_cash(
                lots, entry=entry_prem, available=state.balance, cfg=cfg
            )
        elif is_micro:
            lots = micro_lots(cfg, lots)
            lots = _clamp_lots_to_cash(
                lots, entry=entry_prem, available=state.balance, cfg=cfg
            )
        elif is_soft:
            lots = soft_lots(cfg, lots)
            lots = _clamp_lots_to_cash(
                lots, entry=entry_prem, available=state.balance, cfg=cfg
            )
        if is_pa:
            soft_min = int(cfg.get("daily_pa_min_lots", min_lots))
        elif is_micro:
            soft_min = int(cfg.get("micro_scalp_min_lots", min_lots))
        elif is_soft:
            soft_min = int(cfg.get("daily_soft_min_lots", min_lots))
        else:
            soft_min = min_lots
        if lots < soft_min:
            skip = f"need ≥{soft_min} lots (free cash ₹{state.balance:,.0f})"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip, bal=round(state.balance, 0))
                last_skip = skip
            continue

        # Balanced: 70% analyzed side / 30% opposite ATM hedge
        primary_lots, hedge_lots = split_hedge_lots(lots, cfg)
        hedge_side_s: str | None = (
            opposite_side(str(side)) if hedge_lots > 0 else None
        )
        hedge_entry = float(entry_prem) if hedge_lots > 0 else 0.0

        invested_primary = round(entry_prem * primary_lots * lot_size, 2)
        invested_hedge = (
            round(hedge_entry * hedge_lots * lot_size, 2) if hedge_lots else 0.0
        )
        invested = round(invested_primary + invested_hedge, 2)
        if invested > state.balance + 1e-6:
            skip = f"invested ₹{invested:,.0f} > free ₹{state.balance:,.0f}"
            if skip != last_skip:
                push_ev(i, "SKIP", reason=skip)
                last_skip = skip
            continue

        bal_before = float(state.balance)
        deployable = float(state.deployable)
        state.balance = round(state.balance - invested, 2)  # lock premium in position

        if is_pa:
            entry_cfg = {**cfg, **daily_pa_exit_overlay(cfg)}
        elif is_micro:
            entry_cfg = {**cfg, **micro_exit_overlay(cfg)}
        elif is_soft:
            entry_cfg = {**cfg, **soft_exit_overlay(cfg)}
        else:
            entry_cfg = cfg
        note = reason
        if hedge_lots > 0:
            note = (
                f"[Balanced {primary_lots}/{hedge_lots} "
                f"{side}/{hedge_side_s}] {reason}"
            )
        open_pos = OpenPos(
            side=str(side),
            entry_i=i,
            entry_px=entry_prem,
            entry_spot=closes[-1],
            lots=primary_lots,
            qty=primary_lots * lot_size,
            peak=entry_prem,
            stop_px=stop_price(entry_prem, entry_cfg),
            target_px=target_price(entry_prem, entry_cfg, trail=True),
            note=note,
            conf=conf,
            event=event,
            deployable_at_entry=deployable,
            balance_at_entry=bal_before,
            invested=invested,
            soft=is_soft,
            micro=is_micro,
            daily_pa=is_pa,
            hedge_side=hedge_side_s,
            hedge_lots=hedge_lots,
            hedge_qty=hedge_lots * lot_size,
            hedge_entry_px=hedge_entry,
            hedge_peak=hedge_entry,
            hedge_invested=invested_hedge,
        )
        day_row["setups"] += 1
        if is_pa:
            day_row["pa_entries"] = int(day_row.get("pa_entries") or 0) + 1
        if is_micro:
            day_row["micro_entries"] = int(day_row.get("micro_entries") or 0) + 1
        if is_soft:
            day_row["soft_entries"] = int(day_row.get("soft_entries") or 0) + 1
        if is_post_decay:
            day_row["post_decay_entries"] = int(day_row.get("post_decay_entries") or 0) + 1
        if hedge_lots > 0:
            day_row["hedge_entries"] = int(day_row.get("hedge_entries") or 0) + 1
        day_row["invested_total"] = round(
            float(day_row.get("invested_total") or 0) + invested, 2
        )
        est_tag = ""
        if is_post_decay and pd_sig.get("expected_profit_pct"):
            est_tag = f" est+{float(pd_sig['expected_profit_pct']):.0f}%"
        if is_pa:
            path_tag = "PA"
        elif is_micro:
            path_tag = "MICRO"
        elif is_soft:
            path_tag = "SOFT"
        elif is_post_decay:
            path_tag = "POST" + est_tag
        else:
            path_tag = "DIR"
        if hedge_lots > 0:
            path_tag = f"BAL/{path_tag}"
        push_ev(
            i,
            "ENTRY",
            side=side,
            px=entry_prem,
            lots=primary_lots + hedge_lots,
            primary_lots=primary_lots,
            hedge_side=hedge_side_s,
            hedge_lots=hedge_lots,
            invested=invested,
            sl=open_pos.stop_px,
            tg=open_pos.target_px,
            conf=open_pos.conf,
            mkt_event=open_pos.event,
            reason=open_pos.note[:160],
            deployable=round(deployable, 0),
            bal_before=round(bal_before, 2),
            free_after=round(state.balance, 2),
            post_decay=is_post_decay,
            soft=is_soft,
            micro=is_micro,
            daily_pa=is_pa,
            hedged=bool(hedge_lots > 0),
            est_pct=pd_sig.get("expected_profit_pct") if is_post_decay else None,
        )
        if verbose:
            hedge_tag = (
                f"+{hedge_side_s}{hedge_lots}L" if hedge_lots else ""
            )
            print(
                f"      ENTRY {day} {_hhmm(bars, i, cfg)} {side}{primary_lots}L"
                f"{hedge_tag} {path_tag} "
                f"invested=₹{invested:,.0f} ≤ bal ₹{bal_before:,.0f} "
                f"→ free ₹{state.balance:,.0f}",
                flush=True,
            )
        last_skip = ""

    day_row["gross"] = round(day_row["gross"], 2)
    day_row["charges"] = round(day_row["charges"], 2)
    day_row["net"] = round(day_row["net"], 2)
    day_row["wins"] = wins
    day_row["losses"] = losses
    day_row["balance_close"] = round(state.balance, 2)
    return day_row


_WEEK_SERIES_CACHE: list[dict[str, Any]] | None = None


def _week_series_rows() -> list[dict[str, Any]]:
    global _WEEK_SERIES_CACHE
    if _WEEK_SERIES_CACHE is not None:
        return _WEEK_SERIES_CACHE
    try:
        from analyze_crush_bounce import build_week_series, _sessions

        _WEEK_SERIES_CACHE = build_week_series(_sessions())
    except Exception:
        _WEEK_SERIES_CACHE = []
    return _WEEK_SERIES_CACHE


def _seed_decay_days_before(start_date: str) -> dict[str, Any]:
    """Prior CE/PE day bars (synth) so post-decay has history before the window."""
    seed: dict[str, Any] = {}
    for r in _week_series_rows():
        d = r.get("date")
        if not d or d >= start_date:
            continue
        seed[d] = {
            "date": d,
            "finalized": True,
            "source": "synth_seed",
            "strike": r.get("strike"),
            "expiry": r.get("week"),
            "ce_open": r.get("ce_open"),
            "ce_high": r.get("ce_high"),
            "ce_low": r.get("ce_low"),
            "ce_close": r.get("ce_close"),
            "pe_open": r.get("pe_open"),
            "pe_high": r.get("pe_high"),
            "pe_low": r.get("pe_low"),
            "pe_close": r.get("pe_close"),
            "spot_open": r.get("spot_open"),
            "spot_close": r.get("spot_close"),
        }
    return seed


def _day_prem_opens(day: str) -> tuple[float | None, float | None]:
    """CE/PE open premiums for the day from week synth series (carry)."""
    for r in _week_series_rows():
        if r.get("date") == day:
            return float(r["ce_open"]), float(r["pe_open"])
    return None, None


def run_detailed(
    *,
    initial: float = 100_000.0,
    all_available: bool = True,
    calendar_days: int = 31,
    use_post_decay: bool = True,
    deploy_pct: float = 0.75,
    cfg_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = month_cfg()  # paper profile + post-decay from config.json
    cfg["capital_inr"] = float(initial)
    cfg["deploy_pct_of_balance"] = float(deploy_pct)
    if use_post_decay:
        cfg.setdefault("post_decay_entry_enabled", True)
        cfg.setdefault("post_decay_standalone_entry", True)
    else:
        cfg["post_decay_entry_enabled"] = False
    if cfg_overrides:
        cfg.update({k: v for k, v in cfg_overrides.items() if not str(k).startswith("_")})

    days = _load_month_days(calendar_days=calendar_days, all_available=all_available)
    state = LedgerState(
        initial=initial,
        balance=initial,
        deploy_pct=float(deploy_pct),
        deploy_cap=0.0,
    )
    daily: list[dict[str, Any]] = []

    seed = _seed_decay_days_before(days[0]["date"]) if days and use_post_decay else {}
    decay_mem = (
        DecayMemory(persist=False, seed_days=seed) if use_post_decay else None
    )
    if decay_mem is not None:
        print(f"  post-decay memory seeded with {len(seed)} prior sessions", flush=True)

    for n, d in enumerate(days, 1):
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"]) if use_post_decay else (None, None)
        print(f"  [{n}/{len(days)}] {d['date']} bars={len(bars)} bal=₹{state.balance:,.0f}", flush=True)
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
        )
        if decay_mem is not None:
            decay_mem.finalize_day(d["date"])
        daily.append(row)
        if state.halted:
            print(f"  HALTED: {state.halt_reason}")

    tot_gross = sum(r["gross"] for r in daily)
    tot_chg = sum(r["charges"] for r in daily)
    tot_net = sum(r["net"] for r in daily)
    tot_invested = sum(float(t.get("invested") or 0) for t in state.ledger)
    post_n = sum(int(r.get("post_decay_entries") or 0) for r in daily)
    return {
        "title": "Detailed Minute-by-Minute Algorithm Backtest (+ post-decay)",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_minute_live_algo_gates_post_decay",
        "premium_model": (
            "Synthetic ATM premium updated EVERY minute from spot Δ "
            "(delta≈0.5 + theta). analyze_market + post-decay memory + "
            "manage_percent_exit run each bar."
        ),
        "capital": {
            "initial": initial,
            "deploy_pct": state.deploy_pct,
            "deploy_cap": state.deploy_cap,
            "halt_pct": state.halt_pct,
            "halt_floor": state.halt_floor,
            "final_balance": round(state.balance, 2),
            "net_pnl": round(state.balance - initial, 2),
            "return_pct": round(100.0 * (state.balance / initial - 1.0), 2),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "totals": {
            "session_days": len(daily),
            "traded_days": sum(1 for r in daily if r["setups"] > 0),
            "setups": sum(r["setups"] for r in daily),
            "post_decay_entries": post_n,
            "lots": sum(r["lots_total"] for r in daily),
            "invested_total": round(tot_invested, 2),
            "wins": sum(r["wins"] for r in daily),
            "losses": sum(r["losses"] for r in daily),
            "gross": round(tot_gross, 2),
            "charges": round(tot_chg, 2),
            "net": round(tot_net, 2),
            "green_days": sum(1 for r in daily if r["net"] > 0),
            "red_days": sum(1 for r in daily if r["net"] < 0),
            "minutes_processed": sum(r["minutes"] for r in daily),
            "analyses": sum(r["analyses"] for r in daily),
            "signal_minutes": sum(r["allow_true"] for r in daily),
        },
        "config_snapshot": {
            "max_trades_per_day": cfg["max_trades_per_day"],
            "min_lots_per_trade": cfg["min_lots_per_trade"],
            "max_lots_per_trade": cfg["max_lots_per_trade"],
            "entry_cooldown_minutes": cfg["entry_cooldown_minutes"],
            "stop_loss_pct": cfg["stop_loss_pct"],
            "target_pct_low": cfg["target_pct_low"],
            "target_pct_high": cfg["target_pct_high"],
            "entry_style": cfg["entry_style"],
            "orb_minutes": cfg["orb_minutes"],
            "directional_min_confidence": cfg["directional_min_confidence"],
            "min_vs_open_entry": cfg["min_vs_open_entry"],
            "session": f"{cfg['session_start']}–{cfg['session_end']}",
            "lot_size": cfg["lot_size"],
            "risk_per_trade_pct": cfg["risk_per_trade_pct"],
            "post_decay_entry_enabled": cfg.get("post_decay_entry_enabled", True),
            "profile_name": cfg.get("profile_name"),
        },
        "daily": daily,
        "ledger": state.ledger,
        "range": {
            "start": days[0]["date"] if days else None,
            "end": days[-1]["date"] if days else None,
        },
    }


def write_detailed_html(report: dict[str, Any], path: Path) -> None:
    """Ledger summary + per-day minute timeline drill-down."""
    # Base ledger page first (reuse), then append day detail sections
    tmp = path.with_suffix(".ledger.html")
    write_ledger_html(report, tmp)
    base = tmp.read_text(encoding="utf-8")

    day_sections: list[str] = []
    for r in report.get("daily") or []:
        if not r.get("timeline") and r.get("setups", 0) == 0 and not r.get("skip"):
            continue
        ev_rows = []
        for ev in r.get("timeline") or []:
            et = ev.get("event")
            cls = {
                "ENTRY": "g",
                "EXIT": "r" if float(ev.get("net") or 0) < 0 else "g",
                "SIGNAL": "g",
                "WATCH": "f",
                "SKIP": "f",
                "HALT": "r",
                "MTM": "f",
            }.get(str(et), "f")
            detail = ev.get("reason") or ev.get("event") or ""
            extra = []
            if ev.get("side"):
                extra.append(str(ev["side"]))
            if ev.get("lots"):
                extra.append(f"{ev['lots']}L")
            if ev.get("invested") is not None:
                extra.append(f"inv₹{float(ev['invested']):,.0f}")
            if ev.get("px") is not None:
                extra.append(f"@{ev['px']}")
            if ev.get("net") is not None:
                extra.append(_inr(float(ev["net"])))
            if ev.get("conf") is not None:
                extra.append(f"{float(ev['conf']):.0%}")
            if ev.get("mult") is not None:
                extra.append(f"{ev['mult']}×")
            ev_rows.append(
                f"<tr class='{cls}'><td>{ev.get('t')}</td><td>{et}</td>"
                f"<td>{' '.join(extra)}</td><td class='muted'>{detail}</td>"
                f"<td class='num'>{ev.get('spot') or ''}</td></tr>"
            )
        net = float(r.get("net") or 0)
        day_tone = "day-profit" if net > 0 else ("day-loss" if net < 0 else "day-flat")
        pnl_cls = "pnl-pos" if net > 0 else ("pnl-neg" if net < 0 else "pnl-flat")
        day_sections.append(
            f"""
<details class="day {day_tone}" {'open' if r.get('setups') else ''}>
  <summary>
    <b>{r['date']}</b>
    · setups {r.get('setups',0)} · lots {r.get('lots_total',0)}
    · <span class="{pnl_cls}">Day P&amp;L {_inr(r.get('net'))}</span>
    · bal ₹{r.get('balance_close',0):,.0f}
    · spot {r.get('spot_chg',0):+.0f}
    · events {len(r.get('timeline') or [])}
  </summary>
  <div class="day-body">
    <table>
      <thead><tr><th>Time</th><th>Event</th><th>Detail</th><th>Reason / note</th><th>Spot</th></tr></thead>
      <tbody>{''.join(ev_rows) if ev_rows else '<tr><td colspan="5" class="muted">No timeline events</td></tr>'}</tbody>
    </table>
  </div>
</details>"""
        )

    inject = f"""
<style>
details.day {{
  background:var(--card); border:1px solid var(--line); border-radius:12px;
  margin:10px 0; padding:10px 14px;
}}
details.day.day-profit {{
  border-color: rgba(61,190,160,0.55);
  background: linear-gradient(90deg, rgba(61,190,160,0.16), var(--card) 28%);
}}
details.day.day-loss {{
  border-color: rgba(229,115,115,0.55);
  background: linear-gradient(90deg, rgba(229,115,115,0.16), var(--card) 28%);
}}
details.day summary {{ cursor:pointer; list-style:none; }}
details.day summary::-webkit-details-marker {{ display:none; }}
.day-body {{ margin-top:10px; overflow-x:auto; }}
.pnl-pos, .good {{ color:var(--good) !important; font-weight:700; }}
.pnl-neg, .bad {{ color:var(--bad) !important; font-weight:700; }}
.pnl-flat {{ color:var(--muted); }}
</style>
<div class="panel">
  <h2>Minute-by-minute algorithm timeline (per day)</h2>
  <p class="muted" style="margin-top:0">
    Every bar: spot path grows by 1 close → manage open (SL/trail) or
    <code>analyze_market</code> (ORB / gates). WATCH/SIGNAL logged when bias or allow flips.
  </p>
  {''.join(day_sections)}
</div>
"""
    html = base.replace("</div>\n</body>", inject + "</div>\n</body>")
    # title tweak
    html = html.replace(
        "FoxtronTrade — 1-Month Ledger",
        "FoxtronTrade — Detailed Minute Algo Backtest",
        1,
    )
    path.write_text(html, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=0, help="Lookback in months (3 = full dataset)")
    ap.add_argument("--days", type=int, default=0, help="Calendar-day lookback (e.g. 7 = last week)")
    ap.add_argument("--capital", type=float, default=200_000)
    ap.add_argument("--tag", type=str, default="detailed")
    ap.add_argument("--no-post-decay", action="store_true", help="Disable post-decay entries")
    args = ap.parse_args()

    if args.days > 0:
        use_all = False
        calendar_days = args.days
        label = f"last {args.days}d"
        tag = args.tag if args.tag != "detailed" else ("week" if args.days <= 8 else f"d{args.days}")
    elif args.months >= 3:
        use_all = True
        calendar_days = args.months * 31
        label = "full 3mo data"
        tag = args.tag
    elif args.months > 0:
        use_all = False
        calendar_days = args.months * 31
        label = f"{args.months}mo"
        tag = args.tag
    else:
        use_all = True
        calendar_days = 93
        label = "full 3mo data"
        tag = args.tag

    use_pd = not args.no_post_decay
    print(
        f"Detailed minute algo backtest · {label} · "
        f"₹{args.capital:,.0f} · paper_2l · ≤50% cash · "
        f"post-decay={'ON' if use_pd else 'OFF'}"
    )
    report = run_detailed(
        initial=args.capital,
        all_available=use_all,
        calendar_days=calendar_days,
        use_post_decay=use_pd,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / f"{tag}_algo_report.json"
    html_path = OUT / f"{tag}_algo_report.html"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_detailed_html(report, html_path)

    cap, tot = report["capital"], report["totals"]
    print(
        f"Range {report['range']['start']} → {report['range']['end']}\n"
        f"Minutes processed {tot['minutes_processed']:,} · analyses {tot['analyses']:,} · "
        f"signal-minutes {tot['signal_minutes']:,}\n"
        f"Final ₹{cap['final_balance']:,.0f}  net {_inr(cap['net_pnl'])} ({cap['return_pct']:+.1f}%)\n"
        f"Gross {_inr(tot['gross'])}  charges ₹{tot['charges']:,.0f}  "
        f"trades {tot['setups']} (post-decay {tot.get('post_decay_entries', 0)}) "
        f"lots {tot['lots']}  g/r days {tot['green_days']}/{tot['red_days']}\n"
        f"Halted: {cap['halted']} {cap['halt_reason']}\n"
        f"HTML  {html_path}\n"
        f"JSON  {json_path}"
    )


if __name__ == "__main__":
    main()

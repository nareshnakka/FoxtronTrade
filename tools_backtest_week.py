"""Causal backtest for last N calendar days (FastTrack algo profile)."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
    write_detailed_html,
)
from backtest_month_ledger import OUT, LedgerState, _inr, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory

ALGO_FASTTRACK = "FastTrack"
ALGO_BALANCED = "Balanced"
ALGO_NAME = ALGO_FASTTRACK  # default; overridden by --algo
IST = ZoneInfo("Asia/Kolkata")


def aggressive_overrides(capital: float, deploy: float) -> dict:
    # Retrain 2026-11-02 from Oct 6/9/14/23 loss autopsy (last-30d @ 1L):
    # - deny CE multi-day crush (Oct 23 open knife; half-day crush kept)
    # - orb_max 110 (Oct 9 OR 126 already spent)
    # - block CE breakout extended ≥25pts beyond OR → wait S/R (Oct 6)
    # - SR reclaim max_under 12 (Oct 14 scratch under support)
    return {
        "profile_name": ALGO_FASTTRACK,
        "algo_name": ALGO_FASTTRACK,
        "hedge_mode": False,
        "directional_min_confidence": 0.70,
        "directional_min_margin": 0.20,
        "max_trades_per_day": 2,
        "bank_after_win": True,
        "entry_cooldown_minutes": 35,
        "post_decay_respect_session_delay": False,
        "post_decay_min_bar": 0,
        "post_decay_confirm_minutes": 2,
        "post_decay_expiry_confirm_minutes": 3,
        "post_decay_expiry_min_est_pct": 300.0,
        # Expiry gamma playbook (29 Tue autopsy): wait decay → impulse
        "expiry_min_entry_minutes": 120,
        "expiry_no_entry_after_minutes": 195,
        "expiry_min_mom": 25.0,
        "expiry_time_stop_minutes": 20,
        "post_decay_fast_confirm_est_pct": 400.0,
        "post_decay_min_confidence": 0.62,
        # Lesson 2026-10-26: weak-est partial reclaim knife
        "post_decay_strong_est_pct": 200.0,
        "post_decay_ce_max_under_open": 150.0,
        "post_decay_weak_est_confirm_minutes": 5,
        "post_decay_require_spot_align": True,
        "post_decay_spot_align_pts": 35.0,
        # Train RANGE-BOUND + SHARP-FALL (primary)
        "block_structure_in_chop": True,
        "allow_pe_breakdown_in_chop": True,
        # Flat-day PE scalp (learned 2026-11-03): unlock chop on soft path only
        "enable_daily_soft_scalp": True,
        "daily_soft_pe_min_vs_open": 40,
        "daily_soft_max_lots": 8,
        "daily_soft_target_pct_low": 5.0,
        "daily_soft_stop_loss_pct": 2.5,
        "daily_soft_only_if_flat": True,
        "daily_soft_entry_after_minutes": 150,
        "daily_soft_no_entry_after_minutes": 225,
        # 10–15pt pullback-resume scalp before soft PE window
        "enable_micro_scalp": True,
        "micro_scalp_spot_target_pts": 12,
        "micro_scalp_min_drive_pts": 55,
        "micro_scalp_entry_after_minutes": 55,
        "micro_scalp_no_entry_after_minutes": 150,
        "micro_scalp_max_lots": 6,
        "micro_scalp_only_if_flat": True,
        "micro_scalp_both_sides": False,
        "micro_scalp_min_vs_open": 40,
        "micro_scalp_max_giveback_from_extreme": 70,
        # Daily PA/momentum/S-R — participate most flat sessions
        "enable_daily_pa": True,
        "daily_pa_min_vs_open": 20,
        "daily_pa_min_mom_pts": 6,
        "daily_pa_entry_after_minutes": 45,
        "daily_pa_no_entry_after_minutes": 270,
        "daily_pa_max_lots": 5,
        "daily_pa_spot_target_pts": 12,
        "daily_pa_allow_mom_break": False,
        "daily_pa_only_if_flat": True,
        # From 30d + 3m PA loss autopsy — gates that lift 90d AND full
        "daily_pa_pe_min_vs_open": 35,
        "daily_pa_ce_min_vs_open": 28,
        "daily_pa_pe_entry_after_minutes": 75,
        "daily_pa_ce_max_giveback": 55,
        "daily_pa_min_path_efficiency": 0.0,
        "daily_pa_max_orb_width_for_pb": 999,
        "daily_pa_pb_min_giveback": 0,
        "daily_pa_pb_max_giveback": 999,
        "daily_pa_pe_min_giveback": 0,
        "block_ce_on_sharp_fall": True,
        "sharp_fall_day_ext_pts": 100.0,
        "ce_fail_rally_giveback_pts": 80.0,
        "block_ce_low_efficiency": True,
        "ce_min_path_efficiency": 0.35,
        "ce_day_min_efficiency": 0.25,
        # Train Feb/May/Sep 2026 — crash SR / PE snapback / late soft PE
        # Train May/Jun 2026 — PE spent dump; soft CE SR; CE breakout mom
        # Profit-learn 1yr flat+loss autopsy (2026-11-03):
        # cut under-support / late soft CE SR; weak CE breakouts; PE 2m snap.
        # max_under stays 12 — tighter 3 flickered off Oct28/Nov2 green SR.
        # Soft-session reclaim must be at/above support (see expert_filters).
        "sr_reclaim_min_mom10": 0.0,
        "sr_soft_no_entry_after_minutes": 200,
        "sr_late_min_vs_open": 45.0,
        "sr_late_hard_after_minutes": 240,
        "sr_late_min_above_support": 15.0,
        "sr_late_min_mom10": 20.0,
        "sr_require_above_support_if_soft": True,
        "sr_max_giveback_pts": 100.0,
        "sr_soft_vs_open": 25.0,
        "sr_soft_min_mom10": 18.0,
        "pe_snapback_lookback": 3,
        "pe_snapback_bounce_pts": 10.0,
        "ce_breakout_min_mom": 18.0,
        "sr_hard_min_mom10": 15.0,
        "sr_spent_or_range_pts": 100.0,
        "sr_spent_or_until_minutes": 150,
        "sr_spent_or_min_mom10": 25.0,
        "sr_fast_persist_min_vs": 50.0,
        "sr_fast_persist_min_mom": 18.0,
        "post_break_confirm_minutes": 1,
        "pe_soft_no_entry_after_minutes": 180,
        "pe_spent_day_ext_pts": 140.0,
        "pe_max_off_day_low_pts": 22.0,
        "relax_extreme_pe_on_hard_break": False,
        "block_ce_breakout_falling_mom": True,
        "pe_breakdown_min_mom": 32.0,
        "prefer_sr_dip_entry": True,
        "enable_sr_reclaim": True,
        "enable_sr_reject": False,
        "enable_orb_breakout": True,
        "enable_orb_breakdown": True,
        "post_decay_allow_setups": ["ce_half_day_crush"],
        "post_decay_deny_setups": [
            "three_day_fall",
            "pe_multi_day_crush",
            "ce_multi_day_crush",
        ],
        "block_orb_chase_without_retest": True,
        "block_orb_after_failed_break": True,
        "orb_retest_proximity_pts": 8.0,
        "orb_chase_min_extension": 12.0,
        "orb_min_range_pts": 42,
        "orb_max_range_pts": 110,
        "breakout_max_extension_pts": 25.0,
        "block_ce_mid_chase": True,
        "ce_min_vs_open": 65,
        "pe_min_vs_open": 55,
        "ce_min_vs_open_break": 85,
        "pe_min_vs_open_break": 60,
        "soft_vs_open_on_strong_break": False,
        "block_late_sr_in_range": True,
        "sr_range_no_entry_after_minutes": 180,
        "sr_range_min_day_efficiency": 0.35,
        "relax_extreme_on_hard_break": True,
        "allow_orb_rebreak_hard_trend": False,
        "pe_rebreak_min_mom": 30.0,
        "signal_persist_minutes": 2,
        "sr_signal_persist_minutes": 1,
        "sr_signal_persist_minutes": 1,
        "sr_dip_min_extent": 50.0,
        "sr_dip_pullback_min": 25.0,
        "sr_dip_proximity_pts": 22.0,
        "sr_dip_turn_bars": 4,
        "sr_reclaim_max_under_support": 12.0,
        "sr_no_entry_after_minutes": 300,
        "sr_ce_min_vs_open": 0.0,
        "stop_loss_pct": 5.0,
        "target_pct_low": 5.0,
        "target_pct_high": 50.0,
        "max_lots_per_trade": 20,
        "min_lots_per_trade": 1,
        "deploy_pct_of_balance": float(deploy),
        "capital_inr": float(capital),
        "max_daily_loss_pct": 2.5,
        "max_weekly_loss_pct": 0,
        "max_monthly_loss_pct": 0,
        "max_losses_before_halt": 1,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
        "post_decay_prefer_over_mkt": True,
        "post_decay_entry_enabled": True,
        "post_decay_standalone_entry": True,
        "session_start": "09:15",
        "session_end": "15:15",
        # 300 ≈ 14:15 — keeps Jul24 13:51 SR, blocks Nov2 14:45
        "no_entry_after_minutes": 300,
        "sr_no_entry_after_minutes": 300,
    }


def balanced_overrides(capital: float, deploy: float) -> dict:
    """FastTrack rules + 70% analyzed / 30% opposite ATM hedge."""
    from foxtrade.balanced import balanced_cfg_overlay

    cfg = aggressive_overrides(capital, deploy)
    cfg.update(balanced_cfg_overlay())
    return cfg


def profile_overrides(algo: str, capital: float, deploy: float) -> dict:
    name = (algo or ALGO_FASTTRACK).strip().lower()
    if name in ("balanced", "bal", "hedge"):
        return balanced_overrides(capital, deploy)
    return aggressive_overrides(capital, deploy)


def _feed_decay_day(
    decay_mem: DecayMemory,
    bars: list,
    day: str,
    ce_open: float | None,
    pe_open: float | None,
    cfg: dict,
) -> None:
    """Walk bars into DecayMemory only (no trades) so expiry-only runs stay causal."""
    if not bars or decay_mem is None:
        return
    delta = float(cfg.get("synth_delta", 0.50))
    theta = float(cfg.get("synth_theta_per_min", 0.015))
    ce0 = float(ce_open or 135.0)
    pe0 = float(pe_open or 135.0)
    spot0 = float(bars[0]["close"])
    for i, bar in enumerate(bars):
        spot = float(bar["close"])
        ds = spot - spot0
        ce_px = max(5.0, ce0 + delta * ds - theta * float(i))
        pe_px = max(5.0, pe0 - delta * ds - theta * float(i))
        decay_mem.update_live(
            ce=ce_px, pe=pe_px, spot=spot, day=day, source="backtest_feed"
        )
    decay_mem.finalize_day(day)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="calendar days lookback")
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=0.75)
    ap.add_argument(
        "--as-of",
        default=datetime.now(IST).date().isoformat(),
        help="end date YYYY-MM-DD",
    )
    ap.add_argument(
        "--expiry-only",
        action="store_true",
        help="trade Tuesday expiry sessions only (still feed decay on other days)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="use every session on disk from meta range_start through --as-of",
    )
    ap.add_argument(
        "--algo",
        default=ALGO_FASTTRACK,
        choices=[ALGO_FASTTRACK, ALGO_BALANCED, "fasttrack", "balanced"],
        help="FastTrack (directional) or Balanced (70/30 hedge)",
    )
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)
    global ALGO_NAME
    ALGO_NAME = (
        ALGO_BALANCED
        if str(args.algo).lower().startswith("bal")
        else ALGO_FASTTRACK
    )

    days = _load_month_days(
        as_of=as_of,
        calendar_days=int(args.days),
        all_available=bool(args.all),
    )
    if not days:
        print("No session days in range")
        return 1

    trade_days = (
        [d for d in days if d.get("is_expiry_day")] if args.expiry_only else days
    )
    if args.expiry_only and not trade_days:
        print("No expiry sessions in range")
        return 1

    cfg = month_cfg()
    cfg.update(profile_overrides(ALGO_NAME, args.capital, args.deploy))
    # Year profit-max overrides (if trained)
    _nifty_ov = Path("data/nifty/fasttrack_profit_overrides.json")
    if _nifty_ov.exists() and ALGO_NAME == ALGO_FASTTRACK:
        cfg.update(json.loads(_nifty_ov.read_text(encoding="utf-8")))
        cfg["capital_inr"] = float(args.capital)
        cfg["deploy_pct_of_balance"] = float(args.deploy)
    state = LedgerState(
        initial=float(args.capital),
        balance=float(args.capital),
        deploy_pct=float(args.deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(days[0]["date"])
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    mode = "EXPIRY-ONLY" if args.expiry_only else "all sessions"
    print(
        f"{ALGO_NAME} {days[0]['date']}→{days[-1]['date']} · "
        f"{len(trade_days)} traded / {len(days)} calendar · "
        f"₹{args.capital:,.0f} · deploy {args.deploy:.0%} · {mode}"
    )
    print(f"  post-decay seeded {len(seed)} prior sessions", flush=True)

    daily: list[dict] = []
    trade_n = 0
    for n, d in enumerate(days, 1):
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        is_exp = bool(d.get("is_expiry_day"))
        if args.expiry_only and not is_exp:
            _feed_decay_day(decay_mem, bars, d["date"], ce0, pe0, cfg)
            continue

        trade_n += 1
        print(
            f"  [{trade_n}/{len(trade_days)}] {d['date']}"
            f"{' EXP' if is_exp else ''} bars={len(bars)} "
            f"bal=₹{state.balance:,.0f}",
            flush=True,
        )
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=is_exp,
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
        )
        decay_mem.finalize_day(d["date"])
        daily.append(row)
        tone = "+" if float(row.get("net") or 0) > 0 else ("-" if float(row.get("net") or 0) < 0 else "=")
        print(
            f"      {tone} net={_inr(row.get('net'))} setups={row.get('setups')} "
            f"W/L {row.get('wins')}/{row.get('losses')} skip={row.get('skip') or '-'}",
            flush=True,
        )

    ledger = list(state.ledger)
    tot_gross = sum(float(r.get("gross") or 0) for r in daily)
    tot_chg = sum(float(r.get("charges") or 0) for r in daily)
    tot_net = sum(float(r.get("net") or 0) for r in daily)
    tot_invested = sum(float(t.get("invested") or 0) for t in ledger)
    post_n = sum(int(r.get("post_decay_entries") or 0) for r in daily)

    rep = {
        "title": (
            f"{ALGO_NAME} · "
            f"{'EXPIRY-ONLY' if args.expiry_only else f'Last {args.days}d'} "
            f"causal · {days[0]['date']}→{days[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "mode": (
            "causal_expiry_tuesdays_only"
            if args.expiry_only
            else "causal_minute_live_algo_gates_post_decay"
        ),
        "expiry_only": bool(args.expiry_only),
        "premium_model": (
            "Synthetic ATM premium updated EVERY minute from spot Δ "
            "(delta≈0.5 + theta). analyze_market + post-decay memory + "
            "manage_percent_exit run each bar. Impulse/pre-event OFF."
        ),
        "profile": ALGO_NAME,
        "algo_name": ALGO_NAME,
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": float(args.capital),
            "deploy_pct": float(args.deploy),
            "deploy_cap": state.deploy_cap,
            "halt_pct": getattr(state, "halt_pct", 0.0),
            "halt_floor": getattr(state, "halt_floor", 0.0),
            "final_balance": round(state.balance, 2),
            "net_pnl": round(state.balance - float(args.capital), 2),
            "return_pct": round(
                100.0 * (state.balance / float(args.capital) - 1.0), 2
            ),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "totals": {
            "session_days": len(daily),
            "traded_days": sum(1 for r in daily if int(r.get("setups") or 0) > 0),
            "setups": sum(int(r.get("setups") or 0) for r in daily),
            "post_decay_entries": post_n,
            "lots": sum(int(r.get("lots_total") or 0) for r in daily),
            "invested_total": round(tot_invested, 2),
            "wins": sum(int(r.get("wins") or 0) for r in daily),
            "losses": sum(int(r.get("losses") or 0) for r in daily),
            "gross": round(tot_gross, 2),
            "charges": round(tot_chg, 2),
            "net": round(tot_net, 2),
            "green_days": sum(1 for r in daily if float(r.get("net") or 0) > 0),
            "red_days": sum(1 for r in daily if float(r.get("net") or 0) < 0),
            "minutes_processed": sum(int(r.get("minutes") or 0) for r in daily),
            "analyses": sum(int(r.get("analyses") or 0) for r in daily),
            "signal_minutes": sum(int(r.get("allow_true") or 0) for r in daily),
        },
        "config_snapshot": {
            "max_trades_per_day": cfg["max_trades_per_day"],
            "min_lots_per_trade": cfg["min_lots_per_trade"],
            "max_lots_per_trade": cfg["max_lots_per_trade"],
            "stop_loss_pct": cfg["stop_loss_pct"],
            "target_pct_low": cfg["target_pct_low"],
            "target_pct_high": cfg["target_pct_high"],
            "directional_min_confidence": cfg["directional_min_confidence"],
            "deploy_pct_of_balance": cfg.get("deploy_pct_of_balance"),
            "post_decay_entry_enabled": True,
            "impulse_entry_enabled": False,
            "pre_event_entry_enabled": False,
            "profile_name": ALGO_NAME,
            "algo_name": ALGO_NAME,
            "hedge_mode": bool(cfg.get("hedge_mode")),
            "hedge_primary_pct": cfg.get("hedge_primary_pct"),
            "hedge_other_pct": cfg.get("hedge_other_pct"),
            "session": f"{cfg.get('session_start')}–{cfg.get('session_end')}",
            "lot_size": cfg.get("lot_size"),
        },
        "daily": daily,
        "ledger": ledger,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"week_{days[0]['date']}_to_{days[-1]['date']}"
    if args.expiry_only:
        tag = f"expiry_{days[0]['date']}_to_{days[-1]['date']}"
    algo_slug = ALGO_NAME.lower()
    jp = OUT / f"{tag}_{algo_slug}_report.json"
    hp = OUT / f"{tag}_{algo_slug}_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)

    cap, tot = rep["capital"], rep["totals"]
    print()
    print(f"Sessions {tot['session_days']}  traded {tot['traded_days']}")
    print(
        f"Setups {tot['setups']}  W/L {tot['wins']}/{tot['losses']}  "
        f"G/R days {tot['green_days']}/{tot['red_days']}  "
        f"post-decay entries {tot['post_decay_entries']}"
    )
    print(
        f"Net {_inr(cap['net_pnl'])} ({cap['return_pct']:+.1f}%)  "
        f"Final Rs {cap['final_balance']:,.0f}"
    )
    for r in daily:
        print(
            f"  {r['date']}  {_inr(r.get('net')):>10}  "
            f"setups={r.get('setups')}  bal=Rs{float(r.get('balance_close') or 0):,.0f}"
        )
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

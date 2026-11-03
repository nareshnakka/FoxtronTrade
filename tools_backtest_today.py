"""Backtest a single session day with the FastTrack algo profile."""
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
from backtest_month_ledger import OUT, LedgerState, _inr, month_cfg
from foxtrade.decay_memory import DecayMemory
from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")
MINUTES = Path("data/expiries/last_3_months/minutes")


def fetch_day(day: str) -> Path:
    y, m, d = map(int, day.split("-"))
    start = datetime(y, m, d, 9, 15, tzinfo=IST)
    end = datetime(y, m, d, 15, 30, tzinfo=IST)
    kite = get_authenticated_kite()
    rows = kite.historical_data(256265, start, end, "minute") or []
    out = []
    for r in rows:
        dt = r["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        else:
            dt = dt.replace(tzinfo=IST)
        out.append(
            {
                "ts": dt.isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r.get("volume") or 0),
            }
        )
    path = MINUTES / f"nifty_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Fetched {len(out)} bars → {path}")
    return path


def aggressive_overrides(capital: float, deploy: float) -> dict:
    # Reuse trained winners_only / walk-forward profile from week backtest.
    from tools_backtest_week import aggressive_overrides as week_overrides

    return week_overrides(capital, deploy)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=0.75)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()
    day = args.date
    path = MINUTES / f"nifty_{day}.json"

    if args.refetch or not path.exists() or path.stat().st_size < 50:
        fetch_day(day)
    if not path.exists() or path.stat().st_size < 50:
        print(f"No minute data for {day}")
        return 1

    bars = _load_bars(path)
    day_d = date.fromisoformat(day)
    # Nifty weekly expiry = Tuesday
    is_expiry = day_d.weekday() == 1
    print(
        f"Backtest {day} · {len(bars)} bars · ₹{args.capital:,.0f} · "
        f"deploy {args.deploy:.0%} · FastTrack"
        + (" · EXPIRY" if is_expiry else "")
    )

    cfg = month_cfg()
    cfg.update(aggressive_overrides(args.capital, args.deploy))

    state = LedgerState(
        initial=float(args.capital),
        balance=float(args.capital),
        deploy_pct=float(args.deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(day)
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    print(f"  post-decay memory seeded with {len(seed)} prior sessions", flush=True)
    ce0, pe0 = _day_prem_opens(day)

    row = simulate_day_detailed(
        bars,
        cfg,
        state,
        day=day,
        is_expiry_day=is_expiry,
        decay_mem=decay_mem,
        ce_open=ce0,
        pe_open=pe0,
    )
    decay_mem.finalize_day(day)

    ledger = list(state.ledger)
    tot_invested = sum(float(t.get("invested") or 0) for t in ledger)
    tot_net = float(row.get("net") or 0)

    rep = {
        "title": f"Today causal backtest · {day}",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_minute_live_algo_gates_post_decay",
        "premium_model": (
            "Synthetic ATM premium updated EVERY minute from spot Δ "
            "(delta≈0.5 + theta). analyze_market + post-decay memory + "
            "manage_percent_exit run each bar."
        ),
        "profile": "FastTrack",
        "algo_name": "FastTrack",
        "range": {"start": day, "end": day},
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
            "session_days": 1,
            "traded_days": 1 if int(row.get("setups") or 0) > 0 else 0,
            "setups": int(row.get("setups") or 0),
            "post_decay_entries": int(row.get("post_decay_entries") or 0),
            "lots": int(row.get("lots_total") or 0),
            "invested_total": round(tot_invested, 2),
            "wins": int(row.get("wins") or 0),
            "losses": int(row.get("losses") or 0),
            "gross": round(float(row.get("gross") or 0), 2),
            "charges": round(float(row.get("charges") or 0), 2),
            "net": round(tot_net, 2),
            "green_days": 1 if tot_net > 0 else 0,
            "red_days": 1 if tot_net < 0 else 0,
            "minutes_processed": int(row.get("minutes") or len(bars)),
            "analyses": int(row.get("analyses") or 0),
            "signal_minutes": int(row.get("allow_true") or 0),
        },
        "config_snapshot": {
            "max_trades_per_day": cfg["max_trades_per_day"],
            "min_lots_per_trade": cfg["min_lots_per_trade"],
            "max_lots_per_trade": cfg["max_lots_per_trade"],
            "entry_cooldown_minutes": cfg.get("entry_cooldown_minutes"),
            "stop_loss_pct": cfg["stop_loss_pct"],
            "target_pct_low": cfg["target_pct_low"],
            "target_pct_high": cfg["target_pct_high"],
            "entry_style": cfg.get("entry_style"),
            "orb_minutes": cfg.get("orb_minutes"),
            "directional_min_confidence": cfg["directional_min_confidence"],
            "min_vs_open_entry": cfg.get("min_vs_open_entry"),
            "session": f"{cfg.get('session_start')}–{cfg.get('session_end')}",
            "lot_size": cfg.get("lot_size"),
            "risk_per_trade_pct": cfg.get("risk_per_trade_pct"),
            "post_decay_entry_enabled": cfg.get("post_decay_entry_enabled", True),
            "profile_name": "FastTrack",
            "algo_name": "FastTrack",
            "deploy_pct_of_balance": cfg.get("deploy_pct_of_balance"),
        },
        "daily": [row],
        "ledger": ledger,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"today_{day}"
    jp = OUT / f"{tag}_algo_report.json"
    hp = OUT / f"{tag}_algo_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)

    cap, tot = rep["capital"], rep["totals"]
    print(f"Range {day}")
    print(
        f"Setups {tot['setups']}  W/L {tot['wins']}/{tot['losses']}  "
        f"G/R {tot['green_days']}/{tot['red_days']}"
    )
    print(
        f"Net {_inr(cap['net_pnl'])} ({cap['return_pct']:+.1f}%)  "
        f"Final Rs {cap['final_balance']:,.0f}"
    )
    for t in ledger:
        print(
            f"  {t.get('entry_time')}->{t.get('exit_time')} {t.get('side')} "
            f"lots={t.get('lots')} invested=Rs{float(t.get('invested') or 0):,.0f} "
            f"net={_inr(t.get('net'))} · {t.get('reason')}"
        )
    skip = row.get("skip") or ""
    print(
        f"Day {day} net={_inr(row.get('net'))} setups={row.get('setups')} "
        f"skip={skip}"
    )
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

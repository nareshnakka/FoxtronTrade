"""
Day-by-day causal algo replay with a live pause between sessions.

Example (last ~2 months, 5s between days):
  .venv\\Scripts\\python.exe tools_replay_days.py --days 62 --sleep 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
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
from foxtrade.fii_oi_scanner import _actions_for, classify_oi_price
from tools_backtest_week import aggressive_overrides

IST = ZoneInfo("Asia/Kolkata")
OI_DIR = Path("data/expiries/last_3_months/oi_minutes")


def _eod_oi(day: str, cfg: dict) -> dict | None:
    p = OI_DIR / f"nifty_fut_{day}.json"
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(rows, list) or len(rows) < 30:
        return None
    open_px = float(rows[0].get("open") or rows[0].get("close") or 0)
    open_oi = int(rows[0].get("oi") or 0)
    eod = rows[-1]
    eod_px = float(eod.get("close") or 0)
    eod_oi = int(eod.get("oi") or 0)
    if open_px <= 0 or open_oi <= 0:
        return None
    reg = classify_oi_price(
        price_chg=eod_px - open_px,
        oi_chg=eod_oi - open_oi,
        price_eps=float(cfg.get("fii_oi_price_eps", 20)),
        oi_eps=float(cfg.get("fii_oi_chg_eps", 5000)),
    )
    acts = _actions_for(reg, fii_net=None, fii_net_chg=None, cfg=cfg)
    return {
        "regime": reg,
        "dpx": round(eod_px - open_px, 1),
        "doi": eod_oi - open_oi,
        "prefer_ce": acts.get("prefer_ce"),
        "prefer_pe": acts.get("prefer_pe"),
        "block_ce": acts.get("block_ce"),
        "caution_ce": acts.get("caution_ce"),
    }


def _trade_line(t: dict) -> str:
    side = t.get("side") or "?"
    ev = t.get("event") or t.get("reason") or ""
    et = t.get("entry_time") or "?"
    xt = t.get("exit_time") or ""
    net = float(t.get("net") or 0)
    lots = int(t.get("lots") or 0)
    return (
        f"      {et}→{xt}  {side} x{lots}  {_inr(net):>10}  {ev}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Day-by-day algo replay")
    ap.add_argument("--days", type=int, default=62, help="calendar window (~2 months)")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between days")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--deploy", type=float, default=1.0)
    ap.add_argument("--as-of", type=str, default="")
    ap.add_argument("--no-oi", action="store_true")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date(2026, 11, 2)
    days = _load_month_days(
        as_of=as_of, calendar_days=int(args.days), all_available=False
    )
    if not days:
        print("No session days in range")
        return 1

    cfg = month_cfg()
    cfg.update(aggressive_overrides(args.capital, args.deploy))
    state = LedgerState(
        initial=float(args.capital),
        balance=float(args.capital),
        deploy_pct=float(args.deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(days[0]["date"])
    decay_mem = DecayMemory(persist=False, seed_days=seed)

    print("=" * 72)
    print(
        f"DAY-BY-DAY REPLAY  {days[0]['date']} → {days[-1]['date']}  "
        f"({len(days)} sessions)"
    )
    print(
        f"Capital {_inr(args.capital)}  deploy {args.deploy:.0%}  "
        f"pause {args.sleep:.0f}s/day  post-decay seed {len(seed)}"
    )
    print("=" * 72, flush=True)

    daily: list[dict] = []
    oi_regimes: Counter[str] = Counter()
    t0 = time.time()

    for n, d in enumerate(days, 1):
        day = d["date"]
        is_exp = bool(d.get("is_expiry_day"))
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(day)
        bal_open = state.balance

        print(flush=True)
        print(
            f"▶ [{n}/{len(days)}] {day}"
            f"{'  EXPIRY' if is_exp else ''}"
            f"  bars={len(bars)}  open_bal={_inr(bal_open)}",
            flush=True,
        )

        oi = None if args.no_oi else _eod_oi(day, cfg)
        if oi:
            oi_regimes[str(oi["regime"])] += 1
            flag = ""
            if oi.get("block_ce"):
                flag = " BLOCK_CE"
            elif oi.get("prefer_ce"):
                flag = " prefer_CE"
            elif oi.get("prefer_pe"):
                flag = " prefer_PE"
            elif oi.get("caution_ce"):
                flag = " caution_CE"
            print(
                f"   OI EOD  dpx={oi['dpx']:+.1f}  doi={oi['doi']:+d}  "
                f"{oi['regime']}{flag}",
                flush=True,
            )
        elif not args.no_oi:
            print("   OI EOD  (no fut minutes)", flush=True)

        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=day,
            is_expiry_day=is_exp,
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
            verbose=False,
        )
        decay_mem.finalize_day(day)
        if oi:
            row["oi_eod"] = oi
        daily.append(row)

        net = float(row.get("net") or 0)
        tone = "GREEN" if net > 0 else ("RED" if net < 0 else "FLAT")
        print(
            f"   {tone}  day={_inr(net)}  "
            f"W/L {row.get('wins')}/{row.get('losses')}  "
            f"setups={row.get('setups')}  "
            f"skip={row.get('skip') or '-'}  "
            f"close_bal={_inr(state.balance)}  "
            f"cum={_inr(state.balance - args.capital)}",
            flush=True,
        )
        for t in row.get("trades") or []:
            print(_trade_line(t), flush=True)
        if not (row.get("trades") or []):
            print("      (no trades)", flush=True)

        # Live pause so each day reads as a step (skip after last day)
        if n < len(days) and args.sleep > 0:
            print(f"   … next day in {args.sleep:.0f}s", flush=True)
            time.sleep(float(args.sleep))

    elapsed = time.time() - t0
    ledger = list(state.ledger)
    tot_net = sum(float(r.get("net") or 0) for r in daily)
    tot_gross = sum(float(r.get("gross") or 0) for r in daily)
    tot_chg = sum(float(r.get("charges") or 0) for r in daily)
    tot_w = sum(int(r.get("wins") or 0) for r in daily)
    tot_l = sum(int(r.get("losses") or 0) for r in daily)
    green = sum(1 for r in daily if float(r.get("net") or 0) > 0)
    red = sum(1 for r in daily if float(r.get("net") or 0) < 0)
    traded = sum(1 for r in daily if int(r.get("setups") or 0) > 0)
    post_n = sum(int(r.get("post_decay_entries") or 0) for r in daily)
    tot_invested = sum(float(t.get("invested") or 0) for t in ledger)

    print(flush=True)
    print("=" * 72)
    print("REPLAY COMPLETE")
    print(
        f"Sessions {len(daily)}  traded {traded}  "
        f"W/L {tot_w}/{tot_l}  G/R days {green}/{red}  "
        f"post-decay {post_n}"
    )
    if oi_regimes:
        print(f"OI EOD counts: {dict(oi_regimes)}")
    print(
        f"Net {_inr(tot_net)}  ({100.0 * (state.balance / args.capital - 1):+.1f}%)  "
        f"Final {_inr(state.balance)}  elapsed {elapsed:.0f}s"
    )
    print("-" * 72)
    print("Day ledger:")
    for r in daily:
        net = float(r.get("net") or 0)
        mark = "+" if net > 0 else ("-" if net < 0 else " ")
        oi = r.get("oi_eod") or {}
        oi_tag = f"  oi={oi.get('regime')}" if oi else ""
        print(
            f"  {mark} {r['date']}  {_inr(net):>10}  "
            f"W/L {r.get('wins')}/{r.get('losses')}  "
            f"bal={_inr(r.get('balance_close'))}{oi_tag}"
        )

    rep = {
        "title": f"Day-by-day replay {days[0]['date']}→{days[-1]['date']}",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_day_by_day_replay",
        "sleep_sec": float(args.sleep),
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": float(args.capital),
            "deploy_pct": float(args.deploy),
            "deploy_cap": state.deploy_cap,
            "halt_pct": getattr(state, "halt_pct", 0.30),
            "halt_floor": getattr(state, "halt_floor", float(args.capital) * 0.30),
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
            "traded_days": traded,
            "setups": sum(int(r.get("setups") or 0) for r in daily),
            "lots": sum(int(r.get("lots_total") or 0) for r in daily),
            "invested_total": round(tot_invested, 2),
            "wins": tot_w,
            "losses": tot_l,
            "gross": round(tot_gross, 2),
            "charges": round(tot_chg, 2),
            "green_days": green,
            "red_days": red,
            "post_decay_entries": post_n,
            "net": round(tot_net, 2),
            "minutes_processed": sum(int(r.get("minutes") or 0) for r in daily),
            "analyses": sum(int(r.get("analyses") or 0) for r in daily),
            "signal_minutes": sum(int(r.get("allow_true") or 0) for r in daily),
            "oi_eod_counts": dict(oi_regimes),
            "elapsed_sec": round(elapsed, 1),
        },
        "config_snapshot": {
            "max_trades_per_day": cfg.get("max_trades_per_day"),
            "min_lots_per_trade": cfg.get("min_lots_per_trade"),
            "max_lots_per_trade": cfg.get("max_lots_per_trade"),
            "stop_loss_pct": cfg.get("stop_loss_pct"),
            "target_pct_low": cfg.get("target_pct_low"),
            "target_pct_high": cfg.get("target_pct_high"),
            "directional_min_confidence": cfg.get("directional_min_confidence"),
            "deploy_pct_of_balance": cfg.get("deploy_pct_of_balance"),
            "post_decay_entry_enabled": True,
            "profile_name": "paper_10l_aggressive_100",
            "session": f"{cfg.get('session_start')}–{cfg.get('session_end')}",
            "lot_size": cfg.get("lot_size"),
        },
        "daily": daily,
        "ledger": ledger,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"replay_{days[0]['date']}_to_{days[-1]['date']}"
    jp = OUT / f"{tag}.json"
    hp = OUT / f"{tag}.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    try:
        write_detailed_html(rep, hp)
        print(flush=True)
        print(f"JSON {jp}")
        print(f"HTML {hp}")
    except Exception as exc:  # noqa: BLE001
        print(flush=True)
        print(f"JSON {jp}")
        print(f"HTML skipped: {exc}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        raise SystemExit(130)

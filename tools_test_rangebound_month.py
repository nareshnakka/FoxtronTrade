"""Find range-bound days in last ~1 month and causal-backtest each."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
    write_detailed_html,
)
from backtest_month_ledger import OUT, LedgerState, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides

MINUTES = Path("data/expiries/last_3_months/minutes")


def session_days(end: date, calendar_days: int = 31) -> list[str]:
    start = end - timedelta(days=calendar_days - 1)
    out = []
    for f in sorted(MINUTES.glob("nifty_*.json")):
        try:
            d = date.fromisoformat(f.stem.replace("nifty_", ""))
        except ValueError:
            continue
        if start <= d <= end and f.stat().st_size > 100:
            out.append(d.isoformat())
    return out


def day_metrics(day: str) -> dict:
    bars = _load_bars(MINUTES / f"nifty_{day}.json")
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    o, c = closes[0], closes[-1]
    hi, lo = max(highs), min(lows)
    rng = hi - lo
    chg = c - o
    # path efficiency: net move / total range
    eff = abs(chg) / max(rng, 1.0)
    # whipsaw flips on close path (sign changes of 1-bar delta)
    flips = 0
    for i in range(2, len(closes)):
        a = closes[i] - closes[i - 1]
        b = closes[i - 1] - closes[i - 2]
        if a * b < 0:
            flips += 1
    return {
        "date": day,
        "spot_open": round(o, 2),
        "spot_close": round(c, 2),
        "spot_chg": round(chg, 2),
        "day_range": round(rng, 2),
        "efficiency": round(eff, 3),
        "flips": flips,
        "bars": len(bars),
    }


def is_range_bound(m: dict) -> bool:
    """
    Range-bound / chop day (practical Nifty session):
      - |close-open| < 60  (net flat regardless of intraday wiggle)
      - OR |close-open| < 100 AND efficiency < 0.40 (moved little vs day's span)
      - OR day_range < 140 AND efficiency < 0.45
      - OR efficiency < 0.22 (pure grind / mean-revert)
    """
    chg = abs(float(m["spot_chg"]))
    rng = float(m["day_range"])
    eff = float(m["efficiency"])
    if chg < 60:
        return True
    if chg < 100 and eff < 0.40:
        return True
    if rng < 140 and eff < 0.45:
        return True
    if eff < 0.22:
        return True
    return False


def backtest_day(day: str, capital: float, deploy: float) -> dict:
    path = MINUTES / f"nifty_{day}.json"
    bars = _load_bars(path)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(capital, deploy))
    state = LedgerState(
        initial=float(capital),
        balance=float(capital),
        deploy_pct=float(deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(day)
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    ce0, pe0 = _day_prem_opens(day)
    row = simulate_day_detailed(
        bars,
        cfg,
        state,
        day=day,
        is_expiry_day=False,
        decay_mem=decay_mem,
        ce_open=ce0,
        pe_open=pe0,
        verbose=False,
    )
    decay_mem.finalize_day(day)
    trades = row.get("trades") or []
    return {
        "date": day,
        "spot_chg": row.get("spot_chg"),
        "setups": row.get("setups"),
        "wins": row.get("wins"),
        "losses": row.get("losses"),
        "net": round(float(row.get("net") or 0), 2),
        "gross": round(float(row.get("gross") or 0), 2),
        "charges": round(float(row.get("charges") or 0), 2),
        "events": [t.get("event") for t in trades],
        "sides": [t.get("side") for t in trades],
        "trade_nets": [round(float(t.get("net") or 0), 2) for t in trades],
        "balance_close": round(state.balance, 2),
        "row": row,
        "ledger": list(state.ledger),
    }


def main() -> int:
    capital = 500_000.0
    deploy = 0.75
    end = date(2026, 11, 2)
    days = session_days(end, 31)
    print(f"Last ~1m sessions: {len(days)} · {days[0]}→{days[-1]}")

    metrics = [day_metrics(d) for d in days]
    rb = [m for m in metrics if is_range_bound(m)]
    trend = [m for m in metrics if not is_range_bound(m)]

    print(f"\nRange-bound days: {len(rb)} / {len(metrics)}")
    print(f"{'date':12s} {'chg':>7s} {'range':>7s} {'eff':>6s} {'flips':>5s}")
    for m in rb:
        print(
            f"{m['date']:12s} {m['spot_chg']:+7.1f} {m['day_range']:7.1f} "
            f"{m['efficiency']:6.2f} {m['flips']:5d}"
        )

    print(f"\n===== Backtest range-bound @ ₹{capital:,.0f} =====")
    results = []
    for m in rb:
        r = backtest_day(m["date"], capital, deploy)
        r["day_range"] = m["day_range"]
        r["efficiency"] = m["efficiency"]
        results.append(r)
        tone = "+" if r["net"] > 0 else ("-" if r["net"] < 0 else "=")
        print(
            f"  {tone} {r['date']} chg={float(r['spot_chg'] or 0):+.0f} "
            f"rng={m['day_range']:.0f} setups={r['setups']} "
            f"W/L {r['wins']}/{r['losses']} net={r['net']:+,.0f} "
            f"ev={r['events']}",
            flush=True,
        )

    tot_net = sum(r["net"] for r in results)
    traded = [r for r in results if int(r["setups"] or 0) > 0]
    flat = [r for r in results if int(r["setups"] or 0) == 0]
    green = [r for r in results if r["net"] > 0]
    red = [r for r in results if r["net"] < 0]

    summary = {
        "title": f"Range-bound days last 1m · {days[0]}→{days[-1]} · ₹{capital:,.0f}",
        "generated_at": datetime.now().isoformat(),
        "definition": {
            "rules": [
                "|chg|<60",
                "or |chg|<100 and efficiency<0.40",
                "or day_range<140 and efficiency<0.45",
                "or efficiency<0.22",
            ]
        },
        "capital": capital,
        "deploy": deploy,
        "sessions_scanned": len(metrics),
        "range_bound_count": len(rb),
        "trend_count": len(trend),
        "range_bound_days": rb,
        "trend_days": [
            {
                "date": m["date"],
                "spot_chg": m["spot_chg"],
                "day_range": m["day_range"],
                "efficiency": m["efficiency"],
            }
            for m in trend
        ],
        "results": [
            {k: v for k, v in r.items() if k not in ("row", "ledger")} for r in results
        ],
        "totals": {
            "days": len(results),
            "traded": len(traded),
            "flat_skip": len(flat),
            "green": len(green),
            "red": len(red),
            "setups": sum(int(r["setups"] or 0) for r in results),
            "wins": sum(int(r["wins"] or 0) for r in results),
            "losses": sum(int(r["losses"] or 0) for r in results),
            "net": round(tot_net, 2),
            "avg_net_all": round(tot_net / max(len(results), 1), 2),
            "avg_net_traded": round(
                sum(r["net"] for r in traded) / max(len(traded), 1), 2
            ),
        },
    }

    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "rangebound_last1m_report.json"
    jp.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # combined ledger-style report for traded+flat
    daily = []
    ledger = []
    bal = capital
    for r in results:
        row = dict(r["row"])
        # re-base balance path for combined view (independent days each start at capital)
        daily.append(row)
        ledger.extend(r["ledger"])
    combined = {
        "title": summary["title"],
        "generated_at": summary["generated_at"],
        "mode": "causal_rangebound_days_independent",
        "profile": "aggressive + SR reclaim + CE breakout",
        "range": {"start": rb[0]["date"] if rb else None, "end": rb[-1]["date"] if rb else None},
        "capital": {
            "initial": capital,
            "deploy_pct": deploy,
            "deploy_cap": 0.0,
            "halt_pct": 0.3,
            "halt_floor": capital * 0.3,
            "final_balance": capital + tot_net,
            "net_pnl": tot_net,
            "return_pct": round(100.0 * tot_net / capital, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            "session_days": len(results),
            "traded_days": len(traded),
            "setups": summary["totals"]["setups"],
            "wins": summary["totals"]["wins"],
            "losses": summary["totals"]["losses"],
            "green_days": len(green),
            "red_days": len(red),
            "lots": sum(int(d.get("lots_total") or 0) for d in daily),
            "invested_total": sum(float(t.get("invested") or 0) for t in ledger),
            "gross": round(sum(float(d.get("gross") or 0) for d in daily), 2),
            "charges": round(sum(float(d.get("charges") or 0) for d in daily), 2),
            "net": round(tot_net, 2),
            "post_decay_entries": sum(int(d.get("post_decay_entries") or 0) for d in daily),
            "minutes_processed": sum(int(d.get("minutes") or 0) for d in daily),
            "analyses": sum(int(d.get("analyses") or 0) for d in daily),
            "signal_minutes": sum(int(d.get("allow_true") or 0) for d in daily),
        },
        "daily": daily,
        "ledger": ledger,
        "premium_model": "Synthetic ATM; each range-bound day independent ₹5L start",
    }
    hp = OUT / "rangebound_last1m_algo_report.html"
    (OUT / "rangebound_last1m_algo_report.json").write_text(
        json.dumps(combined, indent=2), encoding="utf-8"
    )
    try:
        write_detailed_html(combined, hp)
    except Exception as exc:  # noqa: BLE001
        print("html warn", exc)

    print("\n===== SUMMARY =====")
    print(f"Range-bound {len(rb)} days · traded {len(traded)} · flat {len(flat)}")
    print(f"G/R {len(green)}/{len(red)}  setups {summary['totals']['setups']}  "
          f"W/L {summary['totals']['wins']}/{summary['totals']['losses']}")
    print(f"Net (sum independent days) {tot_net:+,.0f}")
    print(f"JSON {jp}")
    print(f"HTML {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

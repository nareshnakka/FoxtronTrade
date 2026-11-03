"""
Train / evaluate on RANGE-BOUND and SHARP-FALL days (causal).

Range-bound: low |chg| and/or low path efficiency (chop).
Sharp-fall: day close-open <= -150 OR day_range down-extent heavy with close weak.

Writes:
  data/backtests/train_range_sharp_report.{json,html}
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
)
from backtest_month_ledger import OUT, LedgerState, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides
from tools_test_rangebound_month import day_metrics, is_range_bound

IST = ZoneInfo("Asia/Kolkata")
MINUTES = Path("data/expiries/last_3_months/minutes")


def is_sharp_fall(m: dict) -> bool:
    """Sharp one-way dump day (Nifty)."""
    chg = float(m["spot_chg"])
    rng = float(m["day_range"])
    eff = float(m["efficiency"])
    # Hard crash close, or wide down day that mostly stuck down
    if chg <= -150:
        return True
    if chg <= -100 and eff >= 0.45 and rng >= 160:
        return True
    if chg <= -80 and rng >= 250 and eff >= 0.35:
        return True
    return False


def classify_year(as_of: date, calendar_days: int = 365) -> dict:
    days = _load_month_days(as_of=as_of, calendar_days=calendar_days, all_available=False)
    rb, sharp, other = [], [], []
    for d in days:
        path = MINUTES / f"nifty_{d['date']}.json"
        if not path.exists() or path.stat().st_size < 100:
            continue
        m = day_metrics(d["date"])
        m["is_expiry"] = bool(d.get("is_expiry_day"))
        if is_sharp_fall(m):
            m["regime"] = "sharp_fall"
            sharp.append(m)
        elif is_range_bound(m):
            m["regime"] = "range_bound"
            rb.append(m)
        else:
            m["regime"] = "other"
            other.append(m)
    return {"range_bound": rb, "sharp_fall": sharp, "other": other, "all_days": days}


def run_subset(
    days_meta: list[dict],
    *,
    capital: float,
    deploy: float,
    label: str,
    seed_before: str | None,
) -> dict:
    cfg = month_cfg()
    cfg.update(aggressive_overrides(capital, deploy))
    # Regime-specific overlays applied in aggressive_overrides / market gates.
    state = LedgerState(
        initial=float(capital),
        balance=float(capital),
        deploy_pct=float(deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(seed_before or (days_meta[0]["date"] if days_meta else "2026-01-01"))
    decay = DecayMemory(persist=False, seed_days=seed)
    rows = []
    for m in days_meta:
        day = m["date"]
        bars = _load_bars(MINUTES / f"nifty_{day}.json")
        ce0, pe0 = _day_prem_opens(day)
        # Independent capital per day for clean regime score (no compound bias)
        state.balance = float(capital)
        state.halted = False
        state.halt_reason = ""
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=day,
            is_expiry_day=bool(m.get("is_expiry")),
            decay_mem=decay,
            ce_open=ce0,
            pe_open=pe0,
        )
        decay.finalize_day(day)
        rows.append(
            {
                **m,
                "net": round(float(row.get("net") or 0), 2),
                "setups": int(row.get("setups") or 0),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
                "trades": [
                    {
                        "side": t.get("side"),
                        "event": t.get("event"),
                        "net": round(float(t.get("net") or 0), 2),
                        "entry": t.get("entry_time"),
                        "exit": t.get("exit_time"),
                    }
                    for t in (row.get("trades") or [])
                ],
            }
        )
    nets = [float(r["net"]) for r in rows]
    traded = [r for r in rows if r["setups"] > 0]
    flat = [r for r in rows if r["setups"] == 0]
    w = sum(r["wins"] for r in rows)
    l = sum(r["losses"] for r in rows)
    return {
        "label": label,
        "n_days": len(rows),
        "traded": len(traded),
        "flat": len(flat),
        "wins": w,
        "losses": l,
        "net_sum": round(sum(nets), 2),
        "avg_net": round(sum(nets) / len(nets), 2) if nets else 0,
        "days": rows,
    }


def main() -> int:
    as_of = date(2026, 11, 2)
    capital = 1_000_000.0
    deploy = 0.70
    print("Classifying year…")
    buckets = classify_year(as_of)
    rb = buckets["range_bound"]
    sf = buckets["sharp_fall"]
    print(f"  range_bound={len(rb)}  sharp_fall={len(sf)}  other={len(buckets['other'])}")

    print("\n===== RANGE-BOUND (independent ₹10L/day) =====")
    rb_rep = run_subset(rb, capital=capital, deploy=deploy, label="range_bound", seed_before=rb[0]["date"] if rb else None)
    print(
        f"  days={rb_rep['n_days']} traded={rb_rep['traded']} flat={rb_rep['flat']} "
        f"W/L {rb_rep['wins']}/{rb_rep['losses']} net_sum={rb_rep['net_sum']:+,.0f}"
    )
    for r in sorted(rb_rep["days"], key=lambda x: x["net"])[:8]:
        print(f"    {r['date']} chg={r['spot_chg']:+.0f} rng={r['day_range']:.0f} eff={r['efficiency']:.2f} "
              f"net={r['net']:+,.0f} setups={r['setups']} {[ (t['side'], t['event'], t['net']) for t in r['trades'] ]}")

    print("\n===== SHARP-FALL (independent ₹10L/day) =====")
    sf_rep = run_subset(sf, capital=capital, deploy=deploy, label="sharp_fall", seed_before=sf[0]["date"] if sf else None)
    print(
        f"  days={sf_rep['n_days']} traded={sf_rep['traded']} flat={sf_rep['flat']} "
        f"W/L {sf_rep['wins']}/{sf_rep['losses']} net_sum={sf_rep['net_sum']:+,.0f}"
    )
    for r in sorted(sf_rep["days"], key=lambda x: x["net"]):
        print(f"    {r['date']} chg={r['spot_chg']:+.0f} rng={r['day_range']:.0f} eff={r['efficiency']:.2f} "
              f"net={r['net']:+,.0f} setups={r['setups']} {[ (t['side'], t['event'], t['net']) for t in r['trades'] ]}")

    out = {
        "title": "Train/eval range-bound + sharp-fall",
        "generated_at": datetime.now(IST).isoformat(),
        "capital": capital,
        "deploy": deploy,
        "counts": {
            "range_bound": len(rb),
            "sharp_fall": len(sf),
            "other": len(buckets["other"]),
        },
        "range_bound": rb_rep,
        "sharp_fall": sf_rep,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "train_range_sharp_report.json"
    jp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nJSON {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

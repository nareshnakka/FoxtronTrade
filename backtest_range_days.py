"""
Find RANGE days in last ~3 months and causal-test the live algo on them.

Classification (end-of-session label — for selecting the day set only):
  RANGE if most of:
    - day efficiency |close-open|/range is low
    - day range not explosive
    - many direction flips
    - close near open

Backtest: minute-by-minute causal (same as live) — no future bars inside the day.
No lookahead entries. Fresh capital per day so one range day doesn't contaminate another
when reporting per-day behavior (also runs a chained ledger for cumulative).

Usage:
  .venv\\Scripts\\python.exe backtest_range_days.py
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any

from backtest_month_ledger import (
    LedgerState,
    _load_closes,
    _load_month_days,
    month_cfg,
    simulate_day_ledger,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "backtests"


def _classify_day(closes: list[float]) -> dict[str, Any]:
    """Post-session range vs trend label (does not drive intraday entries)."""
    if len(closes) < 60:
        return {"label": "SKIP", "reason": "too few bars"}
    o, c = float(closes[0]), float(closes[-1])
    hi, lo = max(closes), min(closes)
    day_rng = hi - lo
    net = abs(c - o)
    eff = net / max(day_rng, 1.0)
    flips = 0
    for i in range(2, len(closes)):
        d1 = closes[i - 1] - closes[i - 2]
        d2 = closes[i] - closes[i - 1]
        if d1 * d2 < 0:
            flips += 1
    # Mid-session chop: 10:00–14:00 efficiency (indices ~45..285 from 09:15)
    mid = closes[45:286] if len(closes) > 100 else closes
    mid_rng = max(mid) - min(mid) if mid else day_rng
    mid_eff = abs(mid[-1] - mid[0]) / max(mid_rng, 1.0) if len(mid) > 2 else eff

    score = 0.0
    tags: list[str] = []
    if eff <= 0.35:
        score += 0.35
        tags.append(f"low day-eff {eff:.2f}")
    if mid_eff <= 0.40:
        score += 0.25
        tags.append(f"low mid-eff {mid_eff:.2f}")
    if day_rng <= 120:
        score += 0.20
        tags.append(f"tight day-rng {day_rng:.0f}")
    elif day_rng <= 160 and eff <= 0.45:
        score += 0.10
        tags.append(f"mod rng {day_rng:.0f}")
    if flips >= 80:
        score += 0.20
        tags.append(f"flips {flips}")
    elif flips >= 55:
        score += 0.10
        tags.append(f"flips {flips}")
    if abs(c - o) <= 40:
        score += 0.15
        tags.append(f"close~open Δ{c-o:+.0f}")

    # Trend override: strong directional day
    if eff >= 0.55 and day_rng >= 140 and abs(c - o) >= 80:
        label = "TREND"
    elif score >= 0.55:
        label = "RANGE"
    elif eff >= 0.45 and abs(c - o) >= 60:
        label = "TREND"
    else:
        label = "MIXED"

    return {
        "label": label,
        "score": round(score, 3),
        "day_range": round(day_rng, 1),
        "vs_open": round(c - o, 1),
        "efficiency": round(eff, 3),
        "mid_efficiency": round(mid_eff, 3),
        "flips": flips,
        "reason": "; ".join(tags) or "neutral",
    }


def _run_causal_on_days(
    days: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    initial: float,
    deploy_pct: float,
    chained: bool,
) -> dict[str, Any]:
    state = LedgerState(
        initial=initial,
        balance=initial,
        deploy_pct=deploy_pct,
        deploy_cap=0.0,
    )
    daily = []
    for d in days:
        closes = _load_closes(d["minutes_path"])
        if not chained:
            state = LedgerState(
                initial=initial,
                balance=initial,
                deploy_pct=deploy_pct,
                deploy_cap=0.0,
            )
        row = simulate_day_ledger(
            closes,
            cfg=cfg,
            state=state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
        )
        meta = _classify_day(closes)
        row["range_meta"] = meta
        daily.append(row)

    nets = [float(r.get("net") or 0) for r in daily]
    traded = [r for r in daily if (r.get("trades") or r.get("setups"))]
    g = sum(1 for n in nets if n > 0)
    r = sum(1 for n in nets if n < 0)
    z = sum(1 for n in nets if n == 0)
    trade_nets = [
        float(t.get("net") or 0)
        for day in daily
        for t in (day.get("trades") or [])
    ]
    return {
        "chained": chained,
        "initial": initial,
        "final": round(state.balance if chained else initial + sum(nets), 2),
        "net": round((state.balance - initial) if chained else sum(nets), 2),
        "sessions": len(daily),
        "traded_days": len(traded),
        "green_days": g,
        "red_days": r,
        "flat_days": z,
        "trades": sum(len(day.get("trades") or []) for day in daily),
        "avg_day_net": round(mean(nets), 1) if nets else 0,
        "med_day_net": round(median(nets), 1) if nets else 0,
        "trade_win_rate": round(
            100 * sum(1 for n in trade_nets if n > 0) / len(trade_nets), 1
        )
        if trade_nets
        else 0,
        "daily": daily,
    }


def main() -> None:
    cfg = month_cfg()
    all_days = _load_month_days(all_available=True)
    classified: list[dict[str, Any]] = []
    for d in all_days:
        closes = _load_closes(d["minutes_path"])
        meta = _classify_day(closes)
        classified.append({**d, **meta, "bars": len(closes)})

    range_days = [d for d in classified if d["label"] == "RANGE"]
    trend_days = [d for d in classified if d["label"] == "TREND"]
    mixed_days = [d for d in classified if d["label"] == "MIXED"]

    print(
        f"Sessions {len(classified)} · RANGE {len(range_days)} · "
        f"TREND {len(trend_days)} · MIXED {len(mixed_days)}"
    )
    print("Causal backtest on RANGE days only (₹1L, 50% deploy, current profile)…")

    range_iso = [
        {
            "date": d["date"],
            "minutes_path": d["minutes_path"],
            "is_expiry_day": d.get("is_expiry_day"),
        }
        for d in range_days
    ]
    trend_iso = [
        {
            "date": d["date"],
            "minutes_path": d["minutes_path"],
            "is_expiry_day": d.get("is_expiry_day"),
        }
        for d in trend_days
    ]

    # Per-day independent capital (fair day-type comparison)
    range_ind = _run_causal_on_days(
        range_iso, cfg=cfg, initial=100_000, deploy_pct=0.50, chained=False
    )
    trend_ind = _run_causal_on_days(
        trend_iso, cfg=cfg, initial=100_000, deploy_pct=0.50, chained=False
    )
    # Chained ledger across range days only (as if only those sessions existed)
    range_chain = _run_causal_on_days(
        range_iso, cfg=cfg, initial=100_000, deploy_pct=0.50, chained=True
    )

    print(
        f"RANGE independent: net={range_ind['net']:+.0f} "
        f"g/r/z={range_ind['green_days']}/{range_ind['red_days']}/{range_ind['flat_days']} "
        f"trades={range_ind['trades']} traded_days={range_ind['traded_days']}"
    )
    print(
        f"RANGE chained:     net={range_chain['net']:+.0f} final=₹{range_chain['final']:,.0f}"
    )
    print(
        f"TREND independent: net={trend_ind['net']:+.0f} "
        f"g/r/z={trend_ind['green_days']}/{trend_ind['red_days']}/{trend_ind['flat_days']} "
        f"trades={trend_ind['trades']}"
    )

    report = {
        "title": "Range-Day Causal Backtest (last 3 months)",
        "generated_at": date.today().isoformat(),
        "mode": "causal_minute_live_algo_gates",
        "cheat": False,
        "notes": [
            "Day LABEL uses full-session stats only to pick the RANGE set.",
            "Entries/exits inside each day use only bars up to that minute (no lookahead).",
            "Independent = reset ₹1L each day; Chained = continuous ledger across range days.",
        ],
        "classification": {
            "sessions": len(classified),
            "range": len(range_days),
            "trend": len(trend_days),
            "mixed": len(mixed_days),
            "range_dates": [d["date"] for d in range_days],
            "trend_dates": [d["date"] for d in trend_days],
            "mixed_dates": [d["date"] for d in mixed_days],
            "days": [
                {
                    "date": d["date"],
                    "label": d["label"],
                    "day_range": d["day_range"],
                    "vs_open": d["vs_open"],
                    "efficiency": d["efficiency"],
                    "flips": d["flips"],
                    "score": d["score"],
                    "reason": d["reason"],
                    "is_expiry_day": bool(d.get("is_expiry_day")),
                }
                for d in classified
            ],
        },
        "config_snapshot": {
            "profile": cfg.get("profile_name"),
            "max_trades_per_day": cfg.get("max_trades_per_day"),
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "pe_min_vs_open": cfg.get("pe_min_vs_open"),
            "signal_persist_minutes": cfg.get("signal_persist_minutes"),
            "orb_minutes": cfg.get("orb_minutes"),
            "stop_loss_pct": cfg.get("stop_loss_pct"),
            "deploy_pct": 0.50,
        },
        "range_independent": {
            k: v for k, v in range_ind.items() if k != "daily"
        },
        "range_chained": {k: v for k, v in range_chain.items() if k != "daily"},
        "trend_independent": {
            k: v for k, v in trend_ind.items() if k != "daily"
        },
        "range_day_results": [
            {
                "date": r["date"],
                "net": r.get("net"),
                "setups": r.get("setups"),
                "trades": [
                    {
                        "side": t.get("side"),
                        "entry_time": t.get("entry_time"),
                        "exit_time": t.get("exit_time"),
                        "lots": t.get("lots"),
                        "invested": t.get("invested"),
                        "net": t.get("net"),
                        "reason": t.get("reason"),
                    }
                    for t in (r.get("trades") or [])
                ],
                "meta": r.get("range_meta"),
                "skip": r.get("skip"),
            }
            for r in range_ind["daily"]
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / "range_days_report.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Compact HTML
    rows_html = []
    for r in report["range_day_results"]:
        net = float(r.get("net") or 0)
        cls = "good" if net > 0 else ("bad" if net < 0 else "muted")
        trade_s = ", ".join(
            f"{t['side']} {t['entry_time']}→{t['exit_time']} ₹{float(t['net']):+.0f}"
            for t in (r.get("trades") or [])
        ) or "—"
        meta = r.get("meta") or {}
        rows_html.append(
            f"<tr><td>{r['date']}</td><td>{meta.get('day_range')}</td>"
            f"<td>{meta.get('efficiency')}</td><td>{meta.get('vs_open')}</td>"
            f"<td class='{cls}'>{net:+,.0f}</td><td>{r.get('setups') or 0}</td>"
            f"<td>{trade_s}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Range Days Report</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0f1419;color:#e7ecf3;margin:24px}}
h1,h2{{color:#fff}} .good{{color:#7dcea0}} .bad{{color:#f1948a}} .muted{{color:#9aa4b2}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid #2a3340;padding:6px 8px;text-align:left}}
th{{color:#9aa4b2}} .card{{background:#1a222d;border-radius:10px;padding:14px 16px;margin:12px 0}}
</style></head><body>
<h1>Range-day causal backtest · last 3 months</h1>
<p class="muted">No lookahead entries. Labels use full-day stats only to select RANGE days;
intraday sim is minute-causal like live.</p>
<div class="card">
<b>Classification:</b> {len(range_days)} RANGE · {len(trend_days)} TREND · {len(mixed_days)} MIXED
(of {len(classified)} sessions)<br/>
<b>RANGE independent (₹1L reset/day):</b> net <span class="{'good' if range_ind['net']>=0 else 'bad'}">₹{range_ind['net']:+,.0f}</span>
· green/red/flat {range_ind['green_days']}/{range_ind['red_days']}/{range_ind['flat_days']}
· trades {range_ind['trades']} · traded days {range_ind['traded_days']}<br/>
<b>RANGE chained ledger:</b> ₹{range_chain['final']:,.0f} (net ₹{range_chain['net']:+,.0f})<br/>
<b>TREND independent (comparison):</b> net <span class="{'good' if trend_ind['net']>=0 else 'bad'}">₹{trend_ind['net']:+,.0f}</span>
· g/r/z {trend_ind['green_days']}/{trend_ind['red_days']}/{trend_ind['flat_days']} · trades {trend_ind['trades']}
</div>
<h2>Each RANGE day</h2>
<table>
<tr><th>Date</th><th>Day rng</th><th>Eff</th><th>Vs open</th><th>Day net</th><th>Setups</th><th>Trades</th></tr>
{''.join(rows_html)}
</table>
</body></html>"""
    html_path = OUT / "range_days_report.html"
    html_path.write_text(html, encoding="utf-8")
    print("JSON", json_path)
    print("HTML", html_path)


if __name__ == "__main__":
    main()

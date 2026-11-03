"""
Up days: session close vs open in [+10, +500] points.
Causal minute-by-minute algo test (no lookahead).

Usage:
  .venv\\Scripts\\python.exe backtest_up_days.py
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from backtest_month_ledger import (
    LedgerState,
    _load_closes,
    _load_month_days,
    month_cfg,
    simulate_day_ledger,
)

OUT = Path(__file__).resolve().parent / "data" / "backtests"
UP_MIN = 10.0
UP_MAX = 500.0


def _day_vs_open(closes: list[float]) -> dict[str, Any]:
    if len(closes) < 30:
        return {"ok": False}
    o, c = float(closes[0]), float(closes[-1])
    hi, lo = max(closes), min(closes)
    vs = c - o
    return {
        "ok": True,
        "vs_open": round(vs, 1),
        "day_range": round(hi - lo, 1),
        "day_low_vs_open": round(lo - o, 1),
        "day_high_vs_open": round(hi - o, 1),
        "is_up": UP_MIN <= vs <= UP_MAX,
    }


def _run(
    days: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    initial: float = 100_000.0,
    deploy_pct: float = 0.50,
    chained: bool = False,
) -> dict[str, Any]:
    state = LedgerState(
        initial=initial, balance=initial, deploy_pct=deploy_pct, deploy_cap=0.0
    )
    daily = []
    for d in days:
        closes = _load_closes(d["minutes_path"])
        if not chained:
            state = LedgerState(
                initial=initial, balance=initial, deploy_pct=deploy_pct, deploy_cap=0.0
            )
        row = simulate_day_ledger(
            closes,
            cfg=cfg,
            state=state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
        )
        row["up_meta"] = _day_vs_open(closes)
        daily.append(row)

    nets = [float(r.get("net") or 0) for r in daily]
    trade_nets = [
        float(t.get("net") or 0) for day in daily for t in (day.get("trades") or [])
    ]
    g = sum(1 for n in nets if n > 0)
    r = sum(1 for n in nets if n < 0)
    z = sum(1 for n in nets if n == 0)
    return {
        "chained": chained,
        "initial": initial,
        "final": round(state.balance if chained else initial + sum(nets), 2),
        "net": round((state.balance - initial) if chained else sum(nets), 2),
        "sessions": len(daily),
        "traded_days": sum(1 for d in daily if d.get("setups")),
        "green_days": g,
        "red_days": r,
        "flat_days": z,
        "trades": sum(len(d.get("trades") or []) for d in daily),
        "avg_day_net": round(mean(nets), 1) if nets else 0,
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

    up_days: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    for d in all_days:
        closes = _load_closes(d["minutes_path"])
        meta = _day_vs_open(closes)
        if not meta.get("ok"):
            continue
        row = {
            "date": d["date"],
            "minutes_path": d["minutes_path"],
            "is_expiry_day": bool(d.get("is_expiry_day")),
            **meta,
        }
        catalog.append(row)
        if meta["is_up"]:
            up_days.append(row)

    print(
        f"Sessions {len(catalog)} · UP (+10..+500 vs open) {len(up_days)} · "
        f"other {len(catalog) - len(up_days)}"
    )
    print("Causal backtest on UP days only (₹1L, 50% deploy)…")

    up_iso = [
        {
            "date": d["date"],
            "minutes_path": d["minutes_path"],
            "is_expiry_day": d.get("is_expiry_day"),
        }
        for d in sorted(up_days, key=lambda x: x["date"])
    ]
    up_ind = _run(up_iso, cfg=cfg, chained=False)
    up_chain = _run(up_iso, cfg=cfg, chained=True)

    bucket_stats = []
    for name, lo, hi in [
        ("+10 to +50", 10.0, 50.0),
        ("+51 to +100", 51.0, 100.0),
        ("+101 to +200", 101.0, 200.0),
        ("+201 to +500", 201.0, 500.0),
    ]:
        subset = [
            {
                "date": d["date"],
                "minutes_path": d["minutes_path"],
                "is_expiry_day": d.get("is_expiry_day"),
            }
            for d in sorted(up_days, key=lambda x: x["date"])
            if lo <= d["vs_open"] <= hi
        ]
        if not subset:
            bucket_stats.append(
                {
                    "band": name,
                    "days": 0,
                    "traded_days": 0,
                    "green": 0,
                    "red": 0,
                    "net": 0,
                    "trades": 0,
                }
            )
            continue
        res = _run(subset, cfg=cfg, chained=False)
        bucket_stats.append(
            {
                "band": name,
                "days": res["sessions"],
                "traded_days": res["traded_days"],
                "green": res["green_days"],
                "red": res["red_days"],
                "net": res["net"],
                "trades": res["trades"],
            }
        )
        print(
            f"  {name}: days={res['sessions']} net={res['net']:+.0f} "
            f"g/r={res['green_days']}/{res['red_days']} trades={res['trades']}"
        )

    print(
        f"UP all: net={up_ind['net']:+.0f} g/r/z="
        f"{up_ind['green_days']}/{up_ind['red_days']}/{up_ind['flat_days']} "
        f"trades={up_ind['trades']} chained_final=₹{up_chain['final']:,.0f}"
    )

    report = {
        "title": "Up-day causal backtest (+10 to +500 pts vs open)",
        "generated_at": date.today().isoformat(),
        "mode": "causal_minute_live_algo_gates",
        "cheat": False,
        "definition": {
            "metric": "close - open (spot points)",
            "include_if": f"{UP_MIN} <= vs_open <= {UP_MAX}",
        },
        "classification": {
            "sessions": len(catalog),
            "up_days": len(up_days),
            "other_days": len(catalog) - len(up_days),
            "up_dates": [d["date"] for d in sorted(up_days, key=lambda x: x["date"])],
            "days": [
                {
                    "date": d["date"],
                    "vs_open": d["vs_open"],
                    "day_range": d["day_range"],
                    "is_up": d["is_up"],
                    "is_expiry_day": d["is_expiry_day"],
                }
                for d in sorted(catalog, key=lambda x: x["date"])
            ],
        },
        "config_snapshot": {
            "profile": cfg.get("profile_name"),
            "deploy_pct": 0.50,
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "pe_min_vs_open": cfg.get("pe_min_vs_open"),
        },
        "up_independent": {k: v for k, v in up_ind.items() if k != "daily"},
        "up_chained": {k: v for k, v in up_chain.items() if k != "daily"},
        "buckets": bucket_stats,
        "up_day_results": [
            {
                "date": r["date"],
                "vs_open": (r.get("up_meta") or {}).get("vs_open"),
                "day_range": (r.get("up_meta") or {}).get("day_range"),
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
                "skip": r.get("skip"),
            }
            for r in up_ind["daily"]
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / "up_days_report.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rows = []
    for r in report["up_day_results"]:
        net = float(r.get("net") or 0)
        cls = "good" if net > 0 else ("bad" if net < 0 else "muted")
        trades = ", ".join(
            f"{t['side']} {t['entry_time']}→{t['exit_time']} ₹{float(t['net']):+.0f}"
            for t in (r.get("trades") or [])
        ) or "—"
        rows.append(
            f"<tr><td>{r['date']}</td><td>{r.get('vs_open')}</td>"
            f"<td>{r.get('day_range')}</td>"
            f"<td class='{cls}'>{net:+,.0f}</td><td>{r.get('setups') or 0}</td>"
            f"<td>{trades}</td></tr>"
        )
    bucket_rows = "".join(
        f"<tr><td>{b['band']}</td><td>{b['days']}</td><td>{b.get('traded_days', 0)}</td>"
        f"<td>{b.get('green', 0)}/{b.get('red', 0)}</td>"
        f"<td class='{'good' if b['net'] >= 0 else 'bad'}'>{b['net']:+,.0f}</td>"
        f"<td>{b['trades']}</td></tr>"
        for b in bucket_stats
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Up Days Report</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0f1419;color:#e7ecf3;margin:24px}}
h1,h2{{color:#fff}} .good{{color:#7dcea0}} .bad{{color:#f1948a}} .muted{{color:#9aa4b2}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid #2a3340;padding:6px 8px;text-align:left}}
th{{color:#9aa4b2}} .card{{background:#1a222d;border-radius:10px;padding:14px 16px;margin:12px 0}}
</style></head><body>
<h1>Up days (+10 to +500 pts vs open) · causal backtest</h1>
<p class="muted">No lookahead. Filter = close−open in [+10, +500]. Intraday sim is minute-causal.</p>
<div class="card">
<b>Up days:</b> {len(up_days)} / {len(catalog)} sessions<br/>
<b>Independent (₹1L/day reset):</b> net
<span class="{'good' if up_ind['net'] >= 0 else 'bad'}">₹{up_ind['net']:+,.0f}</span>
· g/r/flat {up_ind['green_days']}/{up_ind['red_days']}/{up_ind['flat_days']}
· trades {up_ind['trades']} · win% {up_ind['trade_win_rate']}<br/>
<b>Chained ledger:</b> ₹{up_chain['final']:,.0f} (net ₹{up_chain['net']:+,.0f})
</div>
<h2>By up depth</h2>
<table>
<tr><th>Band (vs open)</th><th>Days</th><th>Traded</th><th>G/R</th><th>Net</th><th>Trades</th></tr>
{bucket_rows}
</table>
<h2>Each up day</h2>
<table>
<tr><th>Date</th><th>Vs open</th><th>Day rng</th><th>Day net</th><th>Setups</th><th>Trades</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    html_path = OUT / "up_days_report.html"
    html_path.write_text(html, encoding="utf-8")
    print("JSON", json_path)
    print("HTML", html_path)


if __name__ == "__main__":
    main()

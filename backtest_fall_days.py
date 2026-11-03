"""
Fall days: session close vs open in [-500, -10] points.
Causal minute-by-minute algo test (no lookahead).

Usage:
  .venv\\Scripts\\python.exe backtest_fall_days.py
"""
from __future__ import annotations

import json
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

OUT = Path(__file__).resolve().parent / "data" / "backtests"

# Fall = close below open by 10..500 pts (inclusive)
FALL_MIN = -500.0  # most negative allowed
FALL_MAX = -10.0  # least negative (smallest fall)


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
        "is_fall": FALL_MIN <= vs <= FALL_MAX,
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
        meta = _day_vs_open(closes)
        row["fall_meta"] = meta
        daily.append(row)

    nets = [float(r.get("net") or 0) for r in daily]
    trade_nets = [
        float(t.get("net") or 0)
        for day in daily
        for t in (day.get("trades") or [])
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

    fall_days: list[dict[str, Any]] = []
    other_days: list[dict[str, Any]] = []
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
        if meta["is_fall"]:
            fall_days.append(row)
        else:
            other_days.append(row)

    fall_days.sort(key=lambda x: x["vs_open"])  # deepest falls first for listing
    print(
        f"Sessions {len(catalog)} · FALL (−500..−10 vs open) {len(fall_days)} · other {len(other_days)}"
    )
    print("Causal backtest on FALL days only (₹1L, 50% deploy)…")

    fall_iso = [
        {
            "date": d["date"],
            "minutes_path": d["minutes_path"],
            "is_expiry_day": d.get("is_expiry_day"),
        }
        for d in fall_days
    ]
    # restore chronological order for chained
    fall_iso_chrono = sorted(fall_iso, key=lambda x: x["date"])

    fall_ind = _run(fall_iso_chrono, cfg=cfg, chained=False)
    fall_chain = _run(fall_iso_chrono, cfg=cfg, chained=True)

    # Buckets by fall depth (inclusive, no overlap)
    bucket_stats = []
    for name, lo, hi in [
        ("−10 to −50", -50.0, -10.0),
        ("−51 to −100", -100.0, -51.0),
        ("−101 to −200", -200.0, -101.0),
        ("−201 to −500", -500.0, -201.0),
    ]:
        subset = [
            {
                "date": d["date"],
                "minutes_path": d["minutes_path"],
                "is_expiry_day": d.get("is_expiry_day"),
            }
            for d in sorted(fall_days, key=lambda x: x["date"])
            if lo <= d["vs_open"] <= hi
        ]
        if not subset:
            bucket_stats.append(
                {"band": name, "days": 0, "traded_days": 0, "green": 0, "red": 0, "net": 0, "trades": 0}
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
        f"FALL all: net={fall_ind['net']:+.0f} g/r/z="
        f"{fall_ind['green_days']}/{fall_ind['red_days']}/{fall_ind['flat_days']} "
        f"trades={fall_ind['trades']} chained_final=₹{fall_chain['final']:,.0f}"
    )

    report = {
        "title": "Fall-day causal backtest (−10 to −500 pts vs open)",
        "generated_at": date.today().isoformat(),
        "mode": "causal_minute_live_algo_gates",
        "cheat": False,
        "definition": {
            "metric": "close - open (spot points)",
            "include_if": f"{FALL_MIN} <= vs_open <= {FALL_MAX}",
            "notes": [
                "Day filter uses full-session close vs open only to select the day set.",
                "Inside each day, entries use bars up to that minute only (no lookahead).",
            ],
        },
        "classification": {
            "sessions": len(catalog),
            "fall_days": len(fall_days),
            "other_days": len(other_days),
            "fall_dates": [d["date"] for d in sorted(fall_days, key=lambda x: x["date"])],
            "days": [
                {
                    "date": d["date"],
                    "vs_open": d["vs_open"],
                    "day_range": d["day_range"],
                    "day_low_vs_open": d["day_low_vs_open"],
                    "is_fall": d["is_fall"],
                    "is_expiry_day": d["is_expiry_day"],
                }
                for d in sorted(catalog, key=lambda x: x["date"])
            ],
        },
        "config_snapshot": {
            "profile": cfg.get("profile_name"),
            "deploy_pct": 0.50,
            "max_trades_per_day": cfg.get("max_trades_per_day"),
            "pe_min_vs_open": cfg.get("pe_min_vs_open"),
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "signal_persist_minutes": cfg.get("signal_persist_minutes"),
        },
        "fall_independent": {k: v for k, v in fall_ind.items() if k != "daily"},
        "fall_chained": {k: v for k, v in fall_chain.items() if k != "daily"},
        "buckets": bucket_stats,
        "fall_day_results": [
            {
                "date": r["date"],
                "vs_open": (r.get("fall_meta") or {}).get("vs_open"),
                "day_range": (r.get("fall_meta") or {}).get("day_range"),
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
            for r in fall_ind["daily"]
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    json_path = OUT / "fall_days_report.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rows = []
    for r in report["fall_day_results"]:
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
        f"<tr><td>{b['band']}</td><td>{b['days']}</td><td>{b.get('traded_days',0)}</td>"
        f"<td>{b.get('green',0)}/{b.get('red',0)}</td>"
        f"<td class='{'good' if b['net']>=0 else 'bad'}'>{b['net']:+,.0f}</td>"
        f"<td>{b['trades']}</td></tr>"
        for b in bucket_stats
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Fall Days Report</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0f1419;color:#e7ecf3;margin:24px}}
h1,h2{{color:#fff}} .good{{color:#7dcea0}} .bad{{color:#f1948a}} .muted{{color:#9aa4b2}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid #2a3340;padding:6px 8px;text-align:left}}
th{{color:#9aa4b2}} .card{{background:#1a222d;border-radius:10px;padding:14px 16px;margin:12px 0}}
</style></head><body>
<h1>Fall days (−10 to −500 pts vs open) · causal backtest</h1>
<p class="muted">No lookahead. Filter = close−open in [−500, −10]. Intraday sim is minute-causal.</p>
<div class="card">
<b>Fall days:</b> {len(fall_days)} / {len(catalog)} sessions<br/>
<b>Independent (₹1L/day reset):</b> net
<span class="{'good' if fall_ind['net']>=0 else 'bad'}">₹{fall_ind['net']:+,.0f}</span>
· g/r/flat {fall_ind['green_days']}/{fall_ind['red_days']}/{fall_ind['flat_days']}
· trades {fall_ind['trades']} · win% {fall_ind['trade_win_rate']}<br/>
<b>Chained ledger:</b> ₹{fall_chain['final']:,.0f} (net ₹{fall_chain['net']:+,.0f})
</div>
<h2>By fall depth</h2>
<table>
<tr><th>Band (vs open)</th><th>Days</th><th>Traded</th><th>G/R</th><th>Net</th><th>Trades</th></tr>
{bucket_rows}
</table>
<h2>Each fall day</h2>
<table>
<tr><th>Date</th><th>Vs open</th><th>Day rng</th><th>Day net</th><th>Setups</th><th>Trades</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    html_path = OUT / "fall_days_report.html"
    html_path.write_text(html, encoding="utf-8")
    print("JSON", json_path)
    print("HTML", html_path)


if __name__ == "__main__":
    main()

"""
Run causal minute algo backtest for multiple lookbacks and write one sectioned report.

Usage:
  .venv\\Scripts\\python.exe backtest_multi_periods.py
  .venv\\Scripts\\python.exe backtest_multi_periods.py --capital 200000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any
from datetime import timezone, timedelta

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001
    IST = timezone(timedelta(hours=5, minutes=30))

from backtest_detailed import run_detailed, write_detailed_html
from backtest_month_ledger import OUT, _inr
PERIODS = [3, 5, 7, 10, 15, 20, 30, 40, 50, 60]


def _summarize(rep: dict[str, Any]) -> dict[str, Any]:
    cap = rep["capital"]
    tot = rep["totals"]
    return {
        "days_calendar": None,
        "range_start": (rep.get("range") or {}).get("start"),
        "range_end": (rep.get("range") or {}).get("end"),
        "session_days": tot.get("session_days"),
        "traded_days": tot.get("traded_days"),
        "setups": tot.get("setups"),
        "post_decay_entries": tot.get("post_decay_entries", 0),
        "lots": tot.get("lots"),
        "wins": tot.get("wins"),
        "losses": tot.get("losses"),
        "green_days": tot.get("green_days"),
        "red_days": tot.get("red_days"),
        "gross": tot.get("gross"),
        "charges": tot.get("charges"),
        "net": tot.get("net"),
        "final_balance": cap.get("final_balance"),
        "net_pnl": cap.get("net_pnl"),
        "return_pct": cap.get("return_pct"),
        "halted": cap.get("halted"),
        "halt_reason": cap.get("halt_reason"),
        "invested_total": tot.get("invested_total"),
    }


def _section_html(days: int, summary: dict[str, Any], daily: list[dict[str, Any]]) -> str:
    net = float(summary.get("net_pnl") or 0)
    tone = "good" if net > 0 else ("bad" if net < 0 else "muted")
    day_rows = []
    for d in daily:
        dn = float(d.get("net") or 0)
        cls = "g" if dn > 0 else ("r" if dn < 0 else "f")
        day_rows.append(
            f"<tr class='{cls}'><td>{d.get('date')}</td>"
            f"<td class='num'>{d.get('setups', 0)}</td>"
            f"<td class='num'>{d.get('post_decay_entries', 0)}</td>"
            f"<td class='num'>{_inr(dn)}</td>"
            f"<td class='num'>₹{float(d.get('balance_close') or 0):,.0f}</td>"
            f"<td class='num'>{d.get('spot_chg', 0):+.0f}</td>"
            f"<td class='muted'>{d.get('skip') or ''}</td></tr>"
        )
    return f"""
<section class="period" id="d{days}">
  <h2>{days}-day section</h2>
  <p class="muted">{summary.get('range_start')} → {summary.get('range_end')} ·
     {summary.get('session_days')} sessions · traded {summary.get('traded_days')}</p>
  <div class="kpis">
    <div><span class="lbl">Net P&amp;L</span><span class="{tone}">{_inr(net)} ({summary.get('return_pct'):+.1f}%)</span></div>
    <div><span class="lbl">Final</span><span>₹{float(summary.get('final_balance') or 0):,.0f}</span></div>
    <div><span class="lbl">Trades</span><span>{summary.get('setups')} (post-decay {summary.get('post_decay_entries')})</span></div>
    <div><span class="lbl">W / L</span><span>{summary.get('wins')} / {summary.get('losses')}</span></div>
    <div><span class="lbl">Green / Red days</span><span>{summary.get('green_days')} / {summary.get('red_days')}</span></div>
    <div><span class="lbl">Gross / Charges</span><span>{_inr(summary.get('gross'))} / ₹{float(summary.get('charges') or 0):,.0f}</span></div>
    <div><span class="lbl">Halted</span><span>{summary.get('halted')} {summary.get('halt_reason') or ''}</span></div>
  </div>
  <details>
    <summary>Daily breakdown</summary>
    <table>
      <thead><tr><th>Date</th><th>Setups</th><th>Post</th><th>Day P&amp;L</th><th>Balance</th><th>Spot</th><th>Note</th></tr></thead>
      <tbody>{''.join(day_rows) if day_rows else '<tr><td colspan="7" class="muted">No sessions</td></tr>'}</tbody>
    </table>
  </details>
</section>
"""


def write_multi_html(payload: dict[str, Any], path: Path) -> None:
    sections = []
    toc = []
    for block in payload.get("periods") or []:
        d = block["days"]
        toc.append(f'<a href="#d{d}">{d}d</a>')
        sections.append(_section_html(d, block["summary"], block.get("daily") or []))

    # comparison table
    cmp_rows = []
    for block in payload.get("periods") or []:
        s = block["summary"]
        net = float(s.get("net_pnl") or 0)
        cls = "g" if net > 0 else ("r" if net < 0 else "f")
        cmp_rows.append(
            f"<tr class='{cls}'><td><a href='#d{block['days']}'>{block['days']}d</a></td>"
            f"<td>{s.get('range_start')}→{s.get('range_end')}</td>"
            f"<td class='num'>{s.get('session_days')}</td>"
            f"<td class='num'>{s.get('setups')}</td>"
            f"<td class='num'>{s.get('wins')}/{s.get('losses')}</td>"
            f"<td class='num'>{s.get('green_days')}/{s.get('red_days')}</td>"
            f"<td class='num'>{_inr(net)}</td>"
            f"<td class='num'>{s.get('return_pct'):+.1f}%</td>"
            f"<td class='num'>₹{float(s.get('final_balance') or 0):,.0f}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/>
<title>FoxtronTrade — Multi-period Algo Backtest</title>
<style>
:root {{ --bg:#0f1419; --card:#1a222c; --line:#2a3542; --text:#e7eef7; --muted:#8b9bb0;
  --good:#3dbea0; --bad:#e57373; }}
body {{ margin:0; font-family: ui-sans-serif, system-ui, Segoe UI, sans-serif;
  background:var(--bg); color:var(--text); padding:24px; }}
h1 {{ margin:0 0 8px; }} h2 {{ margin:28px 0 8px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
.muted {{ color:var(--muted); }} .good {{ color:var(--good); font-weight:700; }} .bad {{ color:var(--bad); font-weight:700; }}
.toc {{ display:flex; flex-wrap:wrap; gap:10px; margin:16px 0 24px; }}
.toc a {{ color:var(--good); text-decoration:none; border:1px solid var(--line); padding:6px 10px; border-radius:8px; background:var(--card); }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:12px 0; }}
.kpis > div {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }}
.lbl {{ display:block; color:var(--muted); font-size:0.75rem; margin-bottom:4px; }}
table {{ width:100%; border-collapse:collapse; font-size:0.9rem; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
tr.g td {{ background:rgba(61,190,160,0.08); }} tr.r td {{ background:rgba(229,115,115,0.08); }}
.period {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin:18px 0; }}
details {{ margin-top:10px; }} summary {{ cursor:pointer; color:var(--muted); }}
</style></head><body>
<h1>FoxtronTrade — Multi-period algo backtest</h1>
<p class="muted">Causal minute sim · ₹{payload.get('capital', 0):,.0f} · deploy {int(float(payload.get('deploy_pct') or 0)*100)}% free cash ·
post-decay ON · generated {payload.get('generated_at')}</p>
<p class="muted">Each section is an independent run (fresh capital), last N calendar days of available sessions.</p>
<div class="toc">{''.join(toc)}</div>
<section class="period">
  <h2>Comparison</h2>
  <table>
    <thead><tr><th>Period</th><th>Range</th><th>Sessions</th><th>Trades</th><th>W/L</th><th>G/R days</th><th>Net</th><th>Return</th><th>Final</th></tr></thead>
    <tbody>{''.join(cmp_rows)}</tbody>
  </table>
</section>
{''.join(sections)}
</body></html>"""
    path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=0.75, help="Fraction of free cash to deploy (0.70–0.80)")
    ap.add_argument("--tag", type=str, default="multi_periods")
    ap.add_argument("--quiet", action="store_true", default=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    quiet = args.quiet and not args.verbose
    deploy = max(0.50, min(0.95, float(args.deploy)))

    OUT.mkdir(parents=True, exist_ok=True)
    periods_out: list[dict[str, Any]] = []

    print(
        f"Multi-period backtest · capital ₹{args.capital:,.0f} · "
        f"deploy {deploy:.0%} · periods {PERIODS}"
    )
    for days in PERIODS:
        print(f"\n=== {days}-day section ===", flush=True)
        buf = StringIO()
        old = sys.stdout
        try:
            if quiet:
                sys.stdout = buf
            rep = run_detailed(
                initial=args.capital,
                all_available=False,
                calendar_days=days,
                use_post_decay=True,
                deploy_pct=deploy,
            )
        finally:
            sys.stdout = old
        if quiet:
            # keep a short progress line from captured noise
            lines = [ln for ln in buf.getvalue().splitlines() if "ENTRY" in ln or "HALTED" in ln]
            print(f"  sessions={rep['totals']['session_days']} trades={rep['totals']['setups']} "
                  f"net={_inr(rep['capital']['net_pnl'])} ({rep['capital']['return_pct']:+.1f}%) "
                  f"entries_logged={len(lines)}", flush=True)
        else:
            print(buf.getvalue())

        summary = _summarize(rep)
        summary["days_calendar"] = days
        # slim daily for combined JSON (drop heavy timelines)
        daily_slim = []
        for d in rep.get("daily") or []:
            daily_slim.append(
                {
                    "date": d.get("date"),
                    "setups": d.get("setups"),
                    "post_decay_entries": d.get("post_decay_entries"),
                    "net": d.get("net"),
                    "balance_close": d.get("balance_close"),
                    "spot_chg": d.get("spot_chg"),
                    "wins": d.get("wins"),
                    "losses": d.get("losses"),
                    "skip": d.get("skip"),
                }
            )
        periods_out.append(
            {
                "days": days,
                "summary": summary,
                "daily": daily_slim,
                "ledger": rep.get("ledger") or [],
            }
        )
        # also write individual detailed html for drill-down
        tag = f"{args.tag}_d{days}"
        (OUT / f"{tag}_algo_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8"
        )
        try:
            write_detailed_html(rep, OUT / f"{tag}_algo_report.html")
        except Exception as exc:  # noqa: BLE001
            print(f"  (detail html skip: {exc})")

    payload = {
        "title": f"Multi-period algo backtest (₹{args.capital:,.0f}, deploy {deploy:.0%})",
        "generated_at": datetime.now(IST).isoformat(),
        "capital": args.capital,
        "deploy_pct": deploy,
        "periods": periods_out,
        "mode": "causal_minute_post_decay_independent_windows",
    }
    json_path = OUT / f"{args.tag}_report.json"
    html_path = OUT / f"{args.tag}_report.html"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_multi_html(payload, html_path)

    print("\n======== SUMMARY ========")
    for block in periods_out:
        s = block["summary"]
        print(
            f"{block['days']:>3}d  {s['range_start']}→{s['range_end']}  "
            f"sess {s['session_days']:>2}  trades {s['setups']:>3}  "
            f"W/L {s['wins']}/{s['losses']}  "
            f"g/r {s['green_days']}/{s['red_days']}  "
            f"net {_inr(s['net_pnl']):>12} ({s['return_pct']:+6.1f}%)  "
            f"final ₹{s['final_balance']:,.0f}"
        )
    print(f"\nHTML  {html_path}")
    print(f"JSON  {json_path}")


if __name__ == "__main__":
    main()

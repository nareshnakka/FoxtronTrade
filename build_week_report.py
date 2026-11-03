"""Build combined causal week HTML from per-day backtest JSON files."""
from __future__ import annotations

import json
from pathlib import Path

from backtest_day import _inr

ROOT = Path(__file__).resolve().parent / "data" / "backtests"
DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]


def main() -> None:
    reports = []
    for d in DAYS:
        reports.append(json.loads((ROOT / f"backtest_{d}.json").read_text(encoding="utf-8")))

    total_pnl = sum(r["pnl_total"] for r in reports)
    wins = sum(r["wins"] for r in reports)
    losses = sum(r["losses"] for r in reports)
    trades_n = sum(len(r["trades"]) for r in reports)

    cum = 0.0
    day_rows = []
    cum_labels = []
    cum_vals = []
    for r in reports:
        cum += float(r["pnl_total"])
        spot_chg = float(r["spot_close"]) - float(r["spot_open"])
        day_rows.append((r, spot_chg, cum))
        cum_labels.append(r["day"][5:])
        cum_vals.append(round(cum))

    y_abs = max(max(abs(v) for v in cum_vals), 500)
    w, h = 900, 260
    pad_l, pad_r, pad_t = 70, 30, 30
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - 50
    mid_y = pad_t + plot_h / 2
    n = len(cum_vals)

    def x_at(i: int) -> float:
        return pad_l if n == 1 else pad_l + i * plot_w / (n - 1)

    def y_at(v: float) -> float:
        return mid_y - (v / y_abs) * (plot_h / 2)

    poly = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(cum_vals))
    circles = []
    xlabels = []
    for i, (lab, v) in enumerate(zip(cum_labels, cum_vals)):
        fill = "#26a69a" if v > 0 else ("#ef5350" if v < 0 else "#3d9cf0")
        circles.append(
            f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="5" fill="{fill}" />'
            f'<text x="{x_at(i):.1f}" y="{y_at(v) - 12:.1f}" fill="{fill}" '
            f'font-size="12" text-anchor="middle">{_inr(v)}</text>'
        )
        xlabels.append(
            f'<text x="{x_at(i):.1f}" y="{h - 18}" fill="#8fa0b8" '
            f'font-size="12" text-anchor="middle">{lab}</text>'
        )

    bar_svg = []
    bw, bh, zero = 900, 220, 110
    scale = 90 / max(max(abs(float(r["pnl_total"])) for r in reports), 1)
    slot = (bw - 80) / len(reports)
    for i, r in enumerate(reports):
        pnl = float(r["pnl_total"])
        hh = abs(pnl) * scale
        cx = 60 + i * slot + slot / 2
        fill = "#26a69a" if pnl >= 0 else "#ef5350"
        y = zero - hh if pnl >= 0 else zero
        lab_y = y - 8 if pnl >= 0 else y + hh + 14
        bar_svg.append(
            f'<rect x="{cx - 28:.1f}" y="{y:.1f}" width="56" height="{hh:.1f}" fill="{fill}" rx="4"/>'
            f'<text x="{cx:.1f}" y="{lab_y:.1f}" fill="{fill}" font-size="11" '
            f'text-anchor="middle">{_inr(pnl)}</text>'
            f'<text x="{cx:.1f}" y="200" fill="#8fa0b8" font-size="12" '
            f'text-anchor="middle">{r["day"][5:]}</text>'
        )

    day_table = []
    for r, spot_chg, c in day_rows:
        cls = "win" if r["pnl_total"] > 0 else ("loss" if r["pnl_total"] < 0 else "")
        pnl_cls = "up" if r["pnl_total"] > 0 else "down"
        cum_cls = "up" if c > 0 else "down"
        day_table.append(
            f"<tr class='{cls}'><td>{r['day']}</td>"
            f"<td class='num'>{r['spot_open']:.0f}→{r['spot_close']:.0f}</td>"
            f"<td class='num'>{spot_chg:+.0f}</td><td>{r['setups']}</td>"
            f"<td>{len(r['trades'])}</td><td>{r['wins']}/{r['losses']}</td>"
            f"<td class='num {pnl_cls}'>{_inr(r['pnl_total'])}</td>"
            f"<td class='num {cum_cls}'>{_inr(c)}</td></tr>"
        )

    tr_html = []
    for r in reports:
        for t in r["trades"]:
            pnl = float(t["pnl"] or 0)
            cls = "win" if pnl > 0 else "loss"
            pnl_cls = "up" if pnl > 0 else "down"
            ent = (t.get("entry_ts") or "")[11:16]
            ex = (t.get("exit_ts") or "")[11:16]
            tr_html.append(
                f"<tr class='{cls}'><td>{r['day']}</td><td>{ent}→{ex}</td>"
                f"<td>{t['algo']}</td><td>{t['side']}</td><td>{t['symbol']}</td>"
                f"<td class='num'>{t['entry_px']:.1f}→{t['exit_px']:.1f}</td>"
                f"<td class='num {pnl_cls}'>{_inr(pnl)}</td>"
                f"<td>{t.get('exit_reason') or ''}</td></tr>"
            )

    tone = "down" if total_pnl < 0 else "up"
    banner = "banner down" if total_pnl < 0 else "banner up"
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>FoxtronTrade — Causal week Mon–Thu 26–29 Oct 2026</title>
<style>
:root {{ --bg:#0b1017; --panel:#141c27; --line:#243041; --text:#e8eef7; --muted:#8fa0b8; --up:#26a69a; --down:#ef5350; --accent:#3d9cf0; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Segoe UI",system-ui,sans-serif; background:radial-gradient(1200px 600px at 10% -10%,#1a2740 0%,var(--bg) 55%); color:var(--text); }}
header {{ padding:14px 20px; border-bottom:1px solid var(--line); background:#101826cc; display:flex; gap:12px; flex-wrap:wrap; align-items:center; }}
h1 {{ margin:0; font-size:1.15rem; }}
.meta {{ color:var(--muted); font-size:0.8rem; }}
.chip {{ padding:2px 8px; border-radius:999px; font-size:0.7rem; border:1px solid var(--line); color:var(--muted); }}
.chip.ok {{ color:var(--accent); border-color:#2a5a8a; }}
main {{ padding:18px 20px 48px; max-width:1100px; margin:0 auto; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:18px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
.card .label {{ color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:.04em; }}
.card .value {{ font-size:1.35rem; font-weight:700; margin-top:6px; font-variant-numeric:tabular-nums; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; margin-bottom:14px; }}
.panel h2 {{ margin:0; padding:10px 14px; font-size:0.85rem; color:var(--muted); border-bottom:1px solid var(--line); }}
.panel .body {{ padding:14px; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
@media(max-width:860px){{ .grid2 {{ grid-template-columns:1fr; }} }}
table {{ width:100%; border-collapse:collapse; font-size:0.78rem; }}
th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ color:var(--muted); background:#101826; }}
td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
tr.win td {{ background:rgba(38,166,154,.06); }}
tr.loss td {{ background:rgba(239,83,80,.06); }}
.banner {{ margin-bottom:16px; padding:12px 14px; border-radius:10px; font-size:0.9rem; }}
.banner.up {{ border:1px solid #1a6a5a; background:#142820; color:#b8e8d8; }}
.banner.down {{ border:1px solid #6a3030; background:#2a1818; color:#f0c0c0; }}
svg.chart {{ width:100%; height:auto; display:block; }}
.note {{ color:var(--muted); font-size:0.75rem; margin-top:10px; }}
a {{ color:var(--accent); }}
</style></head><body>
<header>
  <div>
    <h1>Causal week · Mon 26 Oct → Thu 29 Oct 2026</h1>
    <div class="meta">Blind minute walk · no lookahead · same live rules (session trend + band 125–145 · stop 120 · trail 175)</div>
  </div>
  <span style="flex:1"></span>
  <span class="chip ok">no lookahead</span>
  <span class="chip">4 sessions</span>
</header>
<main>
  <div class="{banner}">Week net <strong>{_inr(total_pnl)}</strong> · {wins}W / {losses}L across {trades_n} legs · each day replayed only with bars ≤ that minute.</div>
  <div class="cards">
    <div class="card"><div class="label">Week PnL</div><div class="value {tone}">{_inr(total_pnl)}</div></div>
    <div class="card"><div class="label">Wins / losses</div><div class="value">{wins} / {losses}</div></div>
    <div class="card"><div class="label">Trades</div><div class="value">{trades_n}</div></div>
    <div class="card"><div class="label">Sessions</div><div class="value">4</div></div>
  </div>
  <div class="grid2">
    <div class="panel"><h2>Cumulative PnL by day (₹)</h2><div class="body">
      <svg class="chart" viewBox="0 0 {w} {h}" role="img">
        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" stroke="#243041"/>
        <line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{w - pad_r}" y2="{pad_t + plot_h}" stroke="#243041"/>
        <line x1="{pad_l}" y1="{mid_y}" x2="{w - pad_r}" y2="{mid_y}" stroke="#3a4558" stroke-dasharray="4 4"/>
        <text x="{pad_l - 8}" y="{mid_y + 4}" fill="#8fa0b8" font-size="11" text-anchor="end">0</text>
        <polyline fill="none" stroke="#3d9cf0" stroke-width="2.5" points="{poly}"/>
        {"".join(circles)}{"".join(xlabels)}
      </svg>
    </div></div>
    <div class="panel"><h2>Daily PnL (₹)</h2><div class="body">
      <svg class="chart" viewBox="0 0 {bw} {bh}" role="img">
        <line x1="40" y1="{zero}" x2="{bw - 20}" y2="{zero}" stroke="#3a4558" stroke-dasharray="4 4"/>
        {"".join(bar_svg)}
      </svg>
    </div></div>
  </div>
  <div class="panel"><h2>Day summary</h2>
    <table><thead><tr><th>Day</th><th class="num">Spot</th><th class="num">Δ</th><th>Setups</th><th>Trades</th><th>W/L</th><th class="num">Day PnL</th><th class="num">Cum</th></tr></thead>
    <tbody>{"".join(day_table)}</tbody></table>
  </div>
  <div class="panel"><h2>All trades (chronological)</h2>
    <table><thead><tr><th>Day</th><th>Window</th><th>Algo</th><th>Side</th><th>Symbol</th><th class="num">Entry→Exit</th><th class="num">PnL</th><th>Reason</th></tr></thead>
    <tbody>{"".join(tr_html)}</tbody></table>
  </div>
  <p class="note">Per-day reports:
    <a href="backtest_2026-10-26_report.html">26</a> ·
    <a href="backtest_2026-10-27_report.html">27</a> ·
    <a href="backtest_2026-10-28_report.html">28</a> ·
    <a href="backtest_2026-10-29_report.html">29</a>
  </p>
</main></body></html>"""
    out = ROOT / "backtest_week_2026-10-26_to_29_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"Week PnL {_inr(total_pnl)}  W{wins}/L{losses}  trades={trades_n}")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()

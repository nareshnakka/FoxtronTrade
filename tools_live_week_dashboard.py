"""
Build a live minute-playback dashboard HTML for Mon→as-of (algo scan).

Runs causal aggressive backtest day-by-day, records each minute frame,
writes a self-contained interactive HTML report.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
)
from backtest_month_ledger import OUT, LedgerState, _inr, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides

IST = ZoneInfo("Asia/Kolkata")
MINUTES = Path("data/expiries/last_3_months/minutes")


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _session_days(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        p = MINUTES / f"nifty_{cur.isoformat()}.json"
        if p.exists() and p.stat().st_size > 50:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _hhmm_from_ts(ts: str | None, fallback_i: int) -> str:
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).strftime("%H:%M")
        except Exception:
            pass
    # 09:15 + i minutes
    total = 9 * 60 + 15 + fallback_i
    return f"{total // 60:02d}:{total % 60:02d}"


def build_day_frames(
    day: str,
    bars: list[dict],
    row: dict,
    trades: list[dict],
) -> list[dict]:
    """Walk minutes; attach timeline events + open-trade MTM from ledger fills."""
    timeline = list(row.get("timeline") or [])
    by_i: dict[int, list[dict]] = {}
    for ev in timeline:
        by_i.setdefault(int(ev.get("i") or 0), []).append(ev)

    # Map trades for open window
    trade_windows = []
    for t in trades:
        trade_windows.append(
            {
                "side": t.get("side"),
                "lots": int(t.get("lots") or 0),
                "entry_i": int(t.get("entry_i") or 0),
                "exit_i": int(t.get("exit_i") or 0),
                "entry_px": float(t.get("entry_px") or 0),
                "exit_px": float(t.get("exit_px") or 0),
                "net": float(t.get("net") or 0),
                "invested": float(t.get("invested") or 0),
                "reason": t.get("reason"),
                "event": t.get("event"),
                "note": t.get("note"),
            }
        )

    bal_open = float(row.get("balance_open") or 0)
    # Reconstruct free balance over time from completed trades
    realized = 0.0
    frames: list[dict] = []
    spot0 = float(bars[0]["close"]) if bars else 0.0
    delta, theta = 0.50, 0.015
    entry_mid = 135.0

    for i, bar in enumerate(bars):
        spot = float(bar["close"])
        tlabel = _hhmm_from_ts(bar.get("ts"), i)
        evs = by_i.get(i, [])
        phase = "SCAN"
        msg = ""
        side = None
        for ev in evs:
            et = str(ev.get("event") or "")
            if et in ("ENTRY", "SIGNAL", "WATCH", "SKIP", "WAIT_DECAY", "HALT"):
                phase = et
            if ev.get("reason"):
                msg = str(ev.get("reason"))
            if ev.get("side"):
                side = ev.get("side")
            if et == "EXIT":
                phase = "EXIT"

        open_t = None
        for tw in trade_windows:
            if tw["entry_i"] <= i < tw["exit_i"]:
                open_t = tw
                break
            if i == tw["exit_i"]:
                # count realized at exit bar
                realized += float(tw["net"])

        mtm = 0.0
        invested = 0.0
        lots = 0
        open_side = None
        if open_t:
            open_side = open_t["side"]
            lots = open_t["lots"]
            invested = open_t["invested"]
            # synth premium path (same model as backtest)
            ds = spot - float(bars[open_t["entry_i"]]["close"])
            held = float(i - open_t["entry_i"])
            if open_side == "CE":
                px = max(5.0, open_t["entry_px"] + delta * ds - theta * held)
            else:
                px = max(5.0, open_t["entry_px"] - delta * ds - theta * held)
            qty = lots * 75
            mtm = (px - open_t["entry_px"]) * qty
            phase = "IN_TRADE"
            msg = f"{open_side} {lots}L @ {open_t['entry_px']:.1f} · mtm {_inr(mtm)}"

        # free cash ≈ bal_open + realized_before_this_bar - invested_if_open
        # realized already includes exits at this bar; for open, subtract invested
        free = bal_open + realized - (invested if open_t else 0.0)
        # undo double-count: when we exit at i we added net which already returned capital
        equity = free + invested + mtm if open_t else (bal_open + realized)

        frames.append(
            {
                "i": i,
                "t": tlabel,
                "spot": round(spot, 2),
                "vs_open": round(spot - spot0, 1),
                "phase": phase,
                "side": open_side or side,
                "msg": msg[:140],
                "events": [
                    {
                        "event": e.get("event"),
                        "side": e.get("side"),
                        "reason": (e.get("reason") or "")[:100],
                        "net": e.get("net"),
                        "lots": e.get("lots"),
                    }
                    for e in evs
                ],
                "open": bool(open_t),
                "lots": lots,
                "invested": round(invested, 0),
                "mtm": round(mtm, 0),
                "equity": round(equity, 0),
                "realized": round(realized if not open_t else realized, 0),
            }
        )
    return frames


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=0.75)
    ap.add_argument(
        "--mode",
        choices=("week", "month", "days"),
        default="week",
        help="week=Mon→as-of, month=last ~31 calendar days, days=use --calendar-days",
    )
    ap.add_argument("--calendar-days", type=int, default=31)
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)
    if args.mode == "week":
        start = _monday_of(as_of)
    elif args.mode == "month":
        start = as_of - timedelta(days=max(1, int(args.calendar_days)) - 1)
    else:
        start = as_of - timedelta(days=max(1, int(args.calendar_days)) - 1)
    days = _session_days(start, as_of)
    if not days:
        print("No session days")
        return 1

    cfg = month_cfg()
    cfg.update(aggressive_overrides(args.capital, args.deploy))
    state = LedgerState(
        initial=float(args.capital),
        balance=float(args.capital),
        deploy_pct=float(args.deploy),
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(days[0])
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    print(
        f"Live {args.mode} dashboard {days[0]}→{days[-1]} · {len(days)} sessions · "
        f"₹{args.capital:,.0f} · causal minute (no lookahead)",
        flush=True,
    )

    payload_days: list[dict] = []
    for n, day in enumerate(days, 1):
        path = MINUTES / f"nifty_{day}.json"
        bars = _load_bars(path)
        ce0, pe0 = _day_prem_opens(day)
        print(f"  [{n}/{len(days)}] {day} bars={len(bars)} bal=₹{state.balance:,.0f}", flush=True)
        bal_before = state.balance
        ledger_before = len(state.ledger)
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=day,
            is_expiry_day=False,
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
        )
        decay_mem.finalize_day(day)
        day_trades = list(state.ledger[ledger_before:])
        # ensure entry_i/exit_i on trades (simulate already sets them)
        frames = build_day_frames(day, bars, row, day_trades)
        payload_days.append(
            {
                "date": day,
                "weekday": date.fromisoformat(day).strftime("%a"),
                "balance_open": round(bal_before, 2),
                "balance_close": round(float(row.get("balance_close") or state.balance), 2),
                "net": round(float(row.get("net") or 0), 2),
                "setups": int(row.get("setups") or 0),
                "wins": int(row.get("wins") or 0),
                "losses": int(row.get("losses") or 0),
                "spot_open": float(row.get("spot_open") or 0),
                "spot_close": float(row.get("spot_close") or 0),
                "trades": [
                    {
                        "side": t.get("side"),
                        "lots": t.get("lots"),
                        "entry_time": t.get("entry_time"),
                        "exit_time": t.get("exit_time"),
                        "net": t.get("net"),
                        "reason": t.get("reason"),
                        "event": t.get("event"),
                        "note": t.get("note"),
                    }
                    for t in day_trades
                ],
                "frames": frames,
            }
        )
        print(
            f"      net={_inr(row.get('net'))} setups={row.get('setups')} frames={len(frames)}",
            flush=True,
        )

    report = {
        "title": f"Live algo scan · {days[0]} → {days[-1]}",
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrack · post-decay · impulse OFF",
        "capital": {
            "initial": float(args.capital),
            "final": round(state.balance, 2),
            "net": round(state.balance - float(args.capital), 2),
            "return_pct": round(100.0 * (state.balance / float(args.capital) - 1.0), 2),
            "deploy_pct": float(args.deploy),
        },
        "days": payload_days,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    span = "week" if args.mode == "week" else ("month" if args.mode == "month" else "range")
    tag = f"live_{span}_{days[0]}_to_{days[-1]}"
    jp = OUT / f"{tag}.json"
    hp = OUT / f"{tag}_dashboard.html"
    jp.write_text(json.dumps(report), encoding="utf-8")
    hp.write_text(_html(report), encoding="utf-8")
    print(f"JSON {jp}")
    print(f"HTML {hp}")
    print(
        f"Net {_inr(report['capital']['net'])} ({report['capital']['return_pct']:+.1f}%) "
        f"→ ₹{report['capital']['final']:,.0f}"
    )
    return 0


def _html(report: dict) -> str:
    data = json.dumps(report)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{report['title']}</title>
<style>
:root {{
  --bg:#0b1220; --panel:#121a2b; --line:#243049; --text:#e8eefc; --muted:#8b9bb8;
  --good:#3dbea0; --bad:#e57373; --accent:#5b9fff; --warn:#e6b35a;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:"Segoe UI", system-ui, sans-serif;
  background:radial-gradient(1200px 600px at 10% -10%, #1a2744, var(--bg));
  color:var(--text); min-height:100vh;
}}
header {{
  display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center;
  padding:16px 22px; border-bottom:1px solid var(--line);
  background:rgba(18,26,43,.92); position:sticky; top:0; z-index:5;
}}
header h1 {{ font-size:1.05rem; margin:0; font-weight:650; letter-spacing:.02em; }}
.sub {{ color:var(--muted); font-size:.82rem; }}
.stats {{ display:flex; gap:14px; flex-wrap:wrap; margin-left:auto; }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:8px 12px; min-width:110px; }}
.stat .lbl {{ color:var(--muted); font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; }}
.stat .val {{ font-size:1.05rem; font-weight:700; margin-top:2px; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
main {{ display:grid; grid-template-columns: 1fr 320px; gap:14px; padding:14px 18px 28px; }}
@media (max-width:980px) {{ main {{ grid-template-columns:1fr; }} }}
.panel {{
  background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:12px 14px;
}}
.panel h2 {{ margin:0 0 8px; font-size:.92rem; font-weight:650; }}
.chart-wrap {{ position:relative; height:340px; }}
canvas {{ width:100%; height:100%; display:block; }}
.controls {{
  display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:10px;
}}
button, select {{
  background:#1c2740; color:var(--text); border:1px solid var(--line);
  border-radius:8px; padding:8px 12px; cursor:pointer; font-size:.85rem;
}}
button.primary {{ background:linear-gradient(135deg,#2a6df4,#5b9fff); border:none; font-weight:650; }}
button:hover {{ filter:brightness(1.08); }}
input[type=range] {{ width:140px; }}
.day-tabs {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }}
.day-tabs button {{ padding:6px 10px; font-size:.78rem; }}
.day-tabs button.active {{ outline:2px solid var(--accent); }}
.day-tabs button.g {{ border-color:rgba(61,190,160,.5); }}
.day-tabs button.r {{ border-color:rgba(229,115,115,.5); }}
.log {{
  height:280px; overflow:auto; font-family:ui-monospace, Consolas, monospace;
  font-size:.75rem; line-height:1.45; background:#0a101c; border-radius:10px;
  padding:10px; border:1px solid var(--line);
}}
.log .e {{ color:var(--accent); }} .log .x {{ color:var(--bad); }} .log .w {{ color:var(--warn); }}
.phase {{
  display:inline-block; padding:3px 8px; border-radius:999px; font-size:.72rem; font-weight:700;
  background:#1c2740; border:1px solid var(--line);
}}
.phase.IN_TRADE {{ background:rgba(61,190,160,.18); color:var(--good); }}
.phase.ENTRY {{ background:rgba(91,159,255,.2); color:var(--accent); }}
.phase.EXIT {{ background:rgba(229,115,115,.18); color:var(--bad); }}
.kv {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px; }}
.kv div {{ background:#0a101c; border-radius:8px; padding:8px; border:1px solid var(--line); }}
.kv .lbl {{ color:var(--muted); font-size:.68rem; }} .kv .val {{ font-weight:700; margin-top:2px; }}
.trades {{ font-size:.78rem; margin-top:8px; }}
.trades table {{ width:100%; border-collapse:collapse; }}
.trades td, .trades th {{ padding:4px 6px; border-bottom:1px solid var(--line); text-align:left; }}
.progress {{ height:6px; background:#0a101c; border-radius:99px; overflow:hidden; margin-top:8px; }}
.progress > i {{ display:block; height:100%; background:var(--accent); width:0%; }}
</style>
</head>
<body>
<header>
  <div>
    <h1 id="title"></h1>
    <div class="sub" id="subtitle"></div>
  </div>
  <div class="stats">
    <div class="stat"><div class="lbl">Equity</div><div class="val" id="eq">—</div></div>
    <div class="stat"><div class="lbl">Day P&amp;L</div><div class="val" id="dayPnL">—</div></div>
    <div class="stat"><div class="lbl">Week net</div><div class="val" id="weekNet">—</div></div>
    <div class="stat"><div class="lbl">Clock</div><div class="val" id="clock">09:15</div></div>
  </div>
</header>
<main>
  <section class="panel">
    <div class="day-tabs" id="dayTabs"></div>
    <h2>Nifty spot · 1-min algo scan playback</h2>
    <div class="chart-wrap"><canvas id="spotChart"></canvas></div>
    <div class="progress"><i id="prog"></i></div>
    <div class="controls">
      <button class="primary" id="btnPlay">Play</button>
      <button id="btnPause">Pause</button>
      <button id="btnReset">Reset day</button>
      <label class="sub">Speed <input type="range" id="speed" min="1" max="40" value="12"/> <span id="speedLbl">12×</span></label>
      <select id="daySelect"></select>
      <span class="phase" id="phase">SCAN</span>
      <span class="sub" id="msg"></span>
    </div>
    <div class="kv">
      <div><div class="lbl">Spot</div><div class="val" id="spot">—</div></div>
      <div><div class="lbl">vs open</div><div class="val" id="vsOpen">—</div></div>
      <div><div class="lbl">Open / invested</div><div class="val" id="openPos">Flat</div></div>
      <div><div class="lbl">MTM</div><div class="val" id="mtm">₹0</div></div>
    </div>
  </section>
  <aside>
    <div class="panel">
      <h2>Equity (week)</h2>
      <div class="chart-wrap" style="height:160px"><canvas id="eqChart"></canvas></div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h2>Live algo log</h2>
      <div class="log" id="log"></div>
      <div class="trades" id="trades"></div>
    </div>
  </aside>
</main>
<script>
const REPORT = {data};

const inr = (n, signed=true) => {{
  if (n == null || Number.isNaN(n)) return '—';
  const a = Math.abs(Math.round(n));
  const s = '₹' + a.toLocaleString('en-IN');
  if (!signed) return s;
  if (n > 0) return '+' + s;
  if (n < 0) return '−' + s;
  return '₹0';
}};

const days = REPORT.days || [];
let dayIdx = 0;
let frameIdx = 0;
let playing = false;
let timer = null;
let spotPts = [];
let eqDayPts = [];

const spotCanvas = document.getElementById('spotChart');
const eqCanvas = document.getElementById('eqChart');
const sctx = spotCanvas.getContext('2d');
const ectx = eqCanvas.getContext('2d');

function resize() {{
  for (const c of [spotCanvas, eqCanvas]) {{
    const r = c.parentElement.getBoundingClientRect();
    c.width = Math.floor(r.width * devicePixelRatio);
    c.height = Math.floor(r.height * devicePixelRatio);
  }}
  draw();
}}
window.addEventListener('resize', resize);

document.getElementById('title').textContent = REPORT.title;
document.getElementById('subtitle').textContent =
  REPORT.profile + ' · CAUSAL 1-min (no lookahead) · data→algo only · generated ' +
  (REPORT.generated_at || '').slice(0,19);
document.getElementById('weekNet').textContent = inr(REPORT.capital.net);
document.getElementById('weekNet').className = 'val ' + (REPORT.capital.net >= 0 ? 'good' : 'bad');

const tabs = document.getElementById('dayTabs');
const sel = document.getElementById('daySelect');
days.forEach((d,i) => {{
  const b = document.createElement('button');
  b.textContent = d.weekday + ' ' + d.date.slice(5);
  b.className = d.net > 0 ? 'g' : (d.net < 0 ? 'r' : '');
  b.onclick = () => selectDay(i, true);
  tabs.appendChild(b);
  const o = document.createElement('option');
  o.value = i; o.textContent = d.date + ' · ' + inr(d.net);
  sel.appendChild(o);
}});
sel.onchange = () => selectDay(+sel.value, true);

function selectDay(i, reset=false) {{
  dayIdx = i;
  [...tabs.children].forEach((b,j) => b.classList.toggle('active', j===i));
  sel.value = String(i);
  const d = days[i];
  document.getElementById('trades').innerHTML =
    '<table><thead><tr><th>Time</th><th>Side</th><th>Lots</th><th>Net</th></tr></thead><tbody>' +
    (d.trades||[]).map(t => `<tr><td>${{t.entry_time}}→${{t.exit_time}}</td><td>${{t.side}}</td><td>${{t.lots}}</td><td class="${{t.net>=0?'good':'bad'}}">${{inr(t.net)}}</td></tr>`).join('') +
    '</tbody></table>';
  if (reset) resetDay();
  else draw();
}}

function resetDay() {{
  frameIdx = 0; spotPts = []; eqDayPts = [];
  document.getElementById('log').innerHTML = '';
  updateUI(); draw();
}}

function speed() {{ return +document.getElementById('speed').value; }}
document.getElementById('speed').oninput = (e) => {{
  document.getElementById('speedLbl').textContent = e.target.value + '×';
  if (playing) {{ pause(); play(); }}
}};

function play() {{
  if (playing) return;
  playing = true;
  document.getElementById('btnPlay').textContent = 'Playing…';
  const tick = () => {{
    const d = days[dayIdx];
    if (!d || frameIdx >= d.frames.length) {{
      // auto-advance day
      if (dayIdx < days.length - 1) {{
        selectDay(dayIdx + 1, true);
        timer = setTimeout(tick, Math.max(20, 1000/speed()));
        return;
      }}
      pause();
      return;
    }}
    step();
    timer = setTimeout(tick, Math.max(15, 1000/speed()));
  }};
  tick();
}}
function pause() {{
  playing = false;
  document.getElementById('btnPlay').textContent = 'Play';
  if (timer) clearTimeout(timer);
  timer = null;
}}
document.getElementById('btnPlay').onclick = play;
document.getElementById('btnPause').onclick = pause;
document.getElementById('btnReset').onclick = () => {{ pause(); resetDay(); }};

function step() {{
  const d = days[dayIdx];
  const f = d.frames[frameIdx];
  if (!f) return;
  spotPts.push({{ t:f.t, y:f.spot, open:f.open }});
  eqDayPts.push({{ t:f.t, y:f.equity }});
  if (f.events && f.events.length) {{
    const log = document.getElementById('log');
    for (const e of f.events) {{
      const cls = e.event === 'ENTRY' ? 'e' : (e.event === 'EXIT' ? 'x' : 'w');
      const line = document.createElement('div');
      line.className = cls;
      line.textContent = `${{d.date}} ${{f.t}} ${{e.event}} ${{e.side||''}} ${{e.lots?e.lots+'L':''}} ${{e.net!=null?inr(e.net):''}} · ${{e.reason||''}}`;
      log.appendChild(line);
      log.scrollTop = log.scrollHeight;
    }}
  }}
  frameIdx++;
  updateUI();
  draw();
}}

function updateUI() {{
  const d = days[dayIdx];
  const f = d.frames[Math.max(0, frameIdx-1)] || d.frames[0];
  if (!f) return;
  document.getElementById('clock').textContent = f.t;
  document.getElementById('spot').textContent = f.spot.toFixed(2);
  document.getElementById('vsOpen').textContent = (f.vs_open>=0?'+':'') + f.vs_open;
  document.getElementById('eq').textContent = '₹' + Math.round(f.equity).toLocaleString('en-IN');
  const dayPnL = f.equity - d.balance_open;
  const el = document.getElementById('dayPnL');
  el.textContent = inr(dayPnL);
  el.className = 'val ' + (dayPnL >= 0 ? 'good' : 'bad');
  const ph = document.getElementById('phase');
  ph.textContent = f.phase;
  ph.className = 'phase ' + f.phase;
  document.getElementById('msg').textContent = f.msg || '';
  document.getElementById('openPos').textContent = f.open ? `${{f.side}} ${{f.lots}}L · inv ₹${{Math.round(f.invested).toLocaleString('en-IN')}}` : 'Flat';
  const m = document.getElementById('mtm');
  m.textContent = inr(f.mtm);
  m.className = 'val ' + (f.mtm >= 0 ? 'good' : 'bad');
  document.getElementById('prog').style.width = ((frameIdx / d.frames.length) * 100).toFixed(1) + '%';
}}

function drawChart(ctx, canvas, pts, color, markOpen=false) {{
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0,0,w,h);
  if (!pts.length) return;
  const ys = pts.map(p => p.y);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  if (ymax - ymin < 1) {{ ymin -= 1; ymax += 1; }}
  const pad = (ymax - ymin) * 0.08;
  ymin -= pad; ymax += pad;
  const L=40*devicePixelRatio, R=12*devicePixelRatio, T=12*devicePixelRatio, B=24*devicePixelRatio;
  const xAt = i => L + (i / Math.max(pts.length-1,1)) * (w-L-R);
  const yAt = v => T + (1 - (v - ymin)/(ymax-ymin)) * (h-T-B);
  // grid
  ctx.strokeStyle = 'rgba(139,155,184,.15)';
  ctx.lineWidth = 1;
  for (let g=0;g<4;g++) {{
    const y = T + g*(h-T-B)/3;
    ctx.beginPath(); ctx.moveTo(L,y); ctx.lineTo(w-R,y); ctx.stroke();
  }}
  // line
  ctx.beginPath();
  pts.forEach((p,i) => {{
    const x=xAt(i), y=yAt(p.y);
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }});
  ctx.strokeStyle = color; ctx.lineWidth = 2*devicePixelRatio; ctx.stroke();
  // open markers
  if (markOpen) {{
    pts.forEach((p,i) => {{
      if (!p.open) return;
      ctx.fillStyle = 'rgba(61,190,160,.9)';
      ctx.beginPath(); ctx.arc(xAt(i), yAt(p.y), 3*devicePixelRatio, 0, Math.PI*2); ctx.fill();
    }});
  }}
  // labels
  ctx.fillStyle = '#8b9bb8';
  ctx.font = `${{11*devicePixelRatio}}px sans-serif`;
  ctx.fillText(ymax.toFixed(0), 4, T+10*devicePixelRatio);
  ctx.fillText(ymin.toFixed(0), 4, h-B);
  if (pts.length) {{
    const last = pts[pts.length-1];
    ctx.fillStyle = color;
    ctx.fillText(String(Math.round(last.y)), w-R-60*devicePixelRatio, yAt(last.y)-4);
  }}
}}

function draw() {{
  drawChart(sctx, spotCanvas, spotPts, '#5b9fff', true);
  // week equity: day closes so far + current
  const weekEq = [];
  for (let i=0;i<dayIdx;i++) weekEq.push({{ t:days[i].date.slice(5), y:days[i].balance_close }});
  const cur = days[dayIdx].frames[Math.max(0,frameIdx-1)];
  if (cur) weekEq.push({{ t:days[dayIdx].date.slice(5), y:cur.equity }});
  else weekEq.push({{ t:days[dayIdx].date.slice(5), y:days[dayIdx].balance_open }});
  drawChart(ectx, eqCanvas, weekEq, '#3dbea0', false);
}}

selectDay(0, true);
resize();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())

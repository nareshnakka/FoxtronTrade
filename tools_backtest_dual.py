"""
Dual causal backtest: Nifty FastTrack + Sensex FastTrackSensex.

Each index is scanned with its own gates, capital sleeve, and ledger.
Sensex only arms on Tue/Wed (+ Thu if gamma blast). Off-days capital
stays conceptually with Nifty (Sensex sleeve idle).

Usage:
  .venv\\Scripts\\python.exe tools_backtest_dual.py --days 30 --as-of 2026-11-03
  .venv\\Scripts\\python.exe tools_backtest_dual.py --days 7 --capital 100000
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
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
from foxtrade.sensex_calendar import sensex_day_role
from tools_backtest_week import aggressive_overrides
from tools_train_sensex_fasttrack import load_sensex_days

IST = ZoneInfo("Asia/Kolkata")
SX_OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")
NIFTY_OVERRIDES = Path("data/nifty/fasttrack_profit_overrides.json")
NIFTY_MIN = Path("data/expiries/last_3_months/minutes")


def _nifty_cfg(capital: float, deploy: float) -> dict[str, Any]:
    cfg = month_cfg()
    cfg.update(aggressive_overrides(capital, deploy))
    if NIFTY_OVERRIDES.exists():
        cfg.update(json.loads(NIFTY_OVERRIDES.read_text(encoding="utf-8")))
    cfg["capital_inr"] = capital
    cfg["deploy_pct_of_balance"] = deploy
    cfg["underlying"] = "NIFTY"
    cfg["lot_size"] = 75
    cfg["profile_name"] = "FastTrack"
    cfg["algo_name"] = "FastTrack"
    return cfg


def _sensex_cfg(capital: float, deploy: float) -> dict[str, Any]:
    if SX_OVERRIDES.exists():
        cfg = json.loads(SX_OVERRIDES.read_text(encoding="utf-8"))
    else:
        cfg = aggressive_overrides(capital, deploy)
        cfg["lot_size"] = 20
        cfg["point_scale"] = 3.68
    cfg = deepcopy(cfg)
    cfg["capital_inr"] = capital
    cfg["deploy_pct_of_balance"] = deploy
    cfg["underlying"] = "SENSEX"
    cfg["profile_name"] = "FastTrackSensex"
    cfg["algo_name"] = "FastTrackSensex"
    cfg["lot_size"] = int(cfg.get("lot_size") or 20)
    cfg.setdefault("sensex_expiry_weekday", 1)
    cfg.setdefault("sensex_trade_weekdays", [1, 2])
    cfg.setdefault("sensex_conditional_weekdays", [3])
    cfg.setdefault("sensex_thursday_gamma_enabled", True)
    cfg.setdefault("sensex_thursday_gamma_only", True)
    cfg.setdefault("sensex_block_pre_expiry", True)
    return cfg


def _summarize(daily: list[dict], capital: float, state: LedgerState) -> dict[str, Any]:
    nets = [float(r.get("net") or 0) for r in daily]
    bals = [float(r.get("balance_close") or capital) for r in daily]
    peak = capital
    maxdd = 0.0
    for b in bals:
        peak = max(peak, b)
        maxdd = max(maxdd, peak - b)
    wins = sum(int(r.get("wins") or 0) for r in daily)
    losses = sum(int(r.get("losses") or 0) for r in daily)
    return {
        "sessions": len(daily),
        "traded": sum(1 for r in daily if int(r.get("setups") or 0) > 0),
        "setups": sum(int(r.get("setups") or 0) for r in daily),
        "wins": wins,
        "losses": losses,
        "net": round(state.balance - capital, 2),
        "final": round(state.balance, 2),
        "max_dd": round(maxdd, 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "gross": round(sum(float(r.get("gross") or 0) for r in daily), 2),
        "charges": round(sum(float(r.get("charges") or 0) for r in daily), 2),
        "lots": sum(int(r.get("lots_total") or 0) for r in daily),
        "invested_total": round(
            sum(float(r.get("invested_total") or 0) for r in daily), 2
        ),
    }


def _run_nifty(
    days: list[dict],
    cfg: dict[str, Any],
    capital: float,
    deploy: float,
    *,
    verbose: bool,
) -> dict[str, Any]:
    state = LedgerState(
        initial=capital, balance=capital, deploy_pct=deploy, deploy_cap=0.0
    )
    seed = _seed_decay_days_before(days[0]["date"]) if days else {}
    dm = DecayMemory(persist=False, seed_days=seed)
    daily = []
    for d in days:
        path = Path(d["minutes_path"])
        bars = _load_bars(path) if path.exists() else []
        if len(bars) < 40:
            continue
        ce0, pe0 = _day_prem_opens(d["date"])
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=dm,
            ce_open=ce0,
            pe_open=pe0,
            verbose=verbose,
        )
        dm.finalize_day(d["date"])
        row["underlying"] = "NIFTY"
        daily.append(row)
    summ = _summarize(daily, capital, state)
    return {**summ, "daily": daily, "ledger": list(state.ledger), "cfg": cfg}


def _run_sensex(
    days: list[dict],
    cfg: dict[str, Any],
    capital: float,
    deploy: float,
    *,
    verbose: bool,
) -> dict[str, Any]:
    state = LedgerState(
        initial=capital, balance=capital, deploy_pct=deploy, deploy_cap=0.0
    )
    dm = DecayMemory(persist=False, seed_days={})
    daily = []
    for d in days:
        bars = _load_bars(d["minutes_path"])
        if len(bars) < 40:
            continue
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=dm,
            verbose=verbose,
        )
        dm.finalize_day(d["date"])
        row["underlying"] = "SENSEX"
        row["day_role"] = d.get("day_role") or sensex_day_role(d["date"], cfg)
        daily.append(row)
    summ = _summarize(daily, capital, state)
    return {**summ, "daily": daily, "ledger": list(state.ledger), "cfg": cfg}


def _merge_calendar(
    nifty_daily: list[dict],
    sensex_daily: list[dict],
    *,
    nifty_cap: float,
    sensex_cap: float,
) -> list[dict]:
    by_n = {r["date"]: r for r in nifty_daily}
    by_s = {r["date"]: r for r in sensex_daily}
    dates = sorted(set(by_n) | set(by_s))
    n_bal = nifty_cap
    s_bal = sensex_cap
    out = []
    for day in dates:
        nr = by_n.get(day)
        sr = by_s.get(day)
        n_net = float((nr or {}).get("net") or 0)
        s_net = float((sr or {}).get("net") or 0)
        n_bal = float((nr or {}).get("balance_close") or n_bal)
        s_bal = float((sr or {}).get("balance_close") or s_bal)
        out.append(
            {
                "date": day,
                "is_expiry_day": bool(
                    (nr or {}).get("is_expiry_day") or (sr or {}).get("is_expiry_day")
                ),
                "nifty_net": n_net,
                "sensex_net": s_net,
                "net": round(n_net + s_net, 2),
                "nifty_setups": int((nr or {}).get("setups") or 0),
                "sensex_setups": int((sr or {}).get("setups") or 0),
                "setups": int((nr or {}).get("setups") or 0)
                + int((sr or {}).get("setups") or 0),
                "wins": int((nr or {}).get("wins") or 0)
                + int((sr or {}).get("wins") or 0),
                "losses": int((nr or {}).get("losses") or 0)
                + int((sr or {}).get("losses") or 0),
                "lots_total": int((nr or {}).get("lots_total") or 0)
                + int((sr or {}).get("lots_total") or 0),
                "gross": float((nr or {}).get("gross") or 0)
                + float((sr or {}).get("gross") or 0),
                "charges": float((nr or {}).get("charges") or 0)
                + float((sr or {}).get("charges") or 0),
                "invested_total": float((nr or {}).get("invested_total") or 0)
                + float((sr or {}).get("invested_total") or 0),
                "balance_close": round(n_bal + s_bal, 2),
                "spot_chg": float((nr or {}).get("spot_chg") or 0),
                "sensex_role": (sr or {}).get("day_role") or "",
                "trades": list((nr or {}).get("trades") or [])
                + list((sr or {}).get("trades") or []),
            }
        )
    return out


def write_dual_html(report: dict[str, Any], path: Path) -> None:
    cap = report["capital"]
    tot = report["totals"]
    books = report["books"]
    daily = report["daily"]
    rng = report.get("range") or {}
    n = books["NIFTY"]
    s = books["SENSEX"]

    rows = []
    for r in daily:
        net = float(r.get("net") or 0)
        cls = "g" if net > 0 else ("r" if net < 0 else "f")
        rows.append(
            f"""<tr class="{cls}">
            <td>{r['date']}</td>
            <td>{'EXP' if r.get('is_expiry_day') else ''}</td>
            <td class="muted">{r.get('sensex_role') or '—'}</td>
            <td class="num">{_inr(r.get('nifty_net'))}</td>
            <td class="num">{int(r.get('nifty_setups') or 0)}</td>
            <td class="num">{_inr(r.get('sensex_net'))}</td>
            <td class="num">{int(r.get('sensex_setups') or 0)}</td>
            <td class="num"><b>{_inr(net)}</b></td>
            <td class="num">₹{float(r.get('balance_close') or 0):,.0f}</td>
            </tr>"""
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Dual Book · Nifty + Sensex</title>
<style>
:root {{ --bg:#0f1419; --card:#171e27; --line:#243041; --text:#e7eef7; --muted:#8fa0b8;
  --good:#3dbea0; --bad:#e57373; --accent:#5b9fd4; }}
body {{ margin:0; font-family:"Segoe UI",system-ui,sans-serif; background:#0f1419; color:var(--text); }}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 18px 60px; }}
h1 {{ font-size:1.5rem; margin:0 0 6px; }}
.sub {{ color:var(--muted); margin-bottom:18px; font-size:0.92rem; }}
.grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }}
@media (max-width:800px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} }}
.card {{ background:#171e27; border:1px solid var(--line); border-radius:14px; padding:14px 16px; }}
.lbl {{ color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:.06em; }}
.val {{ font-size:1.25rem; font-weight:700; margin-top:4px; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px; margin-top:16px; }}
table {{ width:100%; border-collapse:collapse; font-size:0.86rem; }}
th,td {{ padding:8px; border-bottom:1px solid #223044; text-align:left; }}
th {{ color:var(--muted); font-size:0.72rem; text-transform:uppercase; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
tr.g td {{ background:rgba(61,190,160,.12); }}
tr.r td {{ background:rgba(229,115,115,.12); }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; font-size:0.72rem;
  background:#243246; color:var(--accent); margin-right:6px; }}
.muted {{ color:var(--muted); }}
</style></head><body><div class="wrap">
<h1>Dual Book — Nifty FastTrack + Sensex FastTrackSensex</h1>
<div class="sub">
  <span class="badge">Separate ledgers</span>
  <span class="badge">Sensex Tue/Wed + Thuγ</span>
  <span class="badge">₹{cap['nifty_initial']:,.0f} Nifty · ₹{cap['sensex_initial']:,.0f} Sensex</span>
  <br/>Range {rng.get('start')} → {rng.get('end')} · Generated {report.get('generated_at','')[:19]}
</div>
<div class="grid">
  <div class="card"><div class="lbl">Combined final</div><div class="val">₹{cap['final_balance']:,.0f}</div></div>
  <div class="card"><div class="lbl">Combined net</div><div class="val {'good' if tot['net']>=0 else 'bad'}">{_inr(tot['net'])} ({tot['return_pct']:+.1f}%)</div></div>
  <div class="card"><div class="lbl">Nifty sleeve</div><div class="val {'good' if n['net']>=0 else 'bad'}">{_inr(n['net'])}</div></div>
  <div class="card"><div class="lbl">Sensex sleeve</div><div class="val {'good' if s['net']>=0 else 'bad'}">{_inr(s['net'])}</div></div>
  <div class="card"><div class="lbl">W/L combined</div><div class="val">{tot['wins']}/{tot['losses']}</div></div>
  <div class="card"><div class="lbl">Nifty cov</div><div class="val">{n['traded']}/{n['sessions']}</div></div>
  <div class="card"><div class="lbl">Sensex cov</div><div class="val">{s['traded']}/{s['sessions']}</div></div>
  <div class="card"><div class="lbl">Max DD (sum sleeves)</div><div class="val">₹{tot['max_dd']:,.0f}</div></div>
</div>
<div class="panel">
<h2 style="margin:0 0 12px;font-size:1.05rem;">Per-day dual scan</h2>
<table>
<thead><tr>
<th>Date</th><th></th><th>SX role</th>
<th>Nifty P&amp;L</th><th>N set</th>
<th>Sensex P&amp;L</th><th>S set</th>
<th>Combined</th><th>Dual equity</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody></table>
</div>
<div class="panel muted" style="font-size:0.85rem;">
Nifty profile: {n.get('profile')} · Sensex profile: {s.get('profile')} ·
Each index analyzed on its own spot path; Sensex off Mon/Fri (and Thu without gamma).
</div>
</div></body></html>"""
    path.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dual Nifty+Sensex causal backtest")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--as-of", default=datetime.now(IST).date().isoformat())
    ap.add_argument(
        "--capital",
        type=float,
        default=100_000,
        help="capital PER sleeve (Nifty and Sensex each get this)",
    )
    ap.add_argument("--deploy", type=float, default=1.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)
    capital = float(args.capital)
    deploy = float(args.deploy)

    nifty_days = _load_month_days(as_of=as_of, calendar_days=int(args.days))
    sx_cfg_probe = _sensex_cfg(capital, deploy)
    sensex_days = load_sensex_days(
        as_of, calendar_days=int(args.days), trade_days_only=True, cfg=sx_cfg_probe
    )
    if not nifty_days and not sensex_days:
        print("No session data in range")
        return 1

    print(
        f"DUAL BT {args.days}d → {as_of} · "
        f"Nifty sess={len(nifty_days)} Sensex eligible={len(sensex_days)} · "
        f"₹{capital:,.0f}/sleeve deploy={deploy:.0%}",
        flush=True,
    )

    n_cfg = _nifty_cfg(capital, deploy)
    s_cfg = _sensex_cfg(capital, deploy)

    print("--- NIFTY FastTrack", flush=True)
    nifty = _run_nifty(nifty_days, n_cfg, capital, deploy, verbose=args.verbose)
    print(
        f"  net={nifty['net']:+,.0f} W/L {nifty['wins']}/{nifty['losses']} "
        f"cov {nifty['traded']}/{nifty['sessions']} dd={nifty['max_dd']:,.0f}",
        flush=True,
    )

    print("--- SENSEX FastTrackSensex (Tue/Wed/Thuγ)", flush=True)
    sensex = _run_sensex(sensex_days, s_cfg, capital, deploy, verbose=args.verbose)
    print(
        f"  net={sensex['net']:+,.0f} W/L {sensex['wins']}/{sensex['losses']} "
        f"cov {sensex['traded']}/{sensex['sessions']} dd={sensex['max_dd']:,.0f}",
        flush=True,
    )

    combined_net = nifty["net"] + sensex["net"]
    combined_final = nifty["final"] + sensex["final"]
    combined_dd = nifty["max_dd"] + sensex["max_dd"]
    merged = _merge_calendar(
        nifty["daily"],
        sensex["daily"],
        nifty_cap=capital,
        sensex_cap=capital,
    )

    print()
    print(
        f"COMBINED net={combined_net:+,.0f} "
        f"({100 * combined_net / (2 * capital):+.1f}% on ₹{2*capital:,.0f}) "
        f"final ₹{combined_final:,.0f} "
        f"W/L {nifty['wins']+sensex['wins']}/{nifty['losses']+sensex['losses']}"
    )
    for r in merged:
        if int(r.get("setups") or 0) or abs(float(r.get("net") or 0)) > 1:
            print(
                f"  {r['date']}  N={float(r['nifty_net']):+,.0f}({r['nifty_setups']})  "
                f"S={float(r['sensex_net']):+,.0f}({r['sensex_setups']})  "
                f"Σ={float(r['net']):+,.0f}  {r.get('sensex_role') or ''}"
            )

    start = min(
        ([d["date"] for d in nifty_days] + [d["date"] for d in sensex_days]),
        default=as_of.isoformat(),
    )
    end = max(
        ([d["date"] for d in nifty_days] + [d["date"] for d in sensex_days]),
        default=as_of.isoformat(),
    )

    rep = {
        "title": f"Dual Book · Last {args.days}d · {start}→{end}",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "dual_nifty_sensex_separate_ledgers",
        "range": {"start": start, "end": end},
        "capital": {
            "nifty_initial": capital,
            "sensex_initial": capital,
            "initial": 2 * capital,
            "deploy_pct": deploy,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": combined_final,
            "net_pnl": combined_net,
            "return_pct": round(100 * combined_net / (2 * capital), 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            "session_days": len(merged),
            "traded_days": sum(1 for r in merged if int(r.get("setups") or 0) > 0),
            "setups": nifty["setups"] + sensex["setups"],
            "wins": nifty["wins"] + sensex["wins"],
            "losses": nifty["losses"] + sensex["losses"],
            "net": combined_net,
            "green_days": sum(1 for r in merged if float(r.get("net") or 0) > 0),
            "red_days": sum(1 for r in merged if float(r.get("net") or 0) < 0),
            "max_dd": combined_dd,
            "gross": nifty["gross"] + sensex["gross"],
            "charges": nifty["charges"] + sensex["charges"],
            "lots": nifty["lots"] + sensex["lots"],
            "invested_total": nifty["invested_total"] + sensex["invested_total"],
            "return_pct": round(100 * combined_net / (2 * capital), 2),
        },
        "books": {
            "NIFTY": {
                "profile": "FastTrack",
                "net": nifty["net"],
                "final": nifty["final"],
                "wins": nifty["wins"],
                "losses": nifty["losses"],
                "traded": nifty["traded"],
                "sessions": nifty["sessions"],
                "max_dd": nifty["max_dd"],
                "setups": nifty["setups"],
            },
            "SENSEX": {
                "profile": "FastTrackSensex",
                "net": sensex["net"],
                "final": sensex["final"],
                "wins": sensex["wins"],
                "losses": sensex["losses"],
                "traded": sensex["traded"],
                "sessions": sensex["sessions"],
                "max_dd": sensex["max_dd"],
                "setups": sensex["setups"],
            },
        },
        "daily": merged,
        "nifty_daily": nifty["daily"],
        "sensex_daily": sensex["daily"],
        "config_snapshot": {
            "dual_mode": True,
            "nifty_algo": "FastTrack",
            "sensex_algo": s_cfg.get("algo_name"),
            "sensex_trade_weekdays": s_cfg.get("sensex_trade_weekdays"),
            "sensex_best": (
                json.loads(
                    Path("data/sensex/fasttrack_sensex_profitmax_best.json").read_text(
                        encoding="utf-8"
                    )
                ).get("best_name")
                if Path("data/sensex/fasttrack_sensex_profitmax_best.json").exists()
                else None
            ),
        },
    }

    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"dual_{start}_to_{end}"
    jp = OUT / f"{tag}_report.json"
    hp = OUT / f"{tag}_report.html"
    # per-sleeve detailed reports too
    n_rep = {
        "title": f"Nifty sleeve · {start}→{end}",
        "generated_at": rep["generated_at"],
        "profile": "FastTrack",
        "range": rep["range"],
        "capital": {
            "initial": capital,
            "deploy_pct": deploy,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": nifty["final"],
            "net_pnl": nifty["net"],
            "return_pct": round(100 * nifty["net"] / capital, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            **{k: nifty[k] for k in (
                "sessions", "traded", "setups", "wins", "losses", "net",
                "green", "red", "gross", "charges", "lots", "invested_total",
            )},
            "session_days": nifty["sessions"],
            "traded_days": nifty["traded"],
            "green_days": nifty["green"],
            "red_days": nifty["red"],
            "max_dd": nifty["max_dd"],
            "post_decay_entries": 0,
            "minutes_processed": 0,
            "analyses": 0,
            "signal_minutes": 0,
        },
        "daily": nifty["daily"],
        "ledger": nifty["ledger"],
        "config_snapshot": {"profile_name": "FastTrack", "underlying": "NIFTY"},
    }
    s_rep = {
        "title": f"Sensex sleeve · {start}→{end}",
        "generated_at": rep["generated_at"],
        "profile": "FastTrackSensex",
        "range": {
            "start": sensex_days[0]["date"] if sensex_days else start,
            "end": sensex_days[-1]["date"] if sensex_days else end,
        },
        "capital": {
            "initial": capital,
            "deploy_pct": deploy,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": sensex["final"],
            "net_pnl": sensex["net"],
            "return_pct": round(100 * sensex["net"] / capital, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            **{k: sensex[k] for k in (
                "sessions", "traded", "setups", "wins", "losses", "net",
                "green", "red", "gross", "charges", "lots", "invested_total",
            )},
            "session_days": sensex["sessions"],
            "traded_days": sensex["traded"],
            "green_days": sensex["green"],
            "red_days": sensex["red"],
            "max_dd": sensex["max_dd"],
            "post_decay_entries": 0,
            "minutes_processed": 0,
            "analyses": 0,
            "signal_minutes": 0,
        },
        "daily": sensex["daily"],
        "ledger": sensex["ledger"],
        "config_snapshot": {
            "profile_name": "FastTrackSensex",
            "underlying": "SENSEX",
        },
    }

    jp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    write_dual_html(rep, hp)
    np = OUT / f"{tag}_nifty_report.html"
    sp = OUT / f"{tag}_sensex_report.html"
    write_detailed_html(n_rep, np)
    write_detailed_html(s_rep, sp)
    print(f"DUAL HTML {hp}")
    print(f"NIFTY HTML {np}")
    print(f"SENSEX HTML {sp}")
    print(f"JSON {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

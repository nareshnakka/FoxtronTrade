"""
1-month causal backtest: crush→bounce only (blockbuster thesis).

Setup: CE/PE high ~350–500+ over 3–5 days → crushed into ~90–145 buy zone
→ expect +50% to +200% bounce. Impulse/DIR off.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
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

IST = ZoneInfo("Asia/Kolkata")


def overrides(capital: float, deploy: float) -> dict:
    return {
        "directional_min_confidence": 0.99,  # starve DIR; post-decay owns entries
        "max_trades_per_day": 2,
        "bank_after_win": False,
        "post_decay_entry_enabled": True,
        "post_decay_standalone_entry": True,
        "post_decay_prefer_over_mkt": True,
        "post_decay_only_entries": True,
        "post_decay_respect_session_delay": False,
        "post_decay_min_bar": 0,
        "post_decay_confirm_minutes": 0,
        "post_decay_min_confidence": 0.62,
        "post_decay_pe_crush_pct": 60.0,
        "post_decay_pe_low_max": 135.0,
        "post_decay_high_min": 350.0,
        "post_decay_entry_low": 90.0,
        "post_decay_entry_high": 145.0,
        "post_decay_crush_lookback_days": 5,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
        "stop_loss_pct": 5.0,
        "target_pct_low": 15.0,  # blockbuster: aim bigger first target
        "target_pct_high": 200.0,
        "trail_giveback_pct": 8.0,
        "lock_profit_pct": 2.0,
        "exit_at_first_target": False,
        "max_lots_per_trade": 20,
        "min_lots_per_trade": 2,
        "deploy_pct_of_balance": float(deploy),
        "capital_inr": float(capital),
        "max_daily_loss_pct": 0,
        "max_weekly_loss_pct": 0,
        "max_monthly_loss_pct": 0,
        "max_losses_before_halt": 99,
        "premium_low": 90,
        "premium_high": 145,
    }


def main() -> int:
    as_of = date(2026, 10, 30)
    capital = 100_000.0
    deploy = 0.75
    days = _load_month_days(as_of=as_of, calendar_days=31, all_available=False)
    if not days:
        print("no days")
        return 1

    cfg = month_cfg()
    cfg.update(overrides(capital, deploy))
    state = LedgerState(
        initial=capital, balance=capital, deploy_pct=deploy, deploy_cap=0.0
    )
    seed = _seed_decay_days_before(days[0]["date"])
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    print(
        f"Blockbuster crush→bounce · {days[0]['date']}→{days[-1]['date']} · "
        f"{len(days)} sessions · high≥350 → buy 90–145 · causal",
        flush=True,
    )
    print(f"  decay seed {len(seed)} prior days", flush=True)

    daily = []
    for n, d in enumerate(days, 1):
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        print(
            f"  [{n}/{len(days)}] {d['date']} bal=₹{state.balance:,.0f}",
            flush=True,
        )
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
        )
        decay_mem.finalize_day(d["date"])
        daily.append(row)
        print(
            f"      net={_inr(row.get('net'))} setups={row.get('setups')} "
            f"post_decay={row.get('post_decay_entries')}",
            flush=True,
        )

    ledger = list(state.ledger)
    bb = [
        t
        for t in ledger
        if "post_decay" in str(t.get("event") or "")
        or "crush" in str(t.get("note") or "").lower()
        or "post-decay" in str(t.get("note") or "").lower()
    ]
    wins = [t for t in ledger if float(t.get("net") or 0) > 0]
    losses = [t for t in ledger if float(t.get("net") or 0) <= 0]
    big = [t for t in wins if float(t.get("mult") or 0) >= 1.5]

    tot_net = sum(float(r.get("net") or 0) for r in daily)
    rep = {
        "title": "Blockbuster crush→bounce · 1 month causal",
        "thesis": (
            "When CE/PE decays from high zone (≥350, often 400–500) over 3–5 days "
            "into ~90–145, buy the crushed side for estimated +50% to +200% bounce."
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_minute_post_decay_only",
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": capital,
            "deploy_pct": deploy,
            "halt_pct": getattr(state, "halt_pct", 0),
            "halt_floor": getattr(state, "halt_floor", 0),
            "final_balance": round(state.balance, 2),
            "net_pnl": round(state.balance - capital, 2),
            "return_pct": round(100.0 * (state.balance / capital - 1.0), 2),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "totals": {
            "session_days": len(daily),
            "traded_days": sum(1 for r in daily if int(r.get("setups") or 0) > 0),
            "setups": sum(int(r.get("setups") or 0) for r in daily),
            "post_decay_entries": sum(int(r.get("post_decay_entries") or 0) for r in daily),
            "lots": sum(int(r.get("lots_total") or 0) for r in daily),
            "invested_total": round(sum(float(t.get("invested") or 0) for t in ledger), 2),
            "wins": len(wins),
            "losses": len(losses),
            "blockbuster_ge_50pct": len(big),
            "gross": round(sum(float(r.get("gross") or 0) for r in daily), 2),
            "charges": round(sum(float(r.get("charges") or 0) for r in daily), 2),
            "net": round(tot_net, 2),
            "green_days": sum(1 for r in daily if float(r.get("net") or 0) > 0),
            "red_days": sum(1 for r in daily if float(r.get("net") or 0) < 0),
            "minutes_processed": sum(int(r.get("minutes") or 0) for r in daily),
            "analyses": sum(int(r.get("analyses") or 0) for r in daily),
            "signal_minutes": sum(int(r.get("allow_true") or 0) for r in daily),
        },
        "config_snapshot": {
            "post_decay_high_min": 350,
            "post_decay_entry_band": "90–145",
            "post_decay_crush_pct": 60,
            "lookback_days": 5,
            "post_decay_only_entries": True,
            "impulse_entry_enabled": False,
            "target_pct_high": 200,
            "profile": "blockbuster_crush_bounce",
        },
        "daily": daily,
        "ledger": ledger,
        "blockbuster_hits": [
            {
                "day": t.get("day"),
                "side": t.get("side"),
                "entry": t.get("entry_time"),
                "exit": t.get("exit_time"),
                "mult": t.get("mult"),
                "net": t.get("net"),
                "note": t.get("note"),
                "reason": t.get("reason"),
            }
            for t in big
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"blockbuster_{days[0]['date']}_to_{days[-1]['date']}"
    jp = OUT / f"{tag}_algo_report.json"
    hp = OUT / f"{tag}_algo_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)

    # Extra thesis summary HTML
    sp = OUT / f"{tag}_summary.html"
    rows = "".join(
        f"<tr class={'g' if float(t.get('net') or 0)>0 else 'r'}>"
        f"<td>{t.get('day')}</td><td>{t.get('side')}</td>"
        f"<td>{t.get('entry_time')}→{t.get('exit_time')}</td>"
        f"<td>{t.get('lots')}</td><td>{float(t.get('mult') or 0):.2f}×</td>"
        f"<td>{_inr(t.get('net'))}</td>"
        f"<td class='muted'>{(t.get('note') or t.get('reason') or '')[:120]}</td></tr>"
        for t in ledger
    )
    day_rows = "".join(
        f"<tr class={'g' if float(r.get('net') or 0)>0 else ('r' if float(r.get('net') or 0)<0 else 'f')}>"
        f"<td>{r.get('date')}</td><td>{_inr(r.get('net'))}</td>"
        f"<td>{r.get('setups')}</td><td>{r.get('post_decay_entries')}</td>"
        f"<td>₹{float(r.get('balance_close') or 0):,.0f}</td></tr>"
        for r in daily
    )
    sp.write_text(
        f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>{rep['title']}</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0b1220;color:#e8eefc;margin:24px}}
h1{{font-size:1.3rem}} .muted{{color:#8b9bb8}} .g{{color:#3dbea0}} .r{{color:#e57373}}
.card{{background:#121a2b;border:1px solid #243049;border-radius:12px;padding:14px;margin:12px 0}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
td,th{{padding:6px 8px;border-bottom:1px solid #243049;text-align:left}}
.stat{{display:inline-block;margin:6px 12px 6px 0;padding:10px 14px;background:#0a101c;border-radius:10px}}
.stat b{{display:block;font-size:1.1rem}}
</style></head><body>
<h1>{rep['title']}</h1>
<p class="muted">{rep['thesis']}</p>
<p class="muted">Causal 1-min · no lookahead · post-decay only · {rep['range']['start']} → {rep['range']['end']}</p>
<div class="card">
  <div class="stat">Net <b class="{'g' if rep['capital']['net_pnl']>=0 else 'r'}">{_inr(rep['capital']['net_pnl'])} ({rep['capital']['return_pct']:+.1f}%)</b></div>
  <div class="stat">Final <b>₹{rep['capital']['final_balance']:,.0f}</b></div>
  <div class="stat">Trades <b>{rep['totals']['setups']}</b></div>
  <div class="stat">W/L <b>{rep['totals']['wins']}/{rep['totals']['losses']}</b></div>
  <div class="stat">G/R days <b>{rep['totals']['green_days']}/{rep['totals']['red_days']}</b></div>
  <div class="stat">≥50% hits <b>{rep['totals']['blockbuster_ge_50pct']}</b></div>
</div>
<div class="card"><h2>Day flow</h2>
<table><thead><tr><th>Day</th><th>Net</th><th>Setups</th><th>Post-decay</th><th>Balance</th></tr></thead>
<tbody>{day_rows}</tbody></table></div>
<div class="card"><h2>All crush→bounce trades</h2>
<table><thead><tr><th>Day</th><th>Side</th><th>Time</th><th>Lots</th><th>Mult</th><th>Net</th><th>Note</th></tr></thead>
<tbody>{rows or '<tr><td colspan=7 class=muted>No trades</td></tr>'}</tbody></table></div>
<p class="muted">Full minute timeline: <a href="{hp.name}" style="color:#5b9fff">{hp.name}</a></p>
</body></html>""",
        encoding="utf-8",
    )

    print()
    print(f"Net {_inr(rep['capital']['net_pnl'])} ({rep['capital']['return_pct']:+.1f}%)")
    print(
        f"Setups {rep['totals']['setups']} W/L {rep['totals']['wins']}/{rep['totals']['losses']} "
        f"≥1.5× hits {rep['totals']['blockbuster_ge_50pct']}"
    )
    for t in ledger:
        print(
            f"  {t.get('day')} {t.get('entry_time')}→{t.get('exit_time')} "
            f"{t.get('side')} {t.get('lots')}L {float(t.get('mult') or 0):.2f}× "
            f"{_inr(t.get('net'))} · {(t.get('note') or '')[:80]}"
        )
    print(f"SUMMARY {sp}")
    print(f"DETAIL  {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

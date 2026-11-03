"""Backtest Sensex FastTrackSensex (quality overrides) for last N days."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_train_sensex_fasttrack import load_sensex_days, run_window, scale_point_cfg

IST = ZoneInfo("Asia/Kolkata")
OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--as-of", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=1.0)
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)

    cal_cfg = {
        "sensex_expiry_weekday": 1,
        "sensex_trade_weekdays": [1, 2],
        "sensex_conditional_weekdays": [3],
        "sensex_thursday_gamma_enabled": True,
        "sensex_thursday_gamma_only": True,
        "sensex_gamma_detect_enabled": True,
        "sensex_block_pre_expiry": True,
        "sensex_pre_expiry_weekday": 0,
    }
    # Full calendar window first, then Tue/Wed + Thu gamma slot
    days_all = load_sensex_days(as_of, calendar_days=args.days, cfg=cal_cfg)
    days = load_sensex_days(
        as_of, calendar_days=args.days, trade_days_only=True, cfg=cal_cfg
    )
    if not days:
        print(
            f"No Sensex Tue/Wed/Thu sessions in last {args.days}d "
            f"(saw {len(days_all)} session files)"
        )
        return 1

    if OVERRIDES.exists():
        cfg = json.loads(OVERRIDES.read_text(encoding="utf-8"))
        cfg["capital_inr"] = args.capital
        cfg["deploy_pct_of_balance"] = args.deploy
        cfg.update(cal_cfg)
        src = "gamma_overrides" if cfg.get("enable_orb_breakout") else "sensex_overrides"
    else:
        base = aggressive_overrides(args.capital, args.deploy)
        cfg = scale_point_cfg(base, 3.68)
        cfg.update(cal_cfg)
        src = "scale_3.68_fallback"

    print(
        f"Sensex {src} Tue/Wed+Thuγ {days[0]['date']}→{days[-1]['date']} "
        f"n={len(days)}/{len(days_all)} sess ₹{args.capital:,.0f} "
        f"deploy={args.deploy:.0%}",
        flush=True,
    )
    full = run_window(days, cfg, args.capital, args.deploy, verbose=True)
    wr = full["wins"] / max(1, full["wins"] + full["losses"])
    print()
    print(
        f"Sessions {full['sessions']} traded {full['traded']} "
        f"W/L {full['wins']}/{full['losses']} WR={100*wr:.0f}% "
        f"G/R {full['green']}/{full['red']}"
    )
    print(
        f"Net {full['net']:+,.0f} ({100*full['net']/args.capital:+.1f}%) "
        f"Final ₹{full['final']:,.0f} maxDD ₹{full['max_dd']:,.0f}"
    )
    for r in full["daily"]:
        role = next(
            (d.get("day_role") for d in days if d["date"] == r.get("date")),
            "",
        )
        print(
            f"  {r['date']}  net={float(r.get('net') or 0):+,.0f}  "
            f"setups={r.get('setups')}  "
            f"{role or ('EXP' if r.get('is_expiry_day') else '')}"
        )

    rep = {
        "title": (
            f"FastTrackSensex GAMMA Tue/Wed · Last {args.days}d · "
            f"{days[0]['date']}→{days[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "calendar": cal_cfg,
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": args.capital,
            "deploy_pct": args.deploy,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": full["final"],
            "net_pnl": full["net"],
            "return_pct": round(100 * full["net"] / args.capital, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            "session_days": full["sessions"],
            "traded_days": full["traded"],
            "setups": full["setups"],
            "wins": full["wins"],
            "losses": full["losses"],
            "net": full["net"],
            "green_days": full["green"],
            "red_days": full["red"],
            "gross": 0,
            "charges": 0,
            "lots": 0,
            "invested_total": 0,
            "post_decay_entries": 0,
            "minutes_processed": 0,
            "analyses": 0,
            "signal_minutes": 0,
        },
        "daily": full["daily"],
        "ledger": full["ledger"],
        "config_snapshot": {
            "profile_name": cfg.get("profile_name"),
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "enable_daily_pa": cfg.get("enable_daily_pa"),
            "enable_sr_reclaim": cfg.get("enable_sr_reclaim"),
            "lot_size": cfg.get("lot_size"),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"sensex_week_{days[0]['date']}_to_{days[-1]['date']}"
    jp = OUT / f"{tag}_report.json"
    hp = OUT / f"{tag}_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

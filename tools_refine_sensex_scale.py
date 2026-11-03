"""Refine Sensex FastTrack point scale around the walk-forward winner."""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_train_sensex_fasttrack import (
    ANALYSIS,
    BEST,
    OVERRIDES,
    load_sensex_days,
    run_window,
    scale_point_cfg,
)

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    base = aggressive_overrides(100_000, 1.0)
    base["post_decay_entry_enabled"] = False
    base["post_decay_standalone_entry"] = False
    as_of = date(2026, 11, 3)
    days = load_sensex_days(as_of)
    cut = int(len(days) * 0.70)
    train, hold = days[:cut], days[cut:]
    best = None
    for s in (3.5, 3.6, 3.68, 3.8, 3.9, 4.0, 4.3):
        cfg = scale_point_cfg(base, s)
        tr = run_window(train, cfg, 100_000, 1.0)
        ho = run_window(hold, cfg, 100_000, 1.0)
        score = ho["net"] - 0.35 * ho["max_dd"]
        print(
            f"scale={s} train={tr['net']:+.0f} hold={ho['net']:+.0f} "
            f"dd={ho['max_dd']:.0f} score={score:.0f} "
            f"wl={ho['wins']}/{ho['losses']}"
        )
        row = (score, s, cfg, tr, ho)
        if best is None or score > best[0]:
            best = row
    assert best is not None
    print("WINNER", best[1], "score", best[0])
    cfg = best[2]
    full = run_window(days, cfg, 100_000, 1.0)
    print(
        f"FULL net={full['net']:+.0f} W/L {full['wins']}/{full['losses']} "
        f"cov {full['traded']}/{full['sessions']} dd={full['max_dd']:.0f}"
    )
    OVERRIDES.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    BEST.write_text(
        json.dumps(
            {
                "best_scale": best[1],
                "hold": {k: best[4][k] for k in best[4] if k not in ("daily", "ledger")},
                "full": {k: full[k] for k in full if k not in ("daily", "ledger")},
                "analysis": analysis,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    rep = {
        "title": (
            f"FastTrackSensex scale={best[1]} · "
            f"{days[0]['date']}→{days[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "mode": "causal_sensex_synth_atm",
        "premium_model": (
            "Synthetic ATM from spot delta "
            "(expired Sensex option history unavailable on Kite)"
        ),
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": 100_000,
            "deploy_pct": 1.0,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": full["final"],
            "net_pnl": full["net"],
            "return_pct": round(100 * full["net"] / 100_000, 2),
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
            "profile_name": "FastTrackSensex",
            "point_scale": best[1],
            "lot_size": cfg.get("lot_size"),
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "pe_min_vs_open": cfg.get("pe_min_vs_open"),
            "orb_min_range_pts": cfg.get("orb_min_range_pts"),
            "orb_max_range_pts": cfg.get("orb_max_range_pts"),
        },
    }
    jp = OUT / "sensex_fasttrack_full_report.json"
    hp = OUT / "sensex_fasttrack_full_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)
    print("HTML", hp)
    print("OVERRIDES", OVERRIDES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

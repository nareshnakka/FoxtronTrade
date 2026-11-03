"""
Retrain Sensex for Tue/Wed only + Tuesday gamma blast.

Calendar:
  • Trade days: Tuesday, Wednesday
  • Expiry: Tuesday → Monday is hard cutoff (excluded)
  • Focus: expiry gamma (ORB / post-decay / SR reclaim / impulse mom)

Score = holdout_net − 0.28*max_dd + 900*(WR−0.5)*trades
        − 450*red + 180*green + 40*traded   (want coverage on gamma days)
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_train_sensex_fasttrack import (
    load_sensex_days,
    run_window,
    scale_point_cfg,
)

IST = ZoneInfo("Asia/Kolkata")
OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")
BEST = Path("data/sensex/fasttrack_sensex_gamma_best.json")
ANALYSIS = Path("data/sensex/momentum_analysis.json")

CAL = {
    "sensex_expiry_weekday": 1,
    "sensex_trade_weekdays": [1, 2],
    "sensex_conditional_weekdays": [3],
    "sensex_thursday_gamma_enabled": True,
    "sensex_thursday_gamma_only": True,
    "sensex_gamma_detect_enabled": True,
    "sensex_block_pre_expiry": True,
    "sensex_pre_expiry_weekday": 0,
}


def score_pair(tr: dict, ho: dict) -> float:
    """Prefer hold profit but punish train blow-ups (overfit guard)."""
    trades = max(1, ho["wins"] + ho["losses"])
    wr = ho["wins"] / trades
    train_pen = min(0.0, tr["net"]) * 0.45  # only penalize train reds
    return (
        0.35 * tr["net"]
        + ho["net"]
        + train_pen
        - 0.28 * ho["max_dd"]
        + 900.0 * (wr - 0.50) * trades
        - 450.0 * ho["red"]
        + 180.0 * ho["green"]
        + 40.0 * ho["traded"]
    )


def pack(base: dict, name: str, scale: float, **kw) -> tuple[str, dict]:
    cfg = scale_point_cfg(base, scale)
    cfg.update(CAL)
    cfg.update(kw)
    cfg["profile_name"] = "FastTrackSensex"
    cfg["algo_name"] = "FastTrackSensex"
    cfg["underlying"] = "SENSEX"
    return name, cfg


def main() -> int:
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8")) if ANALYSIS.exists() else {}
    scale_w = float(analysis.get("recommended_point_scale") or 3.68)
    # stick near prior refined scale if present
    if OVERRIDES.exists():
        try:
            scale_w = float(
                json.loads(OVERRIDES.read_text(encoding="utf-8")).get("point_scale")
                or scale_w
            )
        except Exception:
            pass

    base = aggressive_overrides(100_000, 1.0)
    # Gamma wants decay / crush bounce available on expiry Tue
    base["post_decay_entry_enabled"] = True
    base["post_decay_standalone_entry"] = True

    as_of = date(2026, 11, 3)
    days = load_sensex_days(as_of, trade_days_only=True, cfg=CAL)
    if len(days) < 40:
        print(f"Too few Tue/Wed sessions: {len(days)}")
        return 1
    cut = int(len(days) * 0.70)
    train, hold = days[:cut], days[cut:]
    n_exp = sum(1 for d in days if d.get("is_expiry_day"))
    print(
        f"Sensex GAMMA retrain scale={scale_w} "
        f"Tue/Wed sessions={len(days)} (expiry Tue={n_exp}) "
        f"train={len(train)} hold={len(hold)}",
        flush=True,
    )

    cands: list[tuple[str, dict]] = []

    # 0) prior quality style on new calendar
    cands.append(
        pack(
            base,
            "sr_plus_micro_cal",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            post_decay_entry_enabled=False,
            post_decay_standalone_entry=False,
            signal_persist_minutes=2,
            directional_min_confidence=0.76,
        )
    )

    # 1) Expiry gamma core: ORB + SR reclaim + post_decay
    cands.append(
        pack(
            base,
            "gamma_orb_sr_pd",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            post_decay_min_bar=0,
            post_decay_confirm_minutes=2,
            post_decay_expiry_confirm_minutes=2,
            expiry_min_entry_minutes=60,
            expiry_no_entry_after_minutes=210,
            expiry_min_mom=int(round(18 * scale_w)),
            expiry_time_stop_minutes=18,
            orb_min_range_pts=int(round(35 * scale_w)),
            orb_max_range_pts=int(round(220 * scale_w)),
            orb_chase_min_extension=int(round(25 * scale_w)),
            pe_breakdown_min_mom=int(round(28 * scale_w)),
            ce_breakout_min_mom=int(round(28 * scale_w)),
            ce_min_vs_open=int(round(45 * scale_w)),
            pe_min_vs_open=int(round(40 * scale_w)),
            signal_persist_minutes=1,
            sr_signal_persist_minutes=1,
            directional_min_confidence=0.70,
            max_trades_per_day=2,
            bank_after_win=False,
            entry_cooldown_minutes=20,
        )
    )

    # 2) Gamma aggressive mom — catch blast continuation
    cands.append(
        pack(
            base,
            "gamma_mom_blast",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            post_decay_confirm_minutes=1,
            post_decay_expiry_confirm_minutes=1,
            post_decay_strong_est_pct=150.0,
            expiry_min_entry_minutes=45,
            expiry_no_entry_after_minutes=225,
            expiry_min_mom=int(round(15 * scale_w)),
            expiry_time_stop_minutes=15,
            micro_scalp_min_drive_pts=int(round(40 * scale_w)),
            micro_scalp_min_vs_open=int(round(30 * scale_w)),
            micro_scalp_max_giveback_from_extreme=int(round(55 * scale_w)),
            micro_scalp_entry_after_minutes=30,
            micro_scalp_no_entry_after_minutes=180,
            ce_min_vs_open=int(round(35 * scale_w)),
            pe_min_vs_open=int(round(30 * scale_w)),
            ce_breakout_min_mom=int(round(22 * scale_w)),
            pe_breakdown_min_mom=int(round(22 * scale_w)),
            signal_persist_minutes=1,
            directional_min_confidence=0.68,
            max_trades_per_day=3,
            bank_after_win=False,
            entry_cooldown_minutes=12,
        )
    )

    # 3) Gamma SR reclaim + ORB CE only (less PE knife)
    cands.append(
        pack(
            base,
            "gamma_sr_ce_orb",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=False,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            expiry_min_entry_minutes=75,
            expiry_min_mom=int(round(20 * scale_w)),
            ce_min_vs_open=int(round(40 * scale_w)),
            pe_min_vs_open=int(round(55 * scale_w)),
            ce_breakout_min_mom=int(round(25 * scale_w)),
            signal_persist_minutes=1,
            sr_signal_persist_minutes=1,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=25,
        )
    )

    # 4) Selective gamma: 1 trade/day, high mom, expiry window onlyish
    cands.append(
        pack(
            base,
            "gamma_selective_1td",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            expiry_min_entry_minutes=90,
            expiry_no_entry_after_minutes=195,
            expiry_min_mom=int(round(24 * scale_w)),
            ce_min_vs_open=int(round(55 * scale_w)),
            pe_min_vs_open=int(round(50 * scale_w)),
            sr_hard_min_mom10=int(round(20 * scale_w)),
            signal_persist_minutes=2,
            directional_min_confidence=0.78,
            max_trades_per_day=1,
            bank_after_win=True,
            entry_cooldown_minutes=40,
        )
    )

    # 5) Scale 3.2 gamma orb+sr
    cands.append(
        pack(
            base,
            "gamma_orb_sr_s32",
            3.2,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            expiry_min_entry_minutes=60,
            expiry_min_mom=int(round(18 * 3.2)),
            ce_min_vs_open=int(round(45 * 3.2)),
            pe_min_vs_open=int(round(40 * 3.2)),
            signal_persist_minutes=1,
            directional_min_confidence=0.70,
            max_trades_per_day=2,
            bank_after_win=False,
        )
    )

    # 6) Wed-friendly: SR + micro, softer ORB (still Tue gamma via expiry flags)
    cands.append(
        pack(
            base,
            "gamma_sr_micro_orb",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=False,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            micro_scalp_min_drive_pts=int(round(55 * scale_w)),
            micro_scalp_min_vs_open=int(round(40 * scale_w)),
            expiry_min_entry_minutes=70,
            expiry_min_mom=int(round(16 * scale_w)),
            ce_min_vs_open=int(round(42 * scale_w)),
            pe_min_vs_open=int(round(38 * scale_w)),
            signal_persist_minutes=1,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=False,
            entry_cooldown_minutes=18,
        )
    )

    # 7) Post-decay gamma only + SR (no ORB chase) — crush→blast
    cands.append(
        pack(
            base,
            "gamma_pd_sr",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            post_decay_confirm_minutes=2,
            post_decay_expiry_confirm_minutes=2,
            post_decay_strong_est_pct=180.0,
            post_decay_min_confidence=0.60,
            expiry_min_entry_minutes=75,
            expiry_no_entry_after_minutes=200,
            expiry_min_mom=int(round(16 * scale_w)),
            expiry_time_stop_minutes=16,
            ce_min_vs_open=int(round(50 * scale_w)),
            pe_min_vs_open=int(round(45 * scale_w)),
            sr_hard_min_mom10=int(round(16 * scale_w)),
            signal_persist_minutes=2,
            directional_min_confidence=0.74,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=25,
        )
    )

    # 8) Tight ORB gamma — high mom floors, bank after win
    cands.append(
        pack(
            base,
            "gamma_orb_tight",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            post_decay_entry_enabled=True,
            post_decay_standalone_entry=True,
            post_decay_respect_session_delay=False,
            expiry_min_entry_minutes=80,
            expiry_min_mom=int(round(22 * scale_w)),
            orb_min_range_pts=int(round(45 * scale_w)),
            orb_max_range_pts=int(round(180 * scale_w)),
            orb_chase_min_extension=int(round(35 * scale_w)),
            ce_breakout_min_mom=int(round(32 * scale_w)),
            pe_breakdown_min_mom=int(round(32 * scale_w)),
            ce_min_vs_open=int(round(55 * scale_w)),
            pe_min_vs_open=int(round(50 * scale_w)),
            signal_persist_minutes=2,
            directional_min_confidence=0.76,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=30,
        )
    )

    results = []
    for name, cfg in cands:
        print(f"--- {name}", flush=True)
        tr = run_window(train, cfg, 100_000, 1.0)
        ho = run_window(hold, cfg, 100_000, 1.0)
        sc = score_pair(tr, ho)
        wr = ho["wins"] / max(1, ho["wins"] + ho["losses"])
        print(
            f"  train net={tr['net']:+.0f} W/L {tr['wins']}/{tr['losses']} "
            f"G/R {tr['green']}/{tr['red']} cov {tr['traded']}/{tr['sessions']}",
            flush=True,
        )
        print(
            f"  hold  net={ho['net']:+.0f} W/L {ho['wins']}/{ho['losses']} "
            f"WR={100*wr:.0f}% G/R {ho['green']}/{ho['red']} "
            f"cov {ho['traded']}/{ho['sessions']} dd={ho['max_dd']:.0f} score={sc:.0f}",
            flush=True,
        )
        results.append(
            {
                "name": name,
                "score": round(sc, 2),
                "train": {k: tr[k] for k in tr if k not in ("daily", "ledger")},
                "hold": {k: ho[k] for k in ho if k not in ("daily", "ledger")},
                "hold_wr": round(wr, 3),
                "_cfg": cfg,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]
    print(f"\nBEST {best['name']} score={best['score']} WR={best['hold_wr']}", flush=True)

    cfg = best["_cfg"]
    full = run_window(days, cfg, 100_000, 1.0)
    wr_f = full["wins"] / max(1, full["wins"] + full["losses"])
    print(
        f"FULL net={full['net']:+.0f} W/L {full['wins']}/{full['losses']} "
        f"WR={100*wr_f:.0f}% G/R {full['green']}/{full['red']} "
        f"cov {full['traded']}/{full['sessions']} dd={full['max_dd']:.0f}",
        flush=True,
    )

    # stamp calendar + lesson onto live overrides
    cfg = deepcopy(cfg)
    cfg.update(CAL)

    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    BEST.write_text(
        json.dumps(
            {
                "best_name": best["name"],
                "score": best["score"],
                "hold": best["hold"],
                "hold_wr": best["hold_wr"],
                "full": {k: full[k] for k in full if k not in ("daily", "ledger")},
                "full_wr": round(wr_f, 3),
                "calendar": CAL,
                "candidates": [
                    {k: v for k, v in r.items() if k != "_cfg"} for r in results
                ],
                "lesson": (
                    "Sensex only Tue/Wed; Mon=pre-expiry cutoff. "
                    "Train for Tuesday gamma blast (ORB/post-decay/SR). "
                    "Off-days dual capital stays on Nifty."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    rep = {
        "title": (
            f"FastTrackSensex GAMMA · {best['name']} · "
            f"{days[0]['date']}→{days[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "mode": "causal_sensex_tue_wed_gamma",
        "calendar": CAL,
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
            "max_dd": full["max_dd"],
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
            "best_name": best["name"],
            "point_scale": cfg.get("point_scale"),
            "enable_orb_breakout": cfg.get("enable_orb_breakout"),
            "post_decay_entry_enabled": cfg.get("post_decay_entry_enabled"),
            "sensex_trade_weekdays": cfg.get("sensex_trade_weekdays"),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "sensex_fasttrack_gamma_report.json"
    hp = OUT / "sensex_fasttrack_gamma_report.html"
    jp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    write_detailed_html(rep, hp)
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    print(f"OVERRIDES → {OVERRIDES}")
    print(f"BEST → {BEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

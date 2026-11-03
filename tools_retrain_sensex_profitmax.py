"""
Sensex profit-max retrain on Tue/Wed + Thuγ calendar.

Train = all eligible sessions before last ~30 calendar days
Test  = last 30 calendar days (true OOS)

Score (train): net − 0.15*max_dd + 200*green − 250*red + 30*traded
               + 0.5*expiry_net   (push Tuesday gamma edge)
Pick max score with train net > 0; break ties by test net.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_train_sensex_fasttrack import load_sensex_days, run_window, scale_point_cfg

IST = ZoneInfo("Asia/Kolkata")
OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")
BEST = Path("data/sensex/fasttrack_sensex_profitmax_best.json")
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
    "sensex_expiry_orb_gamma": True,
    "sensex_expiry_min_break_strength": 0.22,
    "sensex_expiry_require_orb_retest": True,
    "sensex_expiry_block_failed_orb": True,
    "sensex_expiry_gamma_min_entry_minutes": 45,
    "sensex_expiry_gamma_no_entry_after": 210,
    "sensex_expiry_gamma_min_mom": 45,
}


def score_train(tr: dict, expiry_net: float = 0.0) -> float:
    trades = max(1, tr["wins"] + tr["losses"])
    wr = tr["wins"] / trades
    return (
        tr["net"]
        - 0.15 * tr["max_dd"]
        + 200.0 * tr["green"]
        - 250.0 * tr["red"]
        + 30.0 * tr["traded"]
        + 400.0 * (wr - 0.50) * trades
        + 0.50 * expiry_net
    )


def pack(base: dict, name: str, scale: float, **kw) -> tuple[str, dict]:
    cfg = scale_point_cfg(base, scale)
    cfg.update(CAL)
    cfg.update(kw)
    cfg["profile_name"] = "FastTrackSensex"
    cfg["algo_name"] = "FastTrackSensex"
    cfg["underlying"] = "SENSEX"
    cfg["post_decay_entry_enabled"] = bool(cfg.get("post_decay_entry_enabled", True))
    cfg["post_decay_standalone_entry"] = bool(
        cfg.get("post_decay_standalone_entry", True)
    )
    return name, cfg


def main() -> int:
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8")) if ANALYSIS.exists() else {}
    scale_w = float(analysis.get("recommended_point_scale") or 3.68)
    if OVERRIDES.exists():
        try:
            scale_w = float(
                json.loads(OVERRIDES.read_text(encoding="utf-8")).get("point_scale")
                or scale_w
            )
        except Exception:
            pass

    as_of = date(2026, 11, 3)
    test_start = as_of - timedelta(days=29)  # last ~30 calendar days
    all_days = load_sensex_days(as_of, trade_days_only=True, cfg=CAL)
    train = [d for d in all_days if date.fromisoformat(d["date"]) < test_start]
    test = [d for d in all_days if date.fromisoformat(d["date"]) >= test_start]
    if len(train) < 30 or len(test) < 3:
        print(f"Not enough data train={len(train)} test={len(test)}")
        return 1

    train_exp = [d for d in train if d.get("is_expiry_day")]
    print(
        f"Sensex PROFITMAX scale={scale_w} "
        f"train={len(train)} (exp={len(train_exp)}) "
        f"test={len(test)} [{test[0]['date']}→{test[-1]['date']}]",
        flush=True,
    )

    base = aggressive_overrides(100_000, 1.0)
    base["post_decay_entry_enabled"] = True
    base["post_decay_standalone_entry"] = True

    cands: list[tuple[str, dict]] = []

    # 0) current live-ish baseline
    if OVERRIDES.exists():
        cur = json.loads(OVERRIDES.read_text(encoding="utf-8"))
        cur.update(CAL)
        cur["underlying"] = "SENSEX"
        cands.append(("current_overrides", cur))

    # 1) SR + post-decay (prior quality DNA)
    cands.append(
        pack(
            base,
            "pd_sr",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            prefer_sr_dip_entry=True,
            directional_min_confidence=0.74,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=25,
            signal_persist_minutes=2,
        )
    )

    # 2) Profit push: SR + micro + pd
    cands.append(
        pack(
            base,
            "pd_sr_micro",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            micro_scalp_min_drive_pts=int(round(55 * scale_w)),
            micro_scalp_min_vs_open=int(round(40 * scale_w)),
            micro_scalp_max_giveback_from_extreme=int(round(50 * scale_w)),
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=False,
            entry_cooldown_minutes=18,
            signal_persist_minutes=1,
        )
    )

    # 3) Expiry ORB-retest gamma (base ORB off; overlay arms on expiry)
    cands.append(
        pack(
            base,
            "exp_orb_retest_sr",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            sensex_expiry_block_failed_orb=True,
            sensex_expiry_min_break_strength=0.22,
            sensex_expiry_gamma_min_entry_minutes=45,
            sensex_expiry_gamma_no_entry_after=210,
            sensex_expiry_gamma_min_mom=45,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=20,
            signal_persist_minutes=2,
            ce_min_vs_open=int(round(40 * scale_w)),
            pe_min_vs_open=int(round(35 * scale_w)),
        )
    )

    # 4) Aggressive profit: expiry ORB without retest (risky) + SR
    cands.append(
        pack(
            base,
            "exp_orb_chase_sr",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=False,
            sensex_expiry_block_failed_orb=False,
            sensex_expiry_min_break_strength=0.20,
            sensex_expiry_gamma_min_entry_minutes=40,
            sensex_expiry_gamma_no_entry_after=225,
            sensex_expiry_gamma_min_mom=35,
            directional_min_confidence=0.70,
            max_trades_per_day=2,
            bank_after_win=False,
            entry_cooldown_minutes=15,
            signal_persist_minutes=1,
            block_ce_mid_chase=False,
            soft_vs_open_on_strong_break=True,
        )
    )

    # 5) Max coverage SR reclaim looser floors
    cands.append(
        pack(
            base,
            "sr_loose_cov",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            ce_min_vs_open=int(round(30 * scale_w)),
            pe_min_vs_open=int(round(28 * scale_w)),
            sr_hard_min_mom10=int(round(12 * scale_w)),
            directional_min_confidence=0.68,
            max_trades_per_day=3,
            bank_after_win=False,
            entry_cooldown_minutes=12,
            signal_persist_minutes=1,
            sr_signal_persist_minutes=1,
        )
    )

    # 6) Ultra selective high WR
    cands.append(
        pack(
            base,
            "ultra_sr_1td",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            directional_min_confidence=0.80,
            max_trades_per_day=1,
            bank_after_win=True,
            entry_cooldown_minutes=40,
            signal_persist_minutes=2,
            ce_min_vs_open=int(round(55 * scale_w)),
            pe_min_vs_open=int(round(50 * scale_w)),
            sr_hard_min_mom10=int(round(20 * scale_w)),
        )
    )

    # 7) Scale 3.2 exp orb retest
    cands.append(
        pack(
            base,
            "exp_orb_retest_s32",
            3.2,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            sensex_expiry_min_break_strength=0.22,
            sensex_expiry_gamma_min_entry_minutes=45,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=True,
            ce_min_vs_open=int(round(40 * 3.2)),
            pe_min_vs_open=int(round(35 * 3.2)),
        )
    )

    # 8) PA off but ORB always on (not only expiry) — profit hunt
    cands.append(
        pack(
            base,
            "orb_always_sr",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            min_break_strength=0.22,
            block_orb_chase_without_retest=True,
            block_orb_after_failed_break=True,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=20,
        )
    )

    # 9) pd_sr + looser expiry mom / earlier floor
    cands.append(
        pack(
            base,
            "pd_sr_early_gamma",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            sensex_expiry_gamma_min_entry_minutes=35,
            sensex_expiry_gamma_no_entry_after=230,
            sensex_expiry_gamma_min_mom=30,
            sensex_expiry_min_break_strength=0.20,
            directional_min_confidence=0.70,
            max_trades_per_day=2,
            bank_after_win=True,
            entry_cooldown_minutes=18,
            signal_persist_minutes=1,
        )
    )

    results = []
    for name, cfg in cands:
        print(f"--- {name}", flush=True)
        tr = run_window(train, cfg, 100_000, 1.0)
        ex = run_window(train_exp, cfg, 100_000, 1.0) if train_exp else {
            "net": 0.0, "wins": 0, "losses": 0, "traded": 0, "sessions": 0,
            "max_dd": 0.0, "green": 0, "red": 0, "setups": 0,
        }
        te = run_window(test, cfg, 100_000, 1.0)
        sc = score_train(tr, expiry_net=float(ex.get("net") or 0))
        wr = tr["wins"] / max(1, tr["wins"] + tr["losses"])
        twr = te["wins"] / max(1, te["wins"] + te["losses"])
        print(
            f"  train net={tr['net']:+.0f} W/L {tr['wins']}/{tr['losses']} "
            f"WR={100*wr:.0f}% cov {tr['traded']}/{tr['sessions']} "
            f"expNet={ex['net']:+.0f} score={sc:.0f}",
            flush=True,
        )
        print(
            f"  TEST  net={te['net']:+.0f} W/L {te['wins']}/{te['losses']} "
            f"WR={100*twr:.0f}% cov {te['traded']}/{te['sessions']} "
            f"dd={te['max_dd']:.0f}",
            flush=True,
        )
        results.append(
            {
                "name": name,
                "score": round(sc, 2),
                "train": {k: tr[k] for k in tr if k not in ("daily", "ledger")},
                "train_expiry": {k: ex[k] for k in ex if k not in ("daily", "ledger")},
                "test": {k: te[k] for k in te if k not in ("daily", "ledger")},
                "train_wr": round(wr, 3),
                "test_wr": round(twr, 3),
                "_cfg": cfg,
                "_test_daily": te.get("daily"),
                "_test_ledger": te.get("ledger"),
            }
        )

    # Maximize profits: prefer train score, require train net>0, then best test net
    viable = [r for r in results if r["train"]["net"] > 0]
    pool = viable or results
    pool.sort(key=lambda r: (r["score"], r["test"]["net"], r["train"]["net"]), reverse=True)
    best = pool[0]
    print(
        f"\nBEST {best['name']} score={best['score']} "
        f"train={best['train']['net']:+.0f} test={best['test']['net']:+.0f}",
        flush=True,
    )

    cfg = deepcopy(best["_cfg"])
    cfg.update(CAL)
    full = run_window(all_days, cfg, 100_000, 1.0)
    wr_f = full["wins"] / max(1, full["wins"] + full["losses"])
    print(
        f"FULL net={full['net']:+.0f} W/L {full['wins']}/{full['losses']} "
        f"WR={100*wr_f:.0f}% cov {full['traded']}/{full['sessions']} "
        f"dd={full['max_dd']:.0f}",
        flush=True,
    )

    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    BEST.write_text(
        json.dumps(
            {
                "best_name": best["name"],
                "score": best["score"],
                "train": best["train"],
                "train_expiry": best["train_expiry"],
                "test": best["test"],
                "train_wr": best["train_wr"],
                "test_wr": best["test_wr"],
                "full": {k: full[k] for k in full if k not in ("daily", "ledger")},
                "full_wr": round(wr_f, 3),
                "calendar": CAL,
                "test_range": {
                    "start": test[0]["date"],
                    "end": test[-1]["date"],
                },
                "candidates": [
                    {k: v for k, v in r.items() if not k.startswith("_")}
                    for r in sorted(results, key=lambda x: x["score"], reverse=True)
                ],
                "lesson": (
                    "Profit-max on Tue/Wed+Thuγ; train before last 30d, "
                    "test = last month OOS. Expiry ORB via overlay+retest."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    te = best["test"]
    rep = {
        "title": (
            f"FastTrackSensex PROFITMAX · {best['name']} · "
            f"TEST {test[0]['date']}→{test[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "mode": "causal_sensex_profitmax_last_month",
        "calendar": CAL,
        "range": {"start": test[0]["date"], "end": test[-1]["date"]},
        "capital": {
            "initial": 100_000,
            "deploy_pct": 1.0,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": round(100_000 + float(te["net"]), 2),
            "net_pnl": te["net"],
            "return_pct": round(100 * float(te["net"]) / 100_000, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            "session_days": te["sessions"],
            "traded_days": te["traded"],
            "setups": te["setups"],
            "wins": te["wins"],
            "losses": te["losses"],
            "net": te["net"],
            "green_days": te["green"],
            "red_days": te["red"],
            "max_dd": te["max_dd"],
            "gross": 0,
            "charges": 0,
            "lots": 0,
            "invested_total": 0,
            "post_decay_entries": 0,
            "minutes_processed": 0,
            "analyses": 0,
            "signal_minutes": 0,
        },
        "daily": best.get("_test_daily") or [],
        "ledger": best.get("_test_ledger") or [],
        "config_snapshot": {
            "best_name": best["name"],
            "point_scale": cfg.get("point_scale"),
            "sensex_expiry_orb_gamma": cfg.get("sensex_expiry_orb_gamma"),
            "sensex_expiry_require_orb_retest": cfg.get(
                "sensex_expiry_require_orb_retest"
            ),
            "enable_sr_reclaim": cfg.get("enable_sr_reclaim"),
            "train_net": best["train"]["net"],
            "full_net": full["net"],
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"sensex_profitmax_month_{test[0]['date']}_to_{test[-1]['date']}"
    jp = OUT / f"{tag}_report.json"
    hp = OUT / f"{tag}_report.html"
    jp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    write_detailed_html(rep, hp)
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    print(f"OVERRIDES → {OVERRIDES}")
    print(f"BEST → {BEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

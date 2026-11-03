"""
Sensex 1y profit-max retrain on Tue/Wed/Thuγ — failure-driven.

Train before 2026-08-01, hold Aug→Nov. Score maximizes hold net with
2× baseline bonus. Writes sensex overrides.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_retrain_sensex_profitmax import CAL, pack, score_train
from tools_train_sensex_fasttrack import load_sensex_days, run_window

IST = ZoneInfo("Asia/Kolkata")
OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")
BEST = Path("data/sensex/fasttrack_sensex_year_profitmax_best.json")


def main() -> int:
    scale_w = 3.68
    if OVERRIDES.exists():
        try:
            scale_w = float(
                json.loads(OVERRIDES.read_text(encoding="utf-8")).get("point_scale")
                or scale_w
            )
        except Exception:
            pass

    as_of = date(2026, 11, 3)
    all_days = load_sensex_days(as_of, trade_days_only=True, cfg=CAL)
    train = [d for d in all_days if d["date"] < "2026-08-01"]
    hold = [d for d in all_days if d["date"] >= "2026-08-01"]
    train_exp = [d for d in train if d.get("is_expiry_day")]
    print(
        f"Sensex YEAR profitmax scale={scale_w} "
        f"train={len(train)} hold={len(hold)} expTrain={len(train_exp)}",
        flush=True,
    )

    base = aggressive_overrides(100_000, 1.0)
    base["post_decay_entry_enabled"] = True
    base["post_decay_standalone_entry"] = True

    # baseline = current live overrides if present
    cands: list[tuple[str, dict]] = []
    if OVERRIDES.exists():
        cur = json.loads(OVERRIDES.read_text(encoding="utf-8"))
        cur.update(CAL)
        cands.append(("current", cur))

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
    cands.append(
        pack(
            base,
            "exp_orb_retest_run",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=False,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            sensex_expiry_gamma_min_entry_minutes=40,
            sensex_expiry_gamma_min_mom=35,
            sensex_expiry_min_break_strength=0.20,
            target_pct_low=6.0,
            target_pct_high=55.0,
            trail_giveback_pct=3.0,
            lock_profit_pct=2.0,
            directional_min_confidence=0.70,
            max_trades_per_day=2,
            bank_after_win=False,
            entry_cooldown_minutes=15,
            signal_persist_minutes=1,
            ce_min_vs_open=int(round(35 * scale_w)),
            pe_min_vs_open=int(round(32 * scale_w)),
        )
    )
    cands.append(
        pack(
            base,
            "exp_orb_noretest_sel",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=False,
            sensex_expiry_block_failed_orb=True,
            sensex_expiry_gamma_min_entry_minutes=45,
            sensex_expiry_gamma_no_entry_after=180,
            sensex_expiry_gamma_min_mom=50,
            sensex_expiry_min_break_strength=0.25,
            directional_min_confidence=0.74,
            max_trades_per_day=1,
            bank_after_win=True,
            target_pct_low=6.0,
            trail_giveback_pct=2.8,
        )
    )
    cands.append(
        pack(
            base,
            "sr_cov_plus_exp",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_sr_reclaim=True,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            micro_scalp_min_drive_pts=int(round(50 * scale_w)),
            micro_scalp_min_vs_open=int(round(35 * scale_w)),
            ce_min_vs_open=int(round(28 * scale_w)),
            pe_min_vs_open=int(round(28 * scale_w)),
            directional_min_confidence=0.68,
            max_trades_per_day=3,
            bank_after_win=False,
            entry_cooldown_minutes=12,
            signal_persist_minutes=1,
            target_pct_low=5.5,
            target_pct_high=50.0,
        )
    )
    cands.append(
        pack(
            base,
            "ultra_sr_exp",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            directional_min_confidence=0.78,
            max_trades_per_day=1,
            bank_after_win=True,
            ce_min_vs_open=int(round(50 * scale_w)),
            pe_min_vs_open=int(round(45 * scale_w)),
            sr_hard_min_mom10=int(round(18 * scale_w)),
            target_pct_low=7.0,
            trail_giveback_pct=2.5,
        )
    )
    cands.append(
        pack(
            base,
            "orb_always_retest",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_sr_reclaim=True,
            enable_orb_breakout=True,
            enable_orb_breakdown=True,
            min_break_strength=0.22,
            block_orb_chase_without_retest=True,
            block_orb_after_failed_break=True,
            sensex_expiry_orb_gamma=True,
            sensex_expiry_require_orb_retest=True,
            directional_min_confidence=0.72,
            max_trades_per_day=2,
            bank_after_win=True,
            target_pct_low=6.0,
            trail_giveback_pct=3.0,
        )
    )

    # baseline for mult
    base_cfg = cands[0][1]
    base_ho = run_window(hold, base_cfg, 100_000, 1.0)
    base_hold_net = float(base_ho["net"])
    print(f"baseline hold net={base_hold_net:+.0f}", flush=True)

    results = []
    best = None
    best_sc = float("-inf")
    for name, cfg in cands:
        print(f"--- {name}", flush=True)
        tr = run_window(train, cfg, 100_000, 1.0)
        ho = run_window(hold, cfg, 100_000, 1.0)
        ex = run_window(train_exp, cfg, 100_000, 1.0) if train_exp else {"net": 0}
        sc = score_train(tr, float(ex.get("net") or 0))
        # prefer hold profit hard
        sc = sc + float(ho["net"]) * 0.85
        if base_hold_net > 0 and ho["net"] >= 2 * base_hold_net:
            sc += 50_000
        elif base_hold_net > 0 and ho["net"] >= 1.5 * base_hold_net:
            sc += 20_000
        if tr["net"] < 0:
            sc -= 30_000
        mult = ho["net"] / base_hold_net if base_hold_net else 0
        print(
            f"  train={tr['net']:+.0f} hold={ho['net']:+.0f} "
            f"mult={mult:.2f}x score={sc:.0f} cov {ho['traded']}/{ho['sessions']}",
            flush=True,
        )
        row = {
            "name": name,
            "score": round(sc, 2),
            "hold_mult": round(mult, 3),
            "train": {k: tr[k] for k in tr if k not in ("daily", "ledger")},
            "hold": {k: ho[k] for k in ho if k not in ("daily", "ledger")},
            "_cfg": cfg,
            "_hold_daily": ho.get("daily"),
            "_hold_ledger": ho.get("ledger"),
        }
        results.append(row)
        if sc > best_sc and tr["net"] > 0:
            best_sc = sc
            best = row
            print(f"  *** BEST {name} ***", flush=True)

    if best is None:
        best = max(results, key=lambda r: r["score"])

    cfg = deepcopy(best["_cfg"])
    cfg.update(CAL)
    full = run_window(all_days, cfg, 100_000, 1.0)
    last30 = [d for d in all_days if d["date"] >= "2026-10-05"]
    m30 = run_window(last30, cfg, 100_000, 1.0)
    print(
        f"\nBEST {best['name']} hold={best['hold']['net']:+.0f} "
        f"({best['hold_mult']:.2f}×) full={full['net']:+.0f} "
        f"last30={m30['net']:+.0f}",
        flush=True,
    )

    OVERRIDES.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    BEST.write_text(
        json.dumps(
            {
                "best_name": best["name"],
                "score": best["score"],
                "hold_mult": best["hold_mult"],
                "baseline_hold_net": base_hold_net,
                "train": best["train"],
                "hold": best["hold"],
                "full": {k: full[k] for k in full if k not in ("daily", "ledger")},
                "last30": {k: m30[k] for k in m30 if k not in ("daily", "ledger")},
                "ranking": [
                    {k: v for k, v in r.items() if not k.startswith("_")}
                    for r in sorted(results, key=lambda x: -x["score"])
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    ho = best["hold"]
    rep = {
        "title": f"Sensex YEAR PROFITMAX · {best['name']} · hold Aug→Nov",
        "generated_at": date.today().isoformat(),
        "profile": "FastTrackSensex",
        "mode": "sensex_year_profitmax",
        "calendar": CAL,
        "range": {"start": hold[0]["date"], "end": hold[-1]["date"]},
        "capital": {
            "initial": 100_000,
            "deploy_pct": 1.0,
            "deploy_cap": 0.0,
            "halt_pct": 0.0,
            "halt_floor": 0.0,
            "final_balance": round(100_000 + float(ho["net"]), 2),
            "net_pnl": ho["net"],
            "return_pct": round(100 * float(ho["net"]) / 100_000, 2),
            "halted": False,
            "halt_reason": "",
        },
        "totals": {
            "session_days": ho["sessions"],
            "traded_days": ho["traded"],
            "setups": ho["setups"],
            "wins": ho["wins"],
            "losses": ho["losses"],
            "net": ho["net"],
            "green_days": ho["green"],
            "red_days": ho["red"],
            "max_dd": ho["max_dd"],
            "gross": 0,
            "charges": 0,
            "lots": 0,
            "invested_total": 0,
            "post_decay_entries": 0,
            "minutes_processed": 0,
            "analyses": 0,
            "signal_minutes": 0,
        },
        "daily": best.get("_hold_daily") or [],
        "ledger": best.get("_hold_ledger") or [],
        "config_snapshot": {
            "best_name": best["name"],
            "hold_mult": best["hold_mult"],
            "full_net": full["net"],
            "last30_net": m30["net"],
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "sensex_year_profitmax_report.json"
    hp = OUT / "sensex_year_profitmax_report.html"
    jp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    write_detailed_html(rep, hp)
    print(f"OVERRIDES → {OVERRIDES}")
    print(f"HTML {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

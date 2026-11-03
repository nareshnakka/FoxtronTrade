"""
Retrain Sensex FastTrack for fewer reds — S/R-first, kill PA_PB_RESUME bleed.

Lesson from red autopsy (273 trades):
  PA_PB_RESUME 222 trades 103W/119L net ≈ −2k  ← noise
  DIR SR_RECLAIM 21 trades 13W/8L net ≈ +122k ← real edge

Score = holdout_net − 0.25*max_dd + 800*(hold_winrate−0.5)*hold_trades
        − 400*hold_red_days   (prefer green days, punish reds)
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
BEST = Path("data/sensex/fasttrack_sensex_quality_best.json")
ANALYSIS = Path("data/sensex/momentum_analysis.json")


def score_hold(ho: dict) -> float:
    trades = max(1, ho["wins"] + ho["losses"])
    wr = ho["wins"] / trades
    # prioritize fewer reds + holdout profit
    return (
        ho["net"]
        - 0.30 * ho["max_dd"]
        + 1200.0 * (wr - 0.50) * trades
        - 500.0 * ho["red"]
        + 200.0 * ho["green"]
    )


def pack(base: dict, name: str, scale: float, **kw) -> tuple[str, dict]:
    cfg = scale_point_cfg(base, scale)
    cfg.update(kw)
    cfg["profile_name"] = "FastTrackSensex"
    cfg["algo_name"] = "FastTrackSensex"
    return name, cfg


def main() -> int:
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8")) if ANALYSIS.exists() else {}
    scale = float((analysis.get("recommended_point_scale") or 3.2))
    # prior winner
    scale_w = 3.68

    base = aggressive_overrides(100_000, 1.0)
    base["post_decay_entry_enabled"] = False
    base["post_decay_standalone_entry"] = False

    as_of = date(2026, 11, 3)
    days = load_sensex_days(as_of)
    cut = int(len(days) * 0.70)
    train, hold = days[:cut], days[cut:]
    print(
        f"Sensex quality retrain scale~{scale_w} "
        f"train={len(train)} hold={len(hold)}",
        flush=True,
    )

    cands: list[tuple[str, dict]] = []

    # 0) previous full-participation baseline
    cands.append(pack(base, "baseline_s368", scale_w))

    # 1) Kill PA path entirely — DIR S/R only (+ optional ORB)
    cands.append(
        pack(
            base,
            "sr_primary_no_pa",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=True,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            prefer_sr_dip_entry=True,
            signal_persist_minutes=2,
            sr_signal_persist_minutes=2,
            directional_min_confidence=0.75,
        )
    )

    # 2) SR reclaim only (no reject, no ORB) — autopsy king
    cands.append(
        pack(
            base,
            "sr_reclaim_only",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            prefer_sr_dip_entry=True,
            signal_persist_minutes=2,
            sr_signal_persist_minutes=2,
            directional_min_confidence=0.78,
            sr_hard_min_mom10=int(round(18 * scale_w)),
            sr_reclaim_max_under_support=int(round(12 * scale_w)),
        )
    )

    # 3) SR reclaim + careful breakdown (no CE breakout chase)
    cands.append(
        pack(
            base,
            "sr_reclaim_pe_break",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=True,
            pe_breakdown_min_mom=int(round(40 * scale_w)),
            prefer_sr_dip_entry=True,
            signal_persist_minutes=2,
            directional_min_confidence=0.76,
        )
    )

    # 4) PA but S/R only (no PB resume) + DIR SR
    cands.append(
        pack(
            base,
            "pa_sr_only_no_pb",
            scale_w,
            enable_daily_pa=True,
            daily_pa_allow_pb_resume=False,
            daily_pa_allow_mom_break=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            enable_sr_reclaim=True,
            prefer_sr_dip_entry=True,
            signal_persist_minutes=2,
            daily_pa_persist_minutes=2,
            daily_pa_min_mom_pts=int(round(12 * scale_w)),
            directional_min_confidence=0.74,
        )
    )

    # 5) Quality mom + SR reclaim, higher CE/PE floors
    cands.append(
        pack(
            base,
            "sr_mom_strict",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            ce_min_vs_open=int(round(75 * scale_w)),
            pe_min_vs_open=int(round(65 * scale_w)),
            sr_hard_min_mom10=int(round(22 * scale_w)),
            sr_soft_min_mom10=int(round(25 * scale_w)),
            signal_persist_minutes=2,
            sr_signal_persist_minutes=2,
            directional_min_confidence=0.80,
            max_trades_per_day=1,
            bank_after_win=True,
        )
    )

    # 6) Same as 2 but scale 3.2 (range-native)
    cands.append(
        pack(
            base,
            "sr_reclaim_s32",
            3.2,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            prefer_sr_dip_entry=True,
            signal_persist_minutes=2,
            sr_signal_persist_minutes=2,
            directional_min_confidence=0.78,
        )
    )

    # 7) SR reclaim + micro only (careful scalp), no PA
    cands.append(
        pack(
            base,
            "sr_plus_micro",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=True,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            micro_scalp_min_drive_pts=int(round(70 * scale_w)),
            micro_scalp_min_vs_open=int(round(55 * scale_w)),
            micro_scalp_max_giveback_from_extreme=int(round(50 * scale_w)),
            signal_persist_minutes=2,
            directional_min_confidence=0.76,
        )
    )

    # 8) Ultra selective: 1 trade/day, high conf, reclaim only, mom hard
    cands.append(
        pack(
            base,
            "ultra_sr_1td",
            scale_w,
            enable_daily_pa=False,
            enable_micro_scalp=False,
            enable_daily_soft_scalp=False,
            enable_sr_reclaim=True,
            enable_sr_reject=False,
            enable_orb_breakout=False,
            enable_orb_breakdown=False,
            max_trades_per_day=1,
            bank_after_win=True,
            directional_min_confidence=0.82,
            directional_min_margin=0.25,
            signal_persist_minutes=3,
            sr_signal_persist_minutes=2,
            ce_min_vs_open=int(round(80 * scale_w)),
            pe_min_vs_open=int(round(70 * scale_w)),
            sr_hard_min_mom10=int(round(25 * scale_w)),
            entry_cooldown_minutes=45,
        )
    )

    results = []
    for name, cfg in cands:
        print(f"--- {name}", flush=True)
        tr = run_window(train, cfg, 100_000, 1.0)
        ho = run_window(hold, cfg, 100_000, 1.0)
        sc = score_hold(ho)
        wr = ho["wins"] / max(1, ho["wins"] + ho["losses"])
        print(
            f"  train net={tr['net']:+.0f} W/L {tr['wins']}/{tr['losses']} "
            f"G/R {tr['green']}/{tr['red']}",
            flush=True,
        )
        print(
            f"  hold  net={ho['net']:+.0f} W/L {ho['wins']}/{ho['losses']} "
            f"WR={100*wr:.0f}% G/R {ho['green']}/{ho['red']} "
            f"dd={ho['max_dd']:.0f} score={sc:.0f}",
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
                "candidates": [
                    {k: v for k, v in r.items() if k != "_cfg"} for r in results
                ],
                "lesson": (
                    "PA_PB_RESUME flooded reds; DIR SR_RECLAIM is the edge. "
                    "Quality retrain disables PA noise and requires confirmed S/R."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    rep = {
        "title": (
            f"FastTrackSensex QUALITY · {best['name']} · "
            f"{days[0]['date']}→{days[-1]['date']}"
        ),
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "mode": "causal_sensex_sr_quality",
        "premium_model": "Synthetic ATM from spot Δ (no expired option history)",
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
            "best_name": best["name"],
            "enable_daily_pa": cfg.get("enable_daily_pa"),
            "enable_sr_reclaim": cfg.get("enable_sr_reclaim"),
            "enable_orb_breakout": cfg.get("enable_orb_breakout"),
            "ce_min_vs_open": cfg.get("ce_min_vs_open"),
            "pe_min_vs_open": cfg.get("pe_min_vs_open"),
            "directional_min_confidence": cfg.get("directional_min_confidence"),
            "signal_persist_minutes": cfg.get("signal_persist_minutes"),
            "lot_size": cfg.get("lot_size"),
            "point_scale": cfg.get("point_scale"),
        },
    }
    jp = OUT / "sensex_fasttrack_quality_report.json"
    hp = OUT / "sensex_fasttrack_quality_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)
    print(f"HTML {hp}")
    print(f"OVERRIDES {OVERRIDES}")
    print(f"BEST {BEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

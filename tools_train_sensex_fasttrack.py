"""
Train FastTrack-style Sensex profile on local sensex minutes.

Uses synthetic ATM premium (same as Nifty causal backtest). Point gates
are scaled from Nifty FastTrack by Sensex/Nifty range ratio.
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
    _load_bars,
    simulate_day_detailed,
    write_detailed_html,
)
from backtest_month_ledger import OUT, LedgerState, month_cfg
from foxtrade.decay_memory import DecayMemory
from foxtrade.sensex_calendar import (
    is_sensex_calendar_eligible,
    is_sensex_expiry_day,
    sensex_day_role,
)
from tools_backtest_week import aggressive_overrides

IST = ZoneInfo("Asia/Kolkata")
SX_MIN = Path("data/sensex/minutes")
ANALYSIS = Path("data/sensex/momentum_analysis.json")
BEST = Path("data/sensex/fasttrack_sensex_best.json")
OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")


def load_sensex_days(
    as_of: date,
    calendar_days: int | None = None,
    *,
    trade_days_only: bool = False,
    cfg: dict | None = None,
) -> list[dict]:
    files = sorted(SX_MIN.glob("sensex_*.json"))
    rows = []
    for p in files:
        try:
            dd = date.fromisoformat(p.stem.replace("sensex_", ""))
        except ValueError:
            continue
        if dd > as_of:
            continue
        rows.append(dd)
    if not rows:
        return []
    if calendar_days:
        start = as_of.fromordinal(as_of.toordinal() - calendar_days + 1)
        rows = [d for d in rows if d >= start]
    out = []
    for dd in rows:
        # Include Tue/Wed always + Thu gamma slot (armed at runtime by detector)
        if trade_days_only and not is_sensex_calendar_eligible(dd, cfg):
            continue
        # Sensex weekly options expire Tuesday
        out.append(
            {
                "date": dd.isoformat(),
                "is_expiry_day": is_sensex_expiry_day(dd, cfg),
                "day_role": sensex_day_role(dd, cfg),
                "minutes_path": SX_MIN / f"sensex_{dd.isoformat()}.json",
            }
        )
    return out


def scale_point_cfg(base: dict[str, Any], scale: float) -> dict[str, Any]:
    """Scale numeric *point* gates; leave % / ratios / bools / times alone."""
    # Keys that are clearly point distances (spot pts)
    point_keys = {
        "post_decay_spot_align_pts",
        "post_decay_ce_max_under_open",
        "daily_soft_pe_min_vs_open",
        "micro_scalp_spot_target_pts",
        "micro_scalp_min_drive_pts",
        "micro_scalp_min_vs_open",
        "micro_scalp_max_giveback_from_extreme",
        "daily_pa_min_vs_open",
        "daily_pa_min_mom_pts",
        "daily_pa_spot_target_pts",
        "daily_pa_pe_min_vs_open",
        "daily_pa_ce_min_vs_open",
        "daily_pa_ce_max_giveback",
        "daily_pa_sr_proximity_pts",
        "sharp_fall_day_ext_pts",
        "ce_fail_rally_giveback_pts",
        "sr_late_min_vs_open",
        "sr_late_min_above_support",
        "sr_max_giveback_pts",
        "sr_soft_vs_open",
        "sr_soft_min_mom10",
        "pe_snapback_bounce_pts",
        "ce_breakout_min_mom",
        "sr_hard_min_mom10",
        "sr_spent_or_range_pts",
        "sr_fast_persist_min_vs",
        "sr_fast_persist_min_mom",
        "pe_spent_day_ext_pts",
        "pe_max_off_day_low_pts",
        "pe_breakdown_min_mom",
        "orb_retest_proximity_pts",
        "orb_chase_min_extension",
        "orb_min_range_pts",
        "orb_max_range_pts",
        "breakout_max_extension_pts",
        "ce_min_vs_open",
        "pe_min_vs_open",
        "ce_min_vs_open_break",
        "pe_min_vs_open_break",
        "sr_range_min_day_efficiency",  # ratio — skip via type check below
        "sr_dip_min_extent",
        "sr_dip_pullback_min",
        "sr_dip_proximity_pts",
        "sr_reclaim_max_under_support",
        "sr_ce_min_vs_open",
        "pe_rebreak_min_mom",
        "expiry_min_mom",
        "chop_range_pts",
        "chop_near_open_pts",
        "min_vs_open_entry",
    }
    # efficiency / fractions stay
    skip_suffix = ("_pct", "_efficiency", "_frac", "_mult", "_minutes", "_bars")
    cfg = deepcopy(base)
    for k, v in list(cfg.items()):
        if k not in point_keys:
            continue
        if any(k.endswith(s) for s in skip_suffix):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            # keep ints as ints when original was int-like
            nv = float(v) * scale
            cfg[k] = int(round(nv)) if isinstance(v, int) or float(v).is_integer() else round(nv, 1)
    cfg["lot_size"] = 20
    cfg["profile_name"] = "FastTrackSensex"
    cfg["algo_name"] = "FastTrackSensex"
    cfg["underlying"] = "SENSEX"
    cfg["point_scale"] = scale
    # Sensex ATM premium band (wider absolute ₹; still synth mid used in BT)
    cfg["premium_low"] = 180
    cfg["premium_high"] = 280
    return cfg


def run_window(
    days: list[dict],
    cfg: dict[str, Any],
    capital: float,
    deploy: float,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    state = LedgerState(
        initial=capital,
        balance=capital,
        deploy_pct=deploy,
        deploy_cap=0.0,
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
            ce_open=None,
            pe_open=None,
            verbose=verbose,
        )
        dm.finalize_day(d["date"])
        daily.append(row)
    nets = [float(r.get("net") or 0) for r in daily]
    bals = [float(r.get("balance_close") or capital) for r in daily]
    peak = capital
    maxdd = 0.0
    for b in bals:
        peak = max(peak, b)
        maxdd = max(maxdd, peak - b)
    return {
        "sessions": len(daily),
        "traded": sum(1 for r in daily if int(r.get("setups") or 0) > 0),
        "setups": sum(int(r.get("setups") or 0) for r in daily),
        "wins": sum(int(r.get("wins") or 0) for r in daily),
        "losses": sum(int(r.get("losses") or 0) for r in daily),
        "net": round(state.balance - capital, 2),
        "final": round(state.balance, 2),
        "max_dd": round(maxdd, 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "daily": daily,
        "ledger": list(state.ledger),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=1.0)
    ap.add_argument("--as-of", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--scale", type=float, default=0.0, help="0=auto from analysis")
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of)

    if not SX_MIN.exists() or not list(SX_MIN.glob("sensex_*.json")):
        print("No Sensex minutes yet — run tools_fetch_sensex_year.py first")
        return 1

    # Ensure momentum analysis exists
    if not ANALYSIS.exists():
        from tools_analyze_sensex_momentum import main as analyze

        analyze()
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    auto_scale = float(analysis.get("recommended_point_scale") or 3.1)
    base_scale = float(args.scale) if args.scale > 0 else auto_scale
    print(f"Sensex point scale={base_scale} (auto={auto_scale})", flush=True)

    all_days = load_sensex_days(as_of)
    if len(all_days) < 40:
        print(f"Only {len(all_days)} Sensex days — need more history")
        return 1

    # Walk-forward: train first 70%, holdout last 30%
    cut = int(len(all_days) * 0.70)
    train_days = all_days[:cut]
    hold_days = all_days[cut:]
    print(
        f"Days {all_days[0]['date']}→{all_days[-1]['date']} "
        f"n={len(all_days)} train={len(train_days)} hold={len(hold_days)}",
        flush=True,
    )

    base = aggressive_overrides(args.capital, args.deploy)
    # Disable post-decay memory path for Sensex until we have option OHLC seeds
    base["post_decay_entry_enabled"] = False
    base["post_decay_standalone_entry"] = False

    candidates: list[tuple[str, dict]] = []
    for s in sorted({round(base_scale * m, 2) for m in (0.85, 1.0, 1.15, 1.3)}):
        candidates.append((f"scale_{s}", scale_point_cfg(base, s)))

    # Extra: tighter CE vs-open (scaled) + PA gates emphasis
    lean = scale_point_cfg(base, base_scale)
    lean["daily_pa_ce_min_vs_open"] = int(round(28 * base_scale))
    lean["daily_pa_pe_min_vs_open"] = int(round(35 * base_scale))
    lean["daily_pa_pe_entry_after_minutes"] = 75
    candidates.append((f"lean_pa_s{base_scale}", lean))

    strict = scale_point_cfg(base, base_scale)
    strict["ce_min_vs_open"] = int(round(65 * base_scale * 1.1))
    strict["pe_min_vs_open"] = int(round(55 * base_scale * 1.1))
    strict["enable_micro_scalp"] = False  # test without micro first
    candidates.append((f"strict_no_micro_s{base_scale}", strict))

    results = []
    for name, cfg in candidates:
        print(f"--- train {name}", flush=True)
        tr = run_window(train_days, cfg, args.capital, args.deploy)
        print(
            f"    train net={tr['net']:+.0f} W/L {tr['wins']}/{tr['losses']} "
            f"cov {tr['traded']}/{tr['sessions']} dd={tr['max_dd']:.0f}",
            flush=True,
        )
        print(f"--- hold {name}", flush=True)
        ho = run_window(hold_days, cfg, args.capital, args.deploy)
        print(
            f"    hold  net={ho['net']:+.0f} W/L {ho['wins']}/{ho['losses']} "
            f"cov {ho['traded']}/{ho['sessions']} dd={ho['max_dd']:.0f}",
            flush=True,
        )
        score = ho["net"] - 0.35 * ho["max_dd"]  # prefer holdout, penalize DD
        results.append(
            {
                "name": name,
                "score": round(score, 2),
                "train": {k: tr[k] for k in tr if k not in ("daily", "ledger")},
                "hold": {k: ho[k] for k in ho if k not in ("daily", "ledger")},
                "cfg": {
                    k: cfg[k]
                    for k in (
                        "point_scale",
                        "lot_size",
                        "ce_min_vs_open",
                        "pe_min_vs_open",
                        "orb_min_range_pts",
                        "orb_max_range_pts",
                        "daily_pa_ce_min_vs_open",
                        "daily_pa_pe_min_vs_open",
                        "enable_micro_scalp",
                    )
                    if k in cfg
                },
                "_full_cfg": cfg,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]
    print(f"\nBEST {best['name']} score={best['score']}", flush=True)

    # Full-sample with winner
    print("--- full sample best", flush=True)
    full = run_window(all_days, best["_full_cfg"], args.capital, args.deploy, verbose=False)
    print(
        f"FULL net={full['net']:+.0f} ({100*full['net']/args.capital:+.1f}%) "
        f"W/L {full['wins']}/{full['losses']} "
        f"cov {full['traded']}/{full['sessions']} dd={full['max_dd']:.0f}",
        flush=True,
    )

    cfg_best = best["_full_cfg"]
    rep = {
        "title": f"FastTrackSensex · {all_days[0]['date']}→{all_days[-1]['date']}",
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrackSensex",
        "analysis": analysis,
        "candidates": [{k: v for k, v in r.items() if k != "_full_cfg"} for r in results],
        "best_name": best["name"],
        "full": {k: full[k] for k in full if k not in ("daily", "ledger")},
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
        },
        "daily": full["daily"],
        "ledger": full["ledger"],
        "config_snapshot": {
            "profile_name": "FastTrackSensex",
            "algo_name": "FastTrackSensex",
            "underlying": "SENSEX",
            "point_scale": cfg_best.get("point_scale"),
            "lot_size": cfg_best.get("lot_size"),
            "ce_min_vs_open": cfg_best.get("ce_min_vs_open"),
            "pe_min_vs_open": cfg_best.get("pe_min_vs_open"),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "sensex_fasttrack_full_report.json"
    hp = OUT / "sensex_fasttrack_full_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    write_detailed_html(rep, hp)

    save_cfg = {k: v for k, v in cfg_best.items()}
    BEST.write_text(
        json.dumps(
            {
                "best_name": best["name"],
                "hold": best["hold"],
                "full": rep["full"],
                "overrides_path": str(OVERRIDES),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    OVERRIDES.write_text(json.dumps(save_cfg, indent=2), encoding="utf-8")
    print(f"HTML {hp}")
    print(f"JSON {jp}")
    print(f"OVERRIDES {OVERRIDES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

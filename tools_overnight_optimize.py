"""
Overnight iterative backtest → diagnose → fix → retest loop.

No cheating: causal minute synth only (backtest_detailed), no lookahead.

Honest goal ranking:
  1) Maximize green_day_ratio on 30d/60d
  2) Minimize red days / early knife stops
  3) Keep long windows net-positive
  4) Push median *traded* day return toward +5% when a setup fires
     (skip days are allowed — fabricating fills is not)

Writes under data/backtests/:
  overnight_iter_log.jsonl
  overnight_best_report.{json,html}
  overnight_final_multi.{json,html}
  overnight_best_overrides.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest_detailed import run_detailed, write_detailed_html
from backtest_multi_periods import PERIODS, _summarize, write_multi_html
from backtest_month_ledger import OUT

OUT.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def score_period(summary: dict[str, Any]) -> dict[str, float]:
    sessions = max(int(summary.get("session_days") or 0), 1)
    traded = max(int(summary.get("traded_days") or 0), 1)
    green = int(summary.get("green_days") or 0)
    red = int(summary.get("red_days") or 0)
    net = float(summary.get("net_pnl") or 0)
    ret = float(summary.get("return_pct") or 0)
    green_ratio = green / sessions
    red_ratio = red / sessions
    score = (
        green_ratio * 100.0
        - red_ratio * 150.0
        + min(ret, 100.0) * 0.30
        - (35.0 if net < 0 else 0.0)
        - (25.0 if summary.get("halted") else 0.0)
        + min(traded / sessions, 1.0) * 6.0
    )
    return {
        "score": round(score, 3),
        "green_ratio": round(green_ratio, 4),
        "red_ratio": round(red_ratio, 4),
        "ret": round(ret, 3),
        "net": round(net, 2),
    }


def analyze_losses(rep: dict[str, Any]) -> dict[str, Any]:
    from collections import Counter

    daily = rep.get("daily") or []
    ledger = rep.get("ledger") or []
    capital = float((rep.get("capital") or {}).get("initial") or 100000)
    red_days = [d for d in daily if float(d.get("net") or 0) < 0]
    reasons: Counter[str] = Counter()
    early_stops = 0
    for t in ledger:
        reason = str(t.get("reason") or "")
        reasons[reason.split("(")[0].strip()] += 1
        held = int(t.get("held_m") or 0)
        net = float(t.get("net") or 0)
        if net < 0 and held <= 3:
            early_stops += 1
    traded = [d for d in daily if int(d.get("setups") or 0) > 0]
    day_pcts = [100.0 * float(d.get("net") or 0) / max(capital, 1.0) for d in traded]
    return {
        "red_days": len(red_days),
        "green_days": sum(1 for d in daily if float(d.get("net") or 0) > 0),
        "early_stop_losses": early_stops,
        "exit_reasons": reasons.most_common(12),
        "worst_days": [
            {"date": d.get("date"), "net": d.get("net"), "setups": d.get("setups")}
            for d in sorted(red_days, key=lambda x: float(x.get("net") or 0))[:8]
        ],
        "median_traded_day_pct": round(
            sorted(day_pcts)[len(day_pcts) // 2] if day_pcts else 0.0, 3
        ),
        "avg_traded_day_pct": round(sum(day_pcts) / len(day_pcts), 3) if day_pcts else 0.0,
        "pct_traded_days_ge_5": round(
            100.0 * sum(1 for x in day_pcts if x >= 5.0) / len(day_pcts), 1
        )
        if day_pcts
        else 0.0,
    }


def run_periods(
    *,
    capital: float,
    deploy_pct: float,
    cfg_overrides: dict[str, Any],
    periods: list[int],
    tag: str,
) -> dict[str, Any]:
    blocks = []
    for days in periods:
        print(f"[{_now()}] {tag} · {days}d …", flush=True)
        rep = run_detailed(
            initial=capital,
            all_available=False,
            calendar_days=days,
            use_post_decay=True,
            deploy_pct=deploy_pct,
            cfg_overrides=cfg_overrides,
        )
        summary = _summarize(rep)
        summary["days_calendar"] = days
        la = analyze_losses(rep)
        blocks.append(
            {
                "days": days,
                "summary": summary,
                "daily": [
                    {
                        "date": d.get("date"),
                        "setups": d.get("setups"),
                        "post_decay_entries": d.get("post_decay_entries"),
                        "net": d.get("net"),
                        "balance_close": d.get("balance_close"),
                        "spot_chg": d.get("spot_chg"),
                        "wins": d.get("wins"),
                        "losses": d.get("losses"),
                        "skip": d.get("skip"),
                    }
                    for d in (rep.get("daily") or [])
                ],
                "ledger": rep.get("ledger") or [],
                "metrics": score_period(summary),
                "loss_analysis": la,
            }
        )
        stem = OUT / f"overnight_{tag}_d{days}_algo_report"
        stem.with_suffix(".json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        try:
            write_detailed_html(rep, stem.with_suffix(".html"))
        except Exception as exc:  # noqa: BLE001
            print(f"  html warn: {exc}")

    w_raw = {3: 0.05, 5: 0.05, 7: 0.08, 10: 0.08, 15: 0.12, 20: 0.12, 30: 0.22, 40: 0.1, 50: 0.08, 60: 0.15}
    present = [float(w_raw.get(b["days"], 0.05)) for b in blocks]
    wsum = sum(present) or 1.0
    comp = 0.0
    for b in blocks:
        wt = float(w_raw.get(b["days"], 0.05)) / wsum
        comp += float(b["metrics"]["score"]) * wt
        # bonus for share of traded days hitting ≥5%
        ge5 = float((b.get("loss_analysis") or {}).get("pct_traded_days_ge_5") or 0)
        comp += ge5 * 0.18 * wt
        if b["days"] >= 20 and float(b["summary"].get("net_pnl") or 0) < 0:
            comp -= 45.0
        if b["days"] >= 30 and float(b["metrics"]["red_ratio"]) > 0.30:
            comp -= 25.0

    payload = {
        "title": f"Overnight multi-period ({tag}) ₹{capital:,.0f} deploy {deploy_pct:.0%}",
        "generated_at": _now(),
        "capital": capital,
        "deploy_pct": deploy_pct,
        "cfg_overrides": cfg_overrides,
        "composite_score": round(comp, 3),
        "periods": [
            {
                "days": b["days"],
                "summary": b["summary"],
                "daily": b["daily"],
                "ledger": b["ledger"],
                "metrics": b["metrics"],
                "loss_analysis": b["loss_analysis"],
            }
            for b in blocks
        ],
        "mode": "causal_minute_post_decay_independent_windows",
    }
    (OUT / f"overnight_{tag}_multi.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        write_multi_html(payload, OUT / f"overnight_{tag}_multi.html")
    except Exception as exc:  # noqa: BLE001
        print(f"multi html warn: {exc}")
    return payload


# Keys match month_cfg / expert_filters / decay_memory (percent numbers as 5.0 not 0.05)
CANDIDATES: list[tuple[str, dict[str, Any]]] = [
    ("baseline_grace", {
        "post_decay_confirm_minutes": 3,
        "post_decay_min_bar": 25,
        "post_decay_knife_drop_pct": 18.0,
        "stop_grace_minutes": 4,
        "stop_grace_pct": 8.0,
        "bank_after_win": True,
        "max_losses_before_halt": 1,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
    ("quality_grace_v1", {
        "directional_min_confidence": 0.74,
        "directional_min_margin": 0.22,
        "max_trades_per_day": 1,
        "max_losses_before_halt": 1,
        "entry_cooldown_minutes": 35,
        "signal_persist_minutes": 3,
        "entry_delay_minutes": 35,
        "prefer_entry_after_minutes": 40,
        "post_decay_min_confidence": 0.72,
        "post_decay_confirm_minutes": 4,
        "post_decay_min_bar": 30,
        "post_decay_knife_drop_pct": 15.0,
        "post_decay_already_ran_mult": 2.0,
        "stop_loss_pct": 4.5,
        "stop_grace_minutes": 5,
        "stop_grace_pct": 9.0,
        "target_pct_low": 6.0,
        "target_pct_high": 50.0,
        "lock_profit_pct": 1.5,
        "trail_giveback_pct": 4.0,
        "max_daily_loss_pct": 2.5,
        "bank_after_win": True,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
    ("green_bias_v2", {
        "directional_min_confidence": 0.78,
        "directional_min_margin": 0.26,
        "max_trades_per_day": 1,
        "max_losses_before_halt": 1,
        "entry_cooldown_minutes": 45,
        "signal_persist_minutes": 4,
        "entry_delay_minutes": 40,
        "prefer_entry_after_minutes": 45,
        "no_entry_after_minutes": 180,
        "post_decay_min_confidence": 0.76,
        "post_decay_confirm_minutes": 5,
        "post_decay_min_bar": 35,
        "post_decay_knife_drop_pct": 12.0,
        "post_decay_prefer_over_mkt": True,
        "stop_loss_pct": 4.0,
        "stop_grace_minutes": 5,
        "stop_grace_pct": 8.5,
        "target_pct_low": 7.0,
        "target_pct_high": 45.0,
        "lock_profit_pct": 2.0,
        "trail_giveback_pct": 3.5,
        "time_stop_minutes": 22,
        "max_daily_loss_pct": 2.0,
        "bank_after_win": True,
        "chop_score_threshold": 0.55,
        "require_structure_event": True,
        "require_momentum_align": True,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
    ("runner_grace_v3", {
        "directional_min_confidence": 0.72,
        "max_trades_per_day": 2,
        "max_losses_before_halt": 1,
        "entry_cooldown_minutes": 30,
        "signal_persist_minutes": 3,
        "entry_delay_minutes": 32,
        "prefer_entry_after_minutes": 35,
        "post_decay_min_confidence": 0.70,
        "post_decay_confirm_minutes": 3,
        "post_decay_min_bar": 28,
        "post_decay_knife_drop_pct": 16.0,
        "stop_loss_pct": 5.0,
        "stop_grace_minutes": 4,
        "stop_grace_pct": 8.0,
        "target_pct_low": 5.0,
        "target_pct_high": 55.0,
        "exit_at_first_target": False,
        "lock_profit_pct": 1.5,
        "trail_giveback_pct": 4.5,
        "bank_after_win": True,
        "max_daily_loss_pct": 2.5,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
    ("ultra_select_v4", {
        "directional_min_confidence": 0.80,
        "directional_min_margin": 0.28,
        "max_trades_per_day": 1,
        "max_losses_before_halt": 1,
        "entry_cooldown_minutes": 50,
        "signal_persist_minutes": 5,
        "entry_delay_minutes": 45,
        "prefer_entry_after_minutes": 50,
        "no_entry_after_minutes": 165,
        "post_decay_min_confidence": 0.80,
        "post_decay_confirm_minutes": 6,
        "post_decay_min_bar": 40,
        "post_decay_knife_drop_pct": 10.0,
        "post_decay_already_ran_mult": 2.2,
        "post_decay_prefer_over_mkt": True,
        "stop_loss_pct": 4.0,
        "stop_grace_minutes": 6,
        "stop_grace_pct": 9.0,
        "target_pct_low": 8.0,
        "target_pct_high": 40.0,
        "lock_profit_pct": 2.5,
        "trail_giveback_pct": 3.0,
        "bank_after_win": True,
        "max_daily_loss_pct": 1.8,
        "min_break_strength": 0.40,
        "chop_score_threshold": 0.50,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
    ("balanced_v5", {
        "directional_min_confidence": 0.75,
        "directional_min_margin": 0.22,
        "max_trades_per_day": 1,
        "max_losses_before_halt": 1,
        "entry_cooldown_minutes": 40,
        "signal_persist_minutes": 3,
        "entry_delay_minutes": 38,
        "prefer_entry_after_minutes": 42,
        "post_decay_min_confidence": 0.74,
        "post_decay_confirm_minutes": 4,
        "post_decay_min_bar": 32,
        "post_decay_knife_drop_pct": 14.0,
        "post_decay_prefer_over_mkt": True,
        "stop_loss_pct": 4.5,
        "stop_grace_minutes": 5,
        "stop_grace_pct": 8.5,
        "target_pct_low": 6.5,
        "target_pct_high": 48.0,
        "lock_profit_pct": 1.8,
        "trail_giveback_pct": 3.8,
        "bank_after_win": True,
        "max_daily_loss_pct": 2.2,
        "impulse_entry_enabled": False,
        "pre_event_entry_enabled": False,
    }),
]


def propose_from_analysis(base: dict[str, Any], analysis: dict[str, Any], i: int) -> dict[str, Any]:
    nxt = deepcopy(base) if base else deepcopy(CANDIDATES[1][1])
    early = int(analysis.get("early_stop_losses") or 0)
    red = int(analysis.get("red_days") or 0)
    ge5 = float(analysis.get("pct_traded_days_ge_5") or 0)
    if early >= 2:
        nxt["post_decay_min_bar"] = max(int(nxt.get("post_decay_min_bar", 25)), 35)
        nxt["post_decay_confirm_minutes"] = max(int(nxt.get("post_decay_confirm_minutes", 3)), 5)
        nxt["post_decay_knife_drop_pct"] = min(float(nxt.get("post_decay_knife_drop_pct", 20)), 12.0)
        nxt["stop_grace_minutes"] = max(int(nxt.get("stop_grace_minutes", 4)), 6)
        nxt["stop_grace_pct"] = max(float(nxt.get("stop_grace_pct", 8)), 9.0)
        nxt["stop_loss_pct"] = min(float(nxt.get("stop_loss_pct", 5)), 4.0)
    if red >= 6:
        nxt["max_trades_per_day"] = 1
        nxt["bank_after_win"] = True
        nxt["max_daily_loss_pct"] = min(float(nxt.get("max_daily_loss_pct", 3)), 2.0)
        nxt["directional_min_confidence"] = max(float(nxt.get("directional_min_confidence", 0.7)), 0.76)
    if ge5 < 40:
        # let winners run a bit more
        nxt["target_pct_high"] = max(float(nxt.get("target_pct_high", 40)), 50.0)
        nxt["trail_giveback_pct"] = min(float(nxt.get("trail_giveback_pct", 5)), 3.5)
        nxt["exit_at_first_target"] = False
    # tiny exploration
    nxt["directional_min_confidence"] = round(
        min(0.86, max(0.68, float(nxt.get("directional_min_confidence", 0.74)) + 0.01 * ((i % 3) - 1))),
        3,
    )
    nxt["impulse_entry_enabled"] = False
    nxt["pre_event_entry_enabled"] = False
    return nxt


def merge_into_config(best_overrides: dict[str, Any], tag: str) -> None:
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for k, v in best_overrides.items():
        if str(k).startswith("_"):
            continue
        cfg[k] = v
    cfg["overnight_optimized_at"] = _now()
    cfg["overnight_optimized_tag"] = tag
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--deploy", type=float, default=0.75)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--max-iters", type=int, default=10)
    ap.add_argument("--sleep-sec", type=float, default=1.0)
    args = ap.parse_args()

    log_path = OUT / "overnight_iter_log.jsonl"
    periods = [7, 15, 30] if args.quick else list(PERIODS)

    best: dict[str, Any] | None = None
    best_score = float("-inf")
    best_overrides: dict[str, Any] = {}
    best_tag = "none"
    adaptive_base: dict[str, Any] = {}

    for i in range(args.max_iters):
        if i < len(CANDIDATES):
            tag, overrides = CANDIDATES[i]
        else:
            tag = f"adapt_v{i}"
            analysis = {}
            if best:
                for b in best.get("periods") or []:
                    if b["days"] in (30, 15, 7):
                        analysis = b.get("loss_analysis") or analysis
                        if b["days"] == 30:
                            break
            overrides = propose_from_analysis(adaptive_base or best_overrides, analysis, i)

        print(f"\n===== ITER {i+1}/{args.max_iters} · {tag} =====", flush=True)
        try:
            payload = run_periods(
                capital=args.capital,
                deploy_pct=args.deploy,
                cfg_overrides=overrides,
                periods=periods,
                tag=tag,
            )
        except Exception as exc:  # noqa: BLE001
            err = {"ts": _now(), "tag": tag, "error": str(exc), "trace": traceback.format_exc()}
            log_path.open("a", encoding="utf-8").write(json.dumps(err) + "\n")
            print("FAIL", exc)
            traceback.print_exc()
            time.sleep(args.sleep_sec)
            continue

        score = float(payload.get("composite_score") or 0)
        row = {
            "ts": _now(),
            "iter": i + 1,
            "tag": tag,
            "score": score,
            "overrides": overrides,
            "periods": [
                {
                    "days": b["days"],
                    "net": b["summary"].get("net_pnl"),
                    "ret": b["summary"].get("return_pct"),
                    "G": b["summary"].get("green_days"),
                    "R": b["summary"].get("red_days"),
                    "ge5": (b.get("loss_analysis") or {}).get("pct_traded_days_ge_5"),
                    "metrics": b.get("metrics"),
                }
                for b in payload.get("periods") or []
            ],
        }
        log_path.open("a", encoding="utf-8").write(json.dumps(row) + "\n")
        print(f"SCORE {score:.2f} (best {best_score:.2f})", flush=True)
        for b in payload.get("periods") or []:
            s = b["summary"]
            la = b.get("loss_analysis") or {}
            print(
                f"  {b['days']:3d}d  G/R {s.get('green_days')}/{s.get('red_days')}  "
                f"net {float(s.get('net_pnl') or 0):+.0f}  ret {float(s.get('return_pct') or 0):+.1f}%  "
                f"ge5={la.get('pct_traded_days_ge_5')}%  score {b['metrics']['score']}",
                flush=True,
            )

        if score > best_score:
            best_score = score
            best = payload
            best_overrides = deepcopy(overrides)
            best_tag = tag
            adaptive_base = deepcopy(overrides)
            best["best_tag"] = tag
            best["best_score"] = score
            (OUT / "overnight_best_report.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
            (OUT / "overnight_best_overrides.json").write_text(
                json.dumps({"tag": tag, "overrides": best_overrides}, indent=2), encoding="utf-8"
            )
            try:
                write_multi_html(best, OUT / "overnight_best_report.html")
            except Exception:
                pass
            print(f"*** NEW BEST {tag} score={score:.2f} ***", flush=True)

        time.sleep(args.sleep_sec)

    if best is None:
        print("No successful iterations")
        return 1

    print("\n===== FINAL FULL MULTI with best =====", flush=True)
    final = run_periods(
        capital=args.capital,
        deploy_pct=args.deploy,
        cfg_overrides=best_overrides,
        periods=list(PERIODS),
        tag=f"final_{best_tag}",
    )
    (OUT / "overnight_final_multi.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
    try:
        write_multi_html(final, OUT / "overnight_final_multi.html")
    except Exception as exc:  # noqa: BLE001
        print("final html", exc)

    try:
        merge_into_config(best_overrides, best_tag)
        print("Merged best overrides into config.json")
    except Exception as exc:  # noqa: BLE001
        print("config merge failed", exc)

    print(f"DONE best={best_tag} score={best_score:.2f}")
    print(f"Reports: {OUT / 'overnight_best_report.html'}")
    print(f"         {OUT / 'overnight_final_multi.html'}")
    print(f"Log:     {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

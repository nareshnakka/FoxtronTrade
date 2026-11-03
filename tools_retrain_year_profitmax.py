"""
1-year Nifty FastTrack profit-max retrain (failure-driven).

1) Run baseline on train window → autopsy loss events
2) Search candidates that kill bleeders / let winners run
3) Pick by holdout net (aim ≥2× baseline hold) with train not collapsing
4) Write data/nifty/fasttrack_profit_overrides.json + apply into live config keys

Usage:
  .venv\\Scripts\\python.exe tools_retrain_year_profitmax.py --capital 100000 --deploy 1.0 --apply
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backtest_detailed import write_detailed_html
from backtest_month_ledger import OUT
from tools_backtest_week import aggressive_overrides
from tools_train_profit import load_days, run_range, summarize

IST = ZoneInfo("Asia/Kolkata")
NIFTY_OV = Path("data/nifty/fasttrack_profit_overrides.json")
BEST = Path("data/nifty/fasttrack_year_profitmax_best.json")
ROOT = Path(__file__).resolve().parent


def autopsy_losses(rep: dict[str, Any]) -> dict[str, Any]:
    by_ev: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "net": 0.0, "w": 0, "l": 0})
    for t in rep.get("ledger") or []:
        e = str(t.get("event") or t.get("mkt_event") or "UNK")
        net = float(t.get("net") or 0)
        by_ev[e]["n"] += 1
        by_ev[e]["net"] += net
        if net > 0:
            by_ev[e]["w"] += 1
        elif net < 0:
            by_ev[e]["l"] += 1
    ranked = sorted(by_ev.items(), key=lambda kv: kv[1]["net"])
    bleeders = [
        {"event": k, **{kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
        for k, v in ranked
        if v["net"] < 0
    ]
    winners = [
        {"event": k, **{kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
        for k, v in sorted(by_ev.items(), key=lambda kv: -kv[1]["net"])
        if v["net"] > 0
    ]
    return {"bleeders": bleeders[:12], "winners": winners[:12], "by_event": dict(by_ev)}


def score(train: dict, hold: dict, *, base_hold_net: float) -> float:
    tn = float(train["capital"]["net_pnl"])
    hn = float(hold["capital"]["net_pnl"])
    hs = int(hold["totals"]["setups"])
    ts = int(train["totals"]["setups"])
    hwr = hold["totals"]["wins"] / max(1, hold["totals"]["wins"] + hold["totals"]["losses"])
    # Maximize hold profit; require train green; bonus for beating 2× baseline hold
    sc = hn * 1.25 + tn * 0.35
    sc += 800.0 * (hwr - 0.5) * max(1, hold["totals"]["wins"] + hold["totals"]["losses"])
    sc += (hold["totals"]["green_days"] - hold["totals"]["red_days"]) * 500.0
    if hn > 0 and tn > 0:
        sc += 40_000.0
    if base_hold_net > 0 and hn >= 2.0 * base_hold_net:
        sc += 80_000.0
    elif base_hold_net > 0 and hn >= 1.5 * base_hold_net:
        sc += 35_000.0
    if hn < 0:
        sc -= 50_000.0
    if tn < 0:
        sc -= 25_000.0
    if hs < 5 or ts < 15:
        sc -= 40_000.0
    return round(sc, 2)


def candidates(base: dict[str, Any], autopsy: dict[str, Any]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = [("baseline", deepcopy(base))]
    bleed_names = [b["event"] for b in autopsy.get("bleeders") or []]

    def pack(name: str, **kw: Any) -> None:
        c = deepcopy(base)
        c.update(kw)
        out.append((name, c))

    # --- kill / gate known bleed paths ---
    if any("PA_PB" in e or "daily_pa" in e.lower() for e in bleed_names):
        pack(
            "pa_strict",
            daily_pa_ce_min_vs_open=32,
            daily_pa_pe_min_vs_open=40,
            daily_pa_ce_max_giveback=45,
            daily_pa_pe_entry_after_minutes=80,
            daily_pa_min_mom_pts=8,
        )
        pack("pa_off", enable_daily_pa=False)
    if any("SR_RECLAIM" in e for e in bleed_names):
        pack(
            "sr_strict",
            sr_hard_min_mom10=22,
            sr_reclaim_max_under_support=8,
            sr_signal_persist_minutes=2,
            directional_min_confidence=0.74,
        )
    if any("BREAKOUT" in e for e in bleed_names):
        pack(
            "orb_retest_only",
            block_orb_chase_without_retest=True,
            block_ce_mid_chase=True,
            breakout_max_extension_pts=20,
        )
        pack("orb_ce_off", enable_orb_breakout=False)

    pack(
        "deny_decay_bleed",
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush", "ce_multi_day_crush"],
        post_decay_allow_setups=["ce_half_day_crush"],
    )

    # --- profit amplifiers (let winners run / more quality size) ---
    pack(
        "winners_run",
        target_pct_low=6.0,
        target_pct_high=60.0,
        lock_profit_pct=2.0,
        trail_giveback_pct=3.0,
        stop_loss_pct=4.5,
        bank_after_win=False,
        max_trades_per_day=2,
    )
    pack(
        "winners_run_bank1",
        target_pct_low=6.0,
        target_pct_high=55.0,
        lock_profit_pct=2.5,
        trail_giveback_pct=2.8,
        stop_loss_pct=4.5,
        bank_after_win=True,
        max_trades_per_day=1,
        max_losses_before_halt=1,
    )
    pack(
        "breakdown_pd_run",
        enable_orb_breakout=False,
        enable_orb_breakdown=True,
        prefer_sr_dip_entry=True,
        enable_sr_reclaim=True,
        post_decay_allow_setups=["ce_half_day_crush"],
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        target_pct_low=6.0,
        target_pct_high=55.0,
        trail_giveback_pct=3.0,
        bank_after_win=True,
        max_trades_per_day=2,
    )
    pack(
        "pd_sr_pa_strict_run",
        enable_daily_pa=True,
        daily_pa_ce_min_vs_open=30,
        daily_pa_pe_min_vs_open=38,
        daily_pa_ce_max_giveback=50,
        enable_sr_reclaim=True,
        enable_orb_breakout=True,
        block_orb_chase_without_retest=True,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        target_pct_low=5.5,
        target_pct_high=55.0,
        lock_profit_pct=2.0,
        trail_giveback_pct=3.2,
        bank_after_win=False,
        max_trades_per_day=2,
        directional_min_confidence=0.72,
    )
    pack(
        "size_quality",
        deploy_pct_of_balance=1.0,
        max_lots_per_trade=20,
        min_lots_per_trade=1,
        directional_min_confidence=0.74,
        bank_after_win=True,
        max_trades_per_day=2,
        target_pct_low=6.0,
        trail_giveback_pct=3.0,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
    )
    pack(
        "ultra_hold_green",
        enable_daily_pa=True,
        daily_pa_ce_min_vs_open=35,
        daily_pa_pe_min_vs_open=42,
        enable_micro_scalp=False,
        enable_daily_soft_scalp=False,
        enable_sr_reclaim=True,
        sr_hard_min_mom10=20,
        enable_orb_breakout=True,
        block_orb_chase_without_retest=True,
        block_ce_mid_chase=True,
        post_decay_allow_setups=["ce_half_day_crush"],
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush", "ce_multi_day_crush"],
        max_trades_per_day=1,
        bank_after_win=True,
        max_losses_before_halt=1,
        target_pct_low=7.0,
        target_pct_high=50.0,
        trail_giveback_pct=2.8,
        directional_min_confidence=0.76,
    )
    # Combo: strict PA + winners run trail
    pack(
        "failfix_pa_trail",
        daily_pa_ce_min_vs_open=32,
        daily_pa_pe_min_vs_open=40,
        daily_pa_ce_max_giveback=45,
        block_orb_chase_without_retest=True,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        target_pct_low=6.0,
        target_pct_high=60.0,
        lock_profit_pct=2.0,
        trail_giveback_pct=2.8,
        stop_loss_pct=4.5,
        bank_after_win=False,
        max_trades_per_day=2,
    )
    return out


def apply_to_config(overrides: dict[str, Any], tag: str) -> None:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    keep = {
        "enable_sr_reclaim",
        "enable_sr_reject",
        "enable_orb_breakout",
        "enable_orb_breakdown",
        "prefer_sr_dip_entry",
        "enable_daily_pa",
        "enable_micro_scalp",
        "enable_daily_soft_scalp",
        "post_decay_deny_setups",
        "post_decay_allow_setups",
        "max_trades_per_day",
        "bank_after_win",
        "max_losses_before_halt",
        "directional_min_confidence",
        "directional_min_margin",
        "max_daily_loss_pct",
        "entry_cooldown_minutes",
        "lock_profit_pct",
        "trail_giveback_pct",
        "target_pct_low",
        "target_pct_high",
        "stop_loss_pct",
        "deploy_pct_of_balance",
        "max_lots_per_trade",
        "block_orb_chase_without_retest",
        "block_ce_mid_chase",
        "breakout_max_extension_pts",
        "daily_pa_ce_min_vs_open",
        "daily_pa_pe_min_vs_open",
        "daily_pa_ce_max_giveback",
        "sr_hard_min_mom10",
        "sr_reclaim_max_under_support",
    }
    for k, v in overrides.items():
        if k in keep:
            cfg[k] = v
    cfg["train_year_profitmax_at"] = datetime.now(IST).isoformat()
    cfg["train_year_profitmax_tag"] = tag
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--deploy", type=float, default=1.0)
    ap.add_argument("--train-start", default="2025-10-17")
    ap.add_argument("--train-end", default="2026-07-31")
    ap.add_argument("--hold-start", default="2026-08-01")
    ap.add_argument("--hold-end", default="2026-11-03")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    train_days = load_days(args.train_start, args.train_end)
    hold_days = load_days(args.hold_start, args.hold_end)
    full_days = load_days(args.train_start, args.hold_end)
    print(
        f"Nifty YEAR profitmax train={len(train_days)} hold={len(hold_days)} "
        f"full={len(full_days)} ₹{args.capital:,.0f}",
        flush=True,
    )
    if len(train_days) < 40 or len(hold_days) < 10:
        print("Insufficient sessions")
        return 1

    base = aggressive_overrides(args.capital, args.deploy)
    print("\n===== BASELINE autopsy (train) =====", flush=True)
    base_train = run_range(
        train_days, capital=args.capital, deploy=args.deploy, overrides=base, quiet=True
    )
    base_hold = run_range(
        hold_days, capital=args.capital, deploy=args.deploy, overrides=base, quiet=True
    )
    print(summarize("TRAIN", base_train), flush=True)
    print(summarize("HOLD ", base_hold), flush=True)
    auto = autopsy_losses(base_train)
    print("Bleeders:", [(b["event"], b["net"]) for b in auto["bleeders"][:6]], flush=True)
    print("Winners:", [(w["event"], w["net"]) for w in auto["winners"][:6]], flush=True)

    base_hold_net = float(base_hold["capital"]["net_pnl"])
    cands = candidates(base, auto)
    results = []
    best = None
    best_sc = float("-inf")

    for i, (name, ov) in enumerate(cands, 1):
        print(f"\n===== [{i}/{len(cands)}] {name} =====", flush=True)
        tr = run_range(
            train_days, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
        )
        ho = run_range(
            hold_days, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
        )
        sc = score(tr, ho, base_hold_net=base_hold_net)
        mult = (
            float(ho["capital"]["net_pnl"]) / base_hold_net
            if base_hold_net > 0
            else 0.0
        )
        print(summarize("TRAIN", tr), flush=True)
        print(summarize("HOLD ", ho), flush=True)
        print(f"SCORE {sc:,.0f}  hold_mult={mult:.2f}× baseline", flush=True)
        row = {
            "name": name,
            "score": sc,
            "hold_mult": round(mult, 3),
            "train_net": tr["capital"]["net_pnl"],
            "hold_net": ho["capital"]["net_pnl"],
            "train_wl": [tr["totals"]["wins"], tr["totals"]["losses"]],
            "hold_wl": [ho["totals"]["wins"], ho["totals"]["losses"]],
            "_ov": ov,
        }
        results.append(row)
        if sc > best_sc:
            best_sc = sc
            best = row
            print(f"*** NEW BEST {name} ***", flush=True)

    assert best is not None
    print(
        f"\nBEST {best['name']} hold={best['hold_net']:+,.0f} "
        f"({best['hold_mult']:.2f}×) train={best['train_net']:+,.0f}",
        flush=True,
    )

    ov = best["_ov"]
    full = run_range(
        full_days, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
    )
    print(summarize("FULL ", full), flush=True)

    # last 30d pin
    last30 = load_days("2026-10-05", "2026-11-03")
    m30 = run_range(
        last30, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
    )
    m30b = run_range(
        last30, capital=args.capital, deploy=args.deploy, overrides=base, quiet=True
    )
    print(summarize("30d BASE", m30b), flush=True)
    print(summarize("30d BEST", m30), flush=True)

    NIFTY_OV.parent.mkdir(parents=True, exist_ok=True)
    NIFTY_OV.write_text(json.dumps(ov, indent=2), encoding="utf-8")
    BEST.write_text(
        json.dumps(
            {
                "best_name": best["name"],
                "score": best["score"],
                "hold_mult_vs_baseline": best["hold_mult"],
                "baseline_hold_net": base_hold_net,
                "train": {
                    "net": best["train_net"],
                    "wl": best["train_wl"],
                    "range": [args.train_start, args.train_end],
                },
                "hold": {
                    "net": best["hold_net"],
                    "wl": best["hold_wl"],
                    "range": [args.hold_start, args.hold_end],
                },
                "full_net": full["capital"]["net_pnl"],
                "last30_base": m30b["capital"]["net_pnl"],
                "last30_best": m30["capital"]["net_pnl"],
                "autopsy_bleeders": auto["bleeders"][:8],
                "autopsy_winners": auto["winners"][:8],
                "ranking": [
                    {k: v for k, v in r.items() if k != "_ov"}
                    for r in sorted(results, key=lambda x: -x["score"])
                ],
                "lesson": (
                    "1y failure-driven profitmax: gate bleed events from train autopsy, "
                    "amplify winners with trail/target; select by holdout net (2× bonus)."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    rep = {
        "title": f"Nifty YEAR PROFITMAX · {best['name']} · {args.hold_start}→{args.hold_end}",
        "generated_at": datetime.now(IST).isoformat(),
        "profile": "FastTrack",
        "mode": "year_profitmax_holdout",
        "range": full["range"],
        "capital": full["capital"],
        "totals": full["totals"],
        "daily": full["daily"],
        "ledger": full["ledger"],
        "config_snapshot": {
            "best_name": best["name"],
            "hold_mult": best["hold_mult"],
            "last30_best": m30["capital"]["net_pnl"],
            "last30_base": m30b["capital"]["net_pnl"],
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jp = OUT / "nifty_year_profitmax_report.json"
    hp = OUT / "nifty_year_profitmax_report.html"
    jp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    write_detailed_html(rep, hp)

    if args.apply:
        apply_to_config(ov, best["name"])
        print("Applied gates → config.json", flush=True)

    print(f"OVERRIDES → {NIFTY_OV}")
    print(f"BEST → {BEST}")
    print(f"HTML {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

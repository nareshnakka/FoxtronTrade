"""
Walk-forward profit trainer (causal, no look-ahead).

Train 2026-01-01→2026-07-31, holdout 2026-08-01→2026-11-02,
then pin-to-pin full year on the winner.

Hypothesis candidates kill the known bleeders from the Jan→Nov autopsy:
  SR_RECLAIM, BREAKOUT, three_day_fall, pe_multi_day_crush
and keep winners: BREAKDOWN, ce_half_day_crush (+ optional CE multi-day).

Writes:
  data/backtests/train_profit_log.jsonl
  data/backtests/train_profit_best.json
  data/backtests/train_profit_full_algo_report.{json,html}
  data/backtests/train_profit_overrides.json
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest_detailed import (  # noqa: E402
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
    write_detailed_html,
)
from backtest_month_ledger import OUT, LedgerState, _load_month_days, month_cfg  # noqa: E402
from foxtrade.decay_memory import DecayMemory  # noqa: E402
from tools_backtest_week import aggressive_overrides  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")


def base_aggressive(capital: float, deploy: float) -> dict[str, Any]:
    return aggressive_overrides(capital, deploy)


def candidates(capital: float, deploy: float) -> list[tuple[str, dict[str, Any]]]:
    b = base_aggressive(capital, deploy)
    out: list[tuple[str, dict[str, Any]]] = [("baseline", deepcopy(b))]

    def pack(name: str, **kw: Any) -> None:
        c = deepcopy(b)
        c.update(kw)
        out.append((name, c))

    pack("kill_sr_reclaim", enable_sr_reclaim=False)
    pack("kill_breakout", enable_orb_breakout=False)
    pack(
        "deny_bad_decay",
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
    )
    pack(
        "kill_bleeders",
        enable_sr_reclaim=False,
        enable_orb_breakout=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
    )
    pack(
        "winners_only",
        enable_sr_reclaim=False,
        enable_sr_reject=False,
        enable_orb_breakout=False,
        enable_orb_breakdown=True,
        prefer_sr_dip_entry=False,
        post_decay_allow_setups=["ce_half_day_crush", "ce_multi_day_crush"],
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
    )
    pack(
        "winners_bank",
        enable_sr_reclaim=False,
        enable_sr_reject=False,
        enable_orb_breakout=False,
        prefer_sr_dip_entry=False,
        post_decay_allow_setups=["ce_half_day_crush", "ce_multi_day_crush"],
        max_trades_per_day=1,
        bank_after_win=True,
        max_losses_before_halt=1,
        max_daily_loss_pct=2.5,
    )
    pack(
        "breakdown_cehalf",
        enable_sr_reclaim=False,
        enable_sr_reject=False,
        enable_orb_breakout=False,
        prefer_sr_dip_entry=False,
        post_decay_allow_setups=["ce_half_day_crush"],
        max_trades_per_day=1,
        bank_after_win=True,
        max_losses_before_halt=1,
    )
    pack(
        "bleeders_select",
        enable_sr_reclaim=False,
        enable_orb_breakout=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        directional_min_confidence=0.76,
        directional_min_margin=0.24,
        max_trades_per_day=1,
        bank_after_win=True,
        max_losses_before_halt=1,
        max_daily_loss_pct=2.5,
        entry_cooldown_minutes=35,
    )
    pack(
        "bleeders_trail",
        enable_sr_reclaim=False,
        enable_orb_breakout=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        stop_grace_minutes=5,
        stop_grace_pct=9.0,
        lock_profit_pct=2.0,
        trail_giveback_pct=3.5,
        target_pct_low=6.0,
        bank_after_win=True,
        max_trades_per_day=1,
        max_losses_before_halt=1,
    )
    pack(
        "kill_all_sr_breakout",
        enable_sr_reclaim=False,
        enable_sr_reject=False,
        enable_orb_breakout=False,
        prefer_sr_dip_entry=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        bank_after_win=True,
        max_trades_per_day=1,
        max_losses_before_halt=1,
    )
    pack(
        "size_down_bleeders",
        enable_sr_reclaim=False,
        enable_orb_breakout=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        deploy_pct_of_balance=0.50,
        max_lots_per_trade=12,
        bank_after_win=True,
        max_trades_per_day=1,
        max_losses_before_halt=1,
    )
    pack(
        "reject_keep_bd",
        enable_sr_reclaim=False,
        enable_sr_reject=True,
        enable_orb_breakout=False,
        post_decay_deny_setups=["three_day_fall", "pe_multi_day_crush"],
        bank_after_win=True,
        max_trades_per_day=1,
        max_losses_before_halt=1,
        directional_min_confidence=0.74,
    )
    pack(
        "ultra_ce_bd",
        enable_sr_reclaim=False,
        enable_sr_reject=False,
        enable_orb_breakout=False,
        prefer_sr_dip_entry=False,
        post_decay_allow_setups=["ce_half_day_crush"],
        post_decay_min_confidence=0.72,
        post_decay_confirm_minutes=3,
        post_decay_min_bar=25,
        max_trades_per_day=1,
        bank_after_win=True,
        max_losses_before_halt=1,
        max_daily_loss_pct=2.0,
        stop_loss_pct=4.5,
        lock_profit_pct=2.0,
        trail_giveback_pct=3.5,
    )
    return out


def load_days(start: str, end: str) -> list[dict[str, Any]]:
    end_d = date.fromisoformat(end)
    start_d = date.fromisoformat(start)
    span = (end_d - start_d).days + 5
    days = _load_month_days(as_of=end_d, calendar_days=max(span, 1), all_available=False)
    return [d for d in days if start <= d["date"] <= end]


def run_range(
    days: list[dict[str, Any]],
    *,
    capital: float,
    deploy: float,
    overrides: dict[str, Any],
    quiet: bool = True,
) -> dict[str, Any]:
    if not days:
        return {
            "capital": {"initial": capital, "final_balance": capital, "net_pnl": 0.0, "return_pct": 0.0},
            "totals": {
                "session_days": 0,
                "traded_days": 0,
                "setups": 0,
                "wins": 0,
                "losses": 0,
                "green_days": 0,
                "red_days": 0,
                "gross": 0.0,
                "charges": 0.0,
                "net": 0.0,
                "post_decay_entries": 0,
            },
            "daily": [],
            "ledger": [],
            "range": {"start": None, "end": None},
        }

    cfg = month_cfg()
    cfg.update(overrides)
    cfg["capital_inr"] = float(capital)
    cfg["deploy_pct_of_balance"] = float(cfg.get("deploy_pct_of_balance") or deploy)
    deploy_use = float(cfg["deploy_pct_of_balance"])

    state = LedgerState(
        initial=float(capital),
        balance=float(capital),
        deploy_pct=deploy_use,
        deploy_cap=0.0,
    )
    seed = _seed_decay_days_before(days[0]["date"])
    decay_mem = DecayMemory(persist=False, seed_days=seed)
    daily: list[dict[str, Any]] = []

    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
            verbose=not quiet,
        )
        decay_mem.finalize_day(d["date"])
        daily.append(row)
        if state.halted:
            break

    ledger = list(state.ledger)
    tot_gross = sum(float(r.get("gross") or 0) for r in daily)
    tot_chg = sum(float(r.get("charges") or 0) for r in daily)
    tot_net = sum(float(r.get("net") or 0) for r in daily)
    tot_invested = sum(float(t.get("invested") or 0) for t in ledger)
    tot_lots = sum(int(r.get("lots_total") or 0) for r in daily)
    post_n = sum(int(r.get("post_decay_entries") or 0) for r in daily)
    wins = sum(int(r.get("wins") or 0) for r in daily)
    losses = sum(int(r.get("losses") or 0) for r in daily)
    setups = sum(int(r.get("setups") or 0) for r in daily)
    green = sum(1 for r in daily if float(r.get("net") or 0) > 0)
    red = sum(1 for r in daily if float(r.get("net") or 0) < 0)
    traded = sum(1 for r in daily if int(r.get("setups") or 0) > 0)

    # event expectancy snapshot
    by_ev: dict[str, dict[str, float]] = {}
    for t in ledger:
        e = str(t.get("event") or "UNK")
        slot = by_ev.setdefault(e, {"n": 0, "net": 0.0, "w": 0})
        slot["n"] += 1
        slot["net"] += float(t.get("net") or 0)
        if float(t.get("net") or 0) > 0:
            slot["w"] += 1

    return {
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "capital": {
            "initial": float(capital),
            "deploy_pct": deploy_use,
            "deploy_cap": 0.0,
            "halt_pct": getattr(state, "halt_pct", 0.3),
            "halt_floor": getattr(state, "halt_floor", float(capital) * 0.3),
            "final_balance": round(state.balance, 2),
            "net_pnl": round(state.balance - float(capital), 2),
            "return_pct": round(100.0 * (state.balance / float(capital) - 1.0), 2),
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "totals": {
            "session_days": len(daily),
            "traded_days": traded,
            "setups": setups,
            "wins": wins,
            "losses": losses,
            "green_days": green,
            "red_days": red,
            "lots": tot_lots,
            "invested_total": round(tot_invested, 2),
            "gross": round(tot_gross, 2),
            "charges": round(tot_chg, 2),
            "net": round(tot_net, 2),
            "post_decay_entries": post_n,
            "minutes_processed": sum(int(r.get("minutes") or 0) for r in daily),
            "analyses": sum(int(r.get("analyses") or 0) for r in daily),
            "signal_minutes": sum(int(r.get("allow_true") or 0) for r in daily),
        },
        "by_event": {
            k: {
                "n": int(v["n"]),
                "net": round(v["net"], 2),
                "wr": round(100.0 * v["w"] / v["n"], 1) if v["n"] else 0.0,
            }
            for k, v in sorted(by_ev.items(), key=lambda kv: kv[1]["net"])
        },
        "daily": daily,
        "ledger": ledger,
        "config_snapshot": {
            k: overrides.get(k)
            for k in (
                "enable_sr_reclaim",
                "enable_sr_reject",
                "enable_orb_breakout",
                "enable_orb_breakdown",
                "prefer_sr_dip_entry",
                "post_decay_deny_setups",
                "post_decay_allow_setups",
                "max_trades_per_day",
                "bank_after_win",
                "max_losses_before_halt",
                "directional_min_confidence",
                "deploy_pct_of_balance",
                "stop_loss_pct",
                "max_daily_loss_pct",
            )
            if k in overrides
        },
    }


def score_pair(train: dict[str, Any], hold: dict[str, Any]) -> float:
    """Prefer holdout profit, require train not disastrous; penalize zero activity."""
    tn = float(train["capital"]["net_pnl"])
    hn = float(hold["capital"]["net_pnl"])
    tr = float(train["capital"]["return_pct"])
    hr = float(hold["capital"]["return_pct"])
    ts = int(train["totals"]["setups"])
    hs = int(hold["totals"]["setups"])
    tg = int(train["totals"]["green_days"])
    hg = int(hold["totals"]["green_days"])
    tred = int(train["totals"]["red_days"])
    hred = int(hold["totals"]["red_days"])

    score = hn * 1.0 + tn * 0.35
    score += hr * 80.0 + tr * 25.0
    score += (hg - hred) * 400.0 + (tg - tred) * 120.0

    # hard preference: both windows green
    if hn > 0 and tn > 0:
        score += 50_000.0
    elif hn > 0:
        score += 15_000.0
    if hn < 0:
        score -= 25_000.0
    if tn < -40_000:
        score -= 20_000.0
    if hs < 3 or ts < 8:
        score -= 30_000.0  # too inactive / overfit silence
    if hold["capital"].get("halted") or train["capital"].get("halted"):
        score -= 10_000.0
    return round(score, 2)


def summarize(tag: str, rep: dict[str, Any]) -> str:
    c, t = rep["capital"], rep["totals"]
    return (
        f"{tag} {rep['range']['start']}→{rep['range']['end']} "
        f"net={c['net_pnl']:+,.0f} ({c['return_pct']:+.1f}%) "
        f"setups={t['setups']} W/L {t['wins']}/{t['losses']} "
        f"G/R {t['green_days']}/{t['red_days']}"
    )


def merge_into_config(overrides: dict[str, Any], tag: str) -> None:
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    keep = {
        "enable_sr_reclaim",
        "enable_sr_reject",
        "enable_orb_breakout",
        "enable_orb_breakdown",
        "prefer_sr_dip_entry",
        "post_decay_deny_setups",
        "post_decay_allow_setups",
        "max_trades_per_day",
        "bank_after_win",
        "max_losses_before_halt",
        "directional_min_confidence",
        "directional_min_margin",
        "max_daily_loss_pct",
        "entry_cooldown_minutes",
        "stop_grace_minutes",
        "stop_grace_pct",
        "lock_profit_pct",
        "trail_giveback_pct",
        "target_pct_low",
        "stop_loss_pct",
        "post_decay_min_confidence",
        "post_decay_confirm_minutes",
        "post_decay_min_bar",
        "deploy_pct_of_balance",
        "max_lots_per_trade",
    }
    for k, v in overrides.items():
        if k in keep:
            cfg[k] = v
    cfg["train_profit_optimized_at"] = _now()
    cfg["train_profit_tag"] = tag
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=200_000.0)
    ap.add_argument("--deploy", type=float, default=0.75)
    ap.add_argument("--train-start", default="2026-01-01")
    ap.add_argument("--train-end", default="2026-07-31")
    ap.add_argument("--hold-start", default="2026-08-01")
    ap.add_argument("--hold-end", default="2026-11-02")
    ap.add_argument("--full-start", default="2026-01-01")
    ap.add_argument("--full-end", default="2026-11-02")
    ap.add_argument("--apply", action="store_true", help="merge best gates into config.json")
    ap.add_argument("--only", default="", help="comma tags to run (empty=all)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    log_path = OUT / "train_profit_log.jsonl"

    train_days = load_days(args.train_start, args.train_end)
    hold_days = load_days(args.hold_start, args.hold_end)
    full_days = load_days(args.full_start, args.full_end)
    print(
        f"Train {len(train_days)} sessions · Holdout {len(hold_days)} · Full {len(full_days)}",
        flush=True,
    )

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cands = candidates(args.capital, args.deploy)
    if only:
        cands = [(n, o) for n, o in cands if n in only]

    best_score = float("-inf")
    best_tag = ""
    best_overrides: dict[str, Any] = {}
    best_row: dict[str, Any] | None = None
    ranking: list[dict[str, Any]] = []

    for i, (tag, ov) in enumerate(cands, 1):
        print(f"\n===== [{i}/{len(cands)}] {tag} =====", flush=True)
        train = run_range(
            train_days, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
        )
        hold = run_range(
            hold_days, capital=args.capital, deploy=args.deploy, overrides=ov, quiet=True
        )
        sc = score_pair(train, hold)
        print(f"  {summarize('TRAIN', train)}", flush=True)
        print(f"  {summarize('HOLD ', hold)}", flush=True)
        print(f"  SCORE {sc:,.1f}", flush=True)

        row = {
            "ts": _now(),
            "tag": tag,
            "score": sc,
            "train": {
                "net": train["capital"]["net_pnl"],
                "ret": train["capital"]["return_pct"],
                "setups": train["totals"]["setups"],
                "wl": [train["totals"]["wins"], train["totals"]["losses"]],
                "gr": [train["totals"]["green_days"], train["totals"]["red_days"]],
                "by_event": train.get("by_event"),
            },
            "hold": {
                "net": hold["capital"]["net_pnl"],
                "ret": hold["capital"]["return_pct"],
                "setups": hold["totals"]["setups"],
                "wl": [hold["totals"]["wins"], hold["totals"]["losses"]],
                "gr": [hold["totals"]["green_days"], hold["totals"]["red_days"]],
                "by_event": hold.get("by_event"),
            },
            "overrides": ov,
        }
        log_path.open("a", encoding="utf-8").write(json.dumps(row) + "\n")
        ranking.append(
            {
                "tag": tag,
                "score": sc,
                "train_net": train["capital"]["net_pnl"],
                "hold_net": hold["capital"]["net_pnl"],
                "train_ret": train["capital"]["return_pct"],
                "hold_ret": hold["capital"]["return_pct"],
                "both_green": train["capital"]["net_pnl"] > 0 and hold["capital"]["net_pnl"] > 0,
            }
        )
        if sc > best_score:
            best_score = sc
            best_tag = tag
            best_overrides = deepcopy(ov)
            best_row = row
            print(f"  *** NEW BEST {tag} ***", flush=True)

    ranking.sort(key=lambda x: -float(x["score"]))
    print("\n===== RANKING =====", flush=True)
    for r in ranking:
        flag = " ★" if r["tag"] == best_tag else ""
        both = " BOTH+" if r["both_green"] else ""
        print(
            f"  {r['tag']:22s} score={r['score']:>10,.0f}  "
            f"train={r['train_net']:+8,.0f} hold={r['hold_net']:+8,.0f}{both}{flag}",
            flush=True,
        )

    print(f"\n===== FULL PIN-TO-PIN · {best_tag} =====", flush=True)
    full = run_range(
        full_days,
        capital=args.capital,
        deploy=args.deploy,
        overrides=best_overrides,
        quiet=True,
    )
    print(f"  {summarize('FULL ', full)}", flush=True)

    # also baseline full for delta
    baseline_ov = base_aggressive(args.capital, args.deploy)
    base_full = run_range(
        full_days,
        capital=args.capital,
        deploy=args.deploy,
        overrides=baseline_ov,
        quiet=True,
    )
    print(f"  {summarize('BASE ', base_full)}", flush=True)
    delta = float(full["capital"]["net_pnl"]) - float(base_full["capital"]["net_pnl"])
    print(f"  DELTA vs baseline {delta:+,.0f}", flush=True)

    rep = {
        "title": f"Train-profit optimized · {best_tag} · {args.full_start}→{args.full_end}",
        "generated_at": datetime.now(IST).isoformat(),
        "mode": "causal_walk_forward_train_holdout_full",
        "best_tag": best_tag,
        "best_score": best_score,
        "ranking": ranking,
        "train_window": {"start": args.train_start, "end": args.train_end},
        "hold_window": {"start": args.hold_start, "end": args.hold_end},
        "profile": "paper_1l_aggressive_100 / trained",
        "range": full["range"],
        "capital": full["capital"],
        "totals": full["totals"],
        "by_event": full.get("by_event"),
        "baseline_full": {
            "capital": base_full["capital"],
            "totals": base_full["totals"],
            "by_event": base_full.get("by_event"),
        },
        "delta_net_vs_baseline": round(delta, 2),
        "config_snapshot": full.get("config_snapshot"),
        "daily": full["daily"],
        "ledger": full["ledger"],
        "premium_model": (
            "Synthetic ATM premium updated EVERY minute from spot Δ "
            "(delta≈0.5 + theta). Walk-forward trained gates."
        ),
    }

    (OUT / "train_profit_best.json").write_text(
        json.dumps(
            {
                "tag": best_tag,
                "score": best_score,
                "overrides": best_overrides,
                "row": best_row,
                "ranking": ranking,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "train_profit_overrides.json").write_text(
        json.dumps({"tag": best_tag, "overrides": best_overrides}, indent=2),
        encoding="utf-8",
    )
    jp = OUT / "train_profit_full_algo_report.json"
    hp = OUT / "train_profit_full_algo_report.html"
    jp.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    try:
        write_detailed_html(rep, hp)
    except Exception as exc:  # noqa: BLE001
        print(f"html warn: {exc}")

    if args.apply:
        merge_into_config(best_overrides, best_tag)
        print("Merged best gates into config.json", flush=True)

    print(f"\nBEST {best_tag} score={best_score:,.1f}")
    print(f"Full net {full['capital']['net_pnl']:+,.0f}  (baseline {base_full['capital']['net_pnl']:+,.0f})")
    print(f"Overrides {OUT / 'train_profit_overrides.json'}")
    print(f"Report    {hp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

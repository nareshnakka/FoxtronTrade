"""Why last ~30d traded so few days — causal denial autopsy."""
from __future__ import annotations

from collections import Counter
from datetime import date

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
)
from backtest_month_ledger import LedgerState, _load_month_days, month_cfg
from foxtrade.daily_soft import merge_soft_cfg, soft_enabled
from foxtrade.decay_memory import DecayMemory
from foxtrade.market_direction import analyze_market
from foxtrade.micro_scalp import detect_pullback_resume, micro_enabled
from tools_backtest_week import aggressive_overrides


def classify(bars, cfg, is_exp):
    closes = [float(b["close"]) for b in bars]
    if len(closes) < 40:
        return "no_data", {}
    max_i = min(len(closes) - 1, int(cfg.get("no_entry_after_minutes", 300)))
    counts: Counter[str] = Counter()
    best_conf = -1.0
    best = None
    primary_near = soft_near = micro_near = 0
    for i in range(20, max_i + 1):
        a = analyze_market(closes[: i + 1], cfg=cfg, is_expiry_day=is_exp)
        allow = bool(a.get("allow_entry")) and a.get("side") in ("CE", "PE")
        reason = str(a.get("reason") or "")
        conf = float(a.get("confidence") or 0)
        if allow:
            primary_near += 1
            bucket = "PRIMARY_ALLOW"
        else:
            r = reason.lower()
            if "chop" in r:
                bucket = "chop"
            elif "needs spot" in r or "vs open" in r:
                bucket = "vs-open gate"
            elif "orb" in r or "retest" in r or "chase" in r:
                bucket = "ORB/retest"
            elif "efficiency" in r or "path" in r:
                bucket = "path efficiency"
            elif "wait structure" in r or "conf" in r or "margin" in r:
                bucket = "conf/structure"
            elif "expiry" in r:
                bucket = "expiry window"
            elif "mom" in r:
                bucket = "momentum"
            elif "extreme" in r:
                bucket = "session extreme"
            elif "sharp" in r or "fall" in r or "crush" in r:
                bucket = "sharp-fall"
            else:
                bucket = "other"
            counts[bucket] += 1
        if soft_enabled(cfg) and 150 <= i < 225:
            sa = analyze_market(
                closes[: i + 1], cfg=merge_soft_cfg(cfg), is_expiry_day=False
            )
            if sa.get("allow_entry") and sa.get("side") == "PE":
                soft_near += 1
        if micro_enabled(cfg):
            ms = detect_pullback_resume(closes, cfg, bar_i=i)
            if ms.get("allow_entry"):
                micro_near += 1
        if conf >= best_conf:
            best_conf = conf
            best = {
                "side": a.get("side"),
                "conf": round(conf, 2),
                "event": a.get("event"),
                "reason": reason[:100],
                "allow": allow,
            }
    top = counts.most_common(1)[0][0] if counts else "n/a"
    return top, {
        "primary_allow_mins": primary_near,
        "soft_pe_mins": soft_near,
        "micro_mins": micro_near,
        "top_denials": counts.most_common(3),
        "best": best,
        "rng": round(max(closes) - min(closes), 1),
        "oc": round(closes[-1] - closes[0], 1),
    }


def main() -> int:
    as_of = date(2026, 11, 3)
    days = _load_month_days(as_of=as_of, calendar_days=30, all_available=False)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(100_000, 1.0))
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e5, balance=1e5, deploy_pct=1.0, deploy_cap=0.0)

    traded = flat = 0
    why = Counter()
    print(
        f"{'date':12} {'set':4} {'rng':7} {'oc':8} {'why':16} "
        f"{'P':>3} {'S':>3} {'M':>3}  best"
    )
    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        row = simulate_day_detailed(
            bars,
            cfg,
            st,
            day=d["date"],
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=dm,
            ce_open=ce0,
            pe_open=pe0,
            verbose=False,
        )
        dm.finalize_day(d["date"])
        setups = int(row.get("setups") or 0)
        if setups > 0:
            traded += 1
            print(f"{d['date']:12} {setups:<4} {'':7} {'':8} TRADED")
            continue
        if len(bars) < 40:
            flat += 1
            why["no bars"] += 1
            print(f"{d['date']:12} 0    -       -        no bars")
            continue
        flat += 1
        top, info = classify(bars, cfg, bool(d.get("is_expiry_day")))
        why[top] += 1
        b = info["best"] or {}
        print(
            f"{d['date']:12} 0    {info['rng']:<7} {info['oc']:<+8.1f} {top:16} "
            f"{info['primary_allow_mins']:3} {info['soft_pe_mins']:3} {info['micro_mins']:3}  "
            f"{b.get('side')} {b.get('event')} conf={b.get('conf')} | {b.get('reason','')[:65]}"
        )

    print()
    print(f"Traded {traded}/{len(days)} · flat {flat}")
    print("Flat primary denial mode:")
    for k, v in why.most_common():
        print(f"  {k:20} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Learn daily PA params: maximize traded days with positive net (30d + full)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
)
from backtest_month_ledger import LedgerState, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides

OUT = Path("data/backtests/daily_pa_learn.json")


def run_window(cfg: dict, *, days_n=None, all_avail=False) -> dict:
    as_of = date(2026, 11, 3)
    days = _load_month_days(
        as_of=as_of, calendar_days=days_n or 200, all_available=all_avail
    )
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e5, balance=1e5, deploy_pct=1.0, deploy_cap=0.0)
    traded = wins = losses = 0
    pa_n = pa_net = 0.0
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
        if int(row.get("setups") or 0) > 0:
            traded += 1
        wins += int(row.get("wins") or 0)
        losses += int(row.get("losses") or 0)
        pa_n += int(row.get("pa_entries") or 0)
        for t in row.get("trades") or []:
            if t.get("daily_pa"):
                pa_net += float(t.get("net") or 0)
    sessions = sum(1 for d in days if len(_load_bars(d["minutes_path"])) >= 40)
    return {
        "sessions": sessions,
        "days": len(days),
        "traded": traded,
        "coverage_pct": round(100.0 * traded / max(1, sessions), 1),
        "wl": f"{wins}/{losses}",
        "net": round(st.balance - 1e5),
        "pa_n": int(pa_n),
        "pa_net": round(pa_net),
    }


def main() -> int:
    base = month_cfg()
    base.update(aggressive_overrides(100_000, 1.0))
    base["enable_daily_pa"] = True

    combos = []
    for min_vs in (15, 20, 25, 30):
        for min_mom in (6, 8, 10, 12):
            for after, until in ((40, 280), (45, 270), (55, 250)):
                for prox in (10, 14):
                    for lots in (4, 6):
                        over = {
                            "daily_pa_min_vs_open": min_vs,
                            "daily_pa_min_mom_pts": min_mom,
                            "daily_pa_entry_after_minutes": after,
                            "daily_pa_no_entry_after_minutes": until,
                            "daily_pa_sr_proximity_pts": prox,
                            "daily_pa_max_lots": lots,
                            "daily_pa_spot_target_pts": 12,
                            "daily_pa_target_pct_low": 4.0,
                            "daily_pa_stop_loss_pct": 2.5,
                        }
                        combos.append(over)

    # Keep grid tractable — sample key combos
    key = []
    for min_vs in (15, 20, 25):
        for min_mom in (6, 8, 10):
            for after, until in ((40, 280), (45, 270)):
                for lots in (4, 6):
                    key.append(
                        {
                            "daily_pa_min_vs_open": min_vs,
                            "daily_pa_min_mom_pts": min_mom,
                            "daily_pa_entry_after_minutes": after,
                            "daily_pa_no_entry_after_minutes": until,
                            "daily_pa_sr_proximity_pts": 12,
                            "daily_pa_max_lots": lots,
                            "daily_pa_spot_target_pts": 12,
                            "daily_pa_target_pct_low": 4.0,
                            "daily_pa_stop_loss_pct": 2.5,
                            "daily_pa_require_vs_bias": True,
                        }
                    )
    # also no-bias variants
    for min_vs in (15, 20):
        for min_mom in (8, 10):
            key.append(
                {
                    "daily_pa_min_vs_open": min_vs,
                    "daily_pa_min_mom_pts": min_mom,
                    "daily_pa_entry_after_minutes": 45,
                    "daily_pa_no_entry_after_minutes": 270,
                    "daily_pa_sr_proximity_pts": 12,
                    "daily_pa_max_lots": 5,
                    "daily_pa_spot_target_pts": 12,
                    "daily_pa_require_vs_bias": False,
                }
            )

    results = []
    print(f"Testing {len(key)} combos…")
    for i, over in enumerate(key, 1):
        cfg = {**base, **over}
        r30 = run_window(cfg, days_n=30)
        # skip full if 30d net terrible
        if r30["net"] < -15000 and r30["coverage_pct"] < 40:
            continue
        rfull = run_window(cfg, all_avail=True)
        score = (
            r30["coverage_pct"] * 2
            + rfull["coverage_pct"]
            + (r30["net"] / 1000.0)
            + (rfull["net"] / 5000.0)
        )
        # heavily penalize negative 30d
        if r30["net"] < 0:
            score -= 50
        if rfull["net"] < 150000:  # below soft+micro baseline ~190k
            score -= 20
        row = {
            "over": over,
            "r30": r30,
            "rfull": rfull,
            "score": round(score, 1),
        }
        results.append(row)
        if i % 8 == 0 or i == len(key):
            print(f"  …{i}/{len(key)}")

    results.sort(key=lambda x: (x["score"], x["r30"]["coverage_pct"]), reverse=True)
    print("\nTOP 8:")
    for r in results[:8]:
        o = r["over"]
        print(
            f" score {r['score']} | 30d cov {r['r30']['coverage_pct']}% "
            f"net {r['r30']['net']:+} W/L {r['r30']['wl']} pa {r['r30']['pa_n']}/{r['r30']['pa_net']:+} "
            f"| full cov {r['rfull']['coverage_pct']}% net {r['rfull']['net']:+} "
            f"vs={o['daily_pa_min_vs_open']} mom={o['daily_pa_min_mom_pts']} "
            f"win={o['daily_pa_entry_after_minutes']}-{o['daily_pa_no_entry_after_minutes']} "
            f"L{o['daily_pa_max_lots']}"
        )

    # baseline without daily_pa
    off = {**base, "enable_daily_pa": False}
    b30 = run_window(off, days_n=30)
    bfull = run_window(off, all_avail=True)
    print("\nBASELINE off:", "30d", b30, "full", bfull)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {"baseline_30": b30, "baseline_full": bfull, "top": results[:15]},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"JSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

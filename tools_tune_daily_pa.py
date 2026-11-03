from datetime import date

from backtest_detailed import (
    _day_prem_opens,
    _load_bars,
    _seed_decay_days_before,
    simulate_day_detailed,
)
from backtest_month_ledger import LedgerState, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides


def run(label, days_n=None, all_avail=False, **over):
    as_of = date(2026, 11, 3)
    days = _load_month_days(
        as_of=as_of, calendar_days=days_n or 200, all_available=all_avail
    )
    cfg = month_cfg()
    cfg.update(aggressive_overrides(100_000, 1.0))
    cfg["enable_daily_pa"] = True
    cfg.update(over)
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e5, balance=1e5, deploy_pct=1.0, deploy_cap=0.0)
    traded = wins = losses = pa_n = 0
    pa_net = 0.0
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
    sess = sum(1 for d in days if len(_load_bars(d["minutes_path"])) >= 40)
    print(
        label,
        {
            "cov": round(100 * traded / max(1, sess), 1),
            "traded": f"{traded}/{sess}",
            "wl": f"{wins}/{losses}",
            "net": round(st.balance - 1e5),
            "pa": pa_n,
            "pa_net": round(pa_net),
        },
    )


def main() -> int:
    combos = [
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=6,
            daily_pa_max_lots=4,
            daily_pa_stop_loss_pct=2.0,
            daily_pa_target_pct_low=3.5,
            daily_pa_spot_target_pts=10,
        ),
        dict(
            daily_pa_min_vs_open=25,
            daily_pa_min_mom_pts=10,
            daily_pa_max_lots=5,
            daily_pa_entry_after_minutes=60,
            daily_pa_no_entry_after_minutes=240,
        ),
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=8,
            daily_pa_max_lots=4,
            daily_pa_entry_after_minutes=55,
            daily_pa_no_entry_after_minutes=225,
            daily_pa_sr_proximity_pts=10,
        ),
        dict(
            daily_pa_min_vs_open=22,
            daily_pa_min_mom_pts=8,
            daily_pa_max_lots=5,
            daily_pa_turn_bars=3,
            daily_pa_entry_after_minutes=50,
        ),
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=6,
            daily_pa_max_lots=5,
            daily_pa_persist_minutes=2,
        ),
        dict(
            daily_pa_min_vs_open=30,
            daily_pa_min_mom_pts=10,
            daily_pa_max_lots=5,
            daily_pa_entry_after_minutes=45,
            daily_pa_no_entry_after_minutes=250,
        ),
        # best 30d candidate
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=6,
            daily_pa_max_lots=5,
        ),
        # SR-only style: tighter prox + stronger mom
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=10,
            daily_pa_max_lots=5,
            daily_pa_sr_proximity_pts=8,
            daily_pa_entry_after_minutes=50,
            daily_pa_no_entry_after_minutes=240,
            daily_pa_stop_loss_pct=2.2,
            daily_pa_target_pct_low=4.0,
            daily_pa_spot_target_pts=12,
        ),
        # no mom-break chase — S/R + pullback only
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=6,
            daily_pa_max_lots=5,
            daily_pa_allow_mom_break=False,
        ),
        dict(
            daily_pa_min_vs_open=20,
            daily_pa_min_mom_pts=8,
            daily_pa_max_lots=5,
            daily_pa_allow_mom_break=False,
            daily_pa_entry_after_minutes=50,
            daily_pa_no_entry_after_minutes=240,
        ),
    ]
    for c in combos:
        tag = (
            f"vs{c.get('daily_pa_min_vs_open')} "
            f"mom{c.get('daily_pa_min_mom_pts')} "
            f"L{c.get('daily_pa_max_lots')} "
            f"a{c.get('daily_pa_entry_after_minutes', 45)}"
        )
        print("---", tag)
        run("30d", days_n=30, **c)
        run("full", all_avail=True, **c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

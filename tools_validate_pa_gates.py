"""Validate PA loss gates on 30d + full."""
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

BASE_OFF = {
    "daily_pa_pe_min_vs_open": 20,
    "daily_pa_ce_min_vs_open": 20,
    "daily_pa_pe_entry_after_minutes": 45,
    "daily_pa_min_path_efficiency": 0.0,
    "daily_pa_max_orb_width_for_pb": 999,
    "daily_pa_pb_min_giveback": 0,
    "daily_pa_pb_max_giveback": 999,
    "daily_pa_ce_max_giveback": 999,
}


def run(label, days_n=None, all_avail=False, **over):
    as_of = date(2026, 11, 3)
    days = _load_month_days(
        as_of=as_of, calendar_days=days_n or 200, all_available=all_avail
    )
    cfg = month_cfg()
    cfg.update(aggressive_overrides(100_000, 1.0))
    cfg.update(BASE_OFF)
    cfg.update(over)
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e5, balance=1e5, deploy_pct=1.0, deploy_cap=0.0)
    traded = wins = losses = pa = 0
    pan = 0.0
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
        pa += int(row.get("pa_entries") or 0)
        for t in row.get("trades") or []:
            if t.get("daily_pa"):
                pan += float(t.get("net") or 0)
    sess = sum(1 for d in days if len(_load_bars(d["minutes_path"])) >= 40)
    print(
        label,
        {
            "cov": round(100 * traded / max(1, sess), 1),
            "traded": f"{traded}/{sess}",
            "wl": f"{wins}/{losses}",
            "net": round(st.balance - 1e5),
            "pa": pa,
            "pa_net": round(pan),
        },
    )


def main() -> int:
    profiles = [
        ("baseline_no_new_gates", {}),
        ("pb_giveback_band", {"daily_pa_pb_min_giveback": 18, "daily_pa_pb_max_giveback": 55}),
        ("pe_after_75", {"daily_pa_pe_entry_after_minutes": 75}),
        ("pe_vs35", {"daily_pa_pe_min_vs_open": 35}),
        ("eff25", {"daily_pa_min_path_efficiency": 0.25}),
        ("or90", {"daily_pa_max_orb_width_for_pb": 90}),
        ("ce_give55", {"daily_pa_ce_max_giveback": 55}),
        (
            "best_combo",
            {
                "daily_pa_pb_min_giveback": 18,
                "daily_pa_pb_max_giveback": 55,
                "daily_pa_pe_entry_after_minutes": 75,
                "daily_pa_ce_max_giveback": 55,
                "daily_pa_min_path_efficiency": 0.20,
                "daily_pa_max_orb_width_for_pb": 100,
                "daily_pa_pe_min_vs_open": 30,
            },
        ),
        (
            "lean_combo",
            {
                "daily_pa_pb_min_giveback": 15,
                "daily_pa_pb_max_giveback": 60,
                "daily_pa_pe_entry_after_minutes": 70,
                "daily_pa_ce_max_giveback": 60,
                "daily_pa_pe_min_vs_open": 30,
            },
        ),
        (
            "knife_only",
            {
                "daily_pa_pb_min_giveback": 18,
                "daily_pa_pb_max_giveback": 60,
                "daily_pa_pe_entry_after_minutes": 75,
            },
        ),
    ]
    for name, over in profiles:
        print("---", name)
        run("30d", days_n=30, **over)
        run("full", all_avail=True, **over)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

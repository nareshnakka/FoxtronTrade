from __future__ import annotations

from datetime import date
from pathlib import Path

from backtest_detailed import _day_prem_opens, _load_bars, _seed_decay_days_before, simulate_day_detailed
from backtest_month_ledger import LedgerState, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from tools_backtest_week import aggressive_overrides


def main() -> None:
    cfg = month_cfg()
    cfg.update(aggressive_overrides(1_000_000, 1.0))
    days = _load_month_days(as_of=date(2026, 10, 22), calendar_days=20, all_available=False)
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e6, balance=1e6, deploy_pct=1.0, deploy_cap=0.0)
    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        if d["date"] != "2026-10-22":
            row = simulate_day_detailed(
                bars, cfg, st, day=d["date"], is_expiry_day=bool(d.get("is_expiry_day")),
                decay_mem=dm, ce_open=ce0, pe_open=pe0, verbose=False,
            )
            dm.finalize_day(d["date"])
            if int(row.get("setups") or 0):
                print("prior", d["date"], round(float(row.get("net") or 0)))
            continue

        spot0 = float(bars[0]["close"])
        day_open = float(bars[0].get("open") or bars[0]["close"])
        delta, theta = 0.5, 0.015
        for i, bar in enumerate(bars[:45]):
            closes = [float(b["close"]) for b in bars[: i + 1]]
            ds = closes[-1] - spot0
            ce_px = max(5.0, float(ce0) + delta * ds - theta * float(i))
            pe_px = max(5.0, float(pe0) - delta * ds - theta * float(i))
            dm.update_live(
                ce=ce_px, pe=pe_px, spot=closes[-1], day=d["date"], source="backtest",
                spot_open=day_open, spot_high=float(bar["high"]), spot_low=float(bar["low"]),
            )
            sig = dm.analyze(cfg)
            vs = closes[-1] - day_open
            if i < 35 and (
                sig.action != "NONE" or i in (0, 1, 5, 10, 14, 20, 25, 30)
            ):
                print(
                    f"  {str(bar['ts'])[11:16]} act={sig.action:10s} "
                    f"est={sig.expected_profit_pct:.0f} vs={vs:+.1f} :: {sig.reason[:110]}"
                )
        break


if __name__ == "__main__":
    main()

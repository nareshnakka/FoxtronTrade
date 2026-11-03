from datetime import date

from backtest_detailed import _load_bars
from backtest_month_ledger import _load_month_days

days = _load_month_days(as_of=date(2026, 11, 3), calendar_days=40, all_available=False)
for d in days:
    if d["date"] not in ("2026-10-26", "2026-10-27", "2026-10-06", "2026-07-01"):
        continue
    c = [float(b["close"]) for b in _load_bars(d["minutes_path"])]
    print(d["date"], "drive45", round(c[44] - c[0], 1), "day_hi_vs", round(max(c) - c[0], 1))
    for i in (55, 70, 100, 117, 146, 160):
        if i < len(c):
            print(
                "  i",
                i,
                "vs",
                round(c[i] - c[0], 1),
                "from_hi",
                round(max(c[: i + 1]) - c[i], 1),
            )

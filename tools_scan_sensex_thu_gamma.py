"""Scan historical Thursdays for Sensex gamma-blast arm rate + optional BT."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from backtest_detailed import _load_bars
from foxtrade.sensex_calendar import is_sensex_gamma_conditional_day, sensex_day_role
from foxtrade.sensex_gamma import detect_sensex_gamma_blast
from tools_train_sensex_fasttrack import load_sensex_days, run_window

OVERRIDES = Path("data/sensex/fasttrack_sensex_overrides.json")


def main() -> int:
    cfg = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    cfg.setdefault("sensex_thursday_gamma_enabled", True)
    cfg.setdefault("sensex_thursday_gamma_only", True)
    cfg.setdefault("sensex_conditional_weekdays", [3])
    cfg.setdefault("underlying", "SENSEX")

    days = load_sensex_days(date(2026, 11, 3), trade_days_only=True, cfg=cfg)
    thus = [d for d in days if is_sensex_gamma_conditional_day(d["date"], cfg)]
    armed = 0
    samples = []
    for d in thus:
        bars = _load_bars(d["minutes_path"])
        closes: list[float] = []
        hit = None
        for b in bars:
            closes.append(float(b["close"]))
            blast = detect_sensex_gamma_blast(closes, cfg=cfg)
            if blast.get("allow"):
                hit = (b.get("time") or b.get("t") or "?", blast)
                break
        if hit:
            armed += 1
            samples.append((d["date"], hit[0], hit[1].get("side"), hit[1].get("reason")))

    print(
        f"Thursdays {len(thus)} · gamma armed {armed} "
        f"({100 * armed / max(1, len(thus)):.0f}%)"
    )
    for s in samples[-8:]:
        print(f"  {s[0]} @{s[1]} {s[2]} · {s[3]}")

    # Backtest Thu-only sleeve
    if thus:
        full = run_window(thus, cfg, 100_000, 1.0)
        wr = full["wins"] / max(1, full["wins"] + full["losses"])
        print(
            f"Thu-only BT net={full['net']:+,.0f} W/L {full['wins']}/{full['losses']} "
            f"WR={100 * wr:.0f}% cov {full['traded']}/{full['sessions']} "
            f"dd={full['max_dd']:,.0f}"
        )
        for r in full["daily"]:
            if int(r.get("setups") or 0) or float(r.get("net") or 0):
                print(
                    f"  {r['date']} net={float(r.get('net') or 0):+,.0f} "
                    f"setups={r.get('setups')} "
                    f"{sensex_day_role(r['date'], cfg)}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

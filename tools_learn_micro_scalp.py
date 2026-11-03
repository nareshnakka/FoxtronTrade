"""Learn timed 10–15pt spot micro-scalps on days primary+soft stay flat."""
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
from foxtrade.expert_filters import manage_percent_exit
from tools_backtest_week import aggressive_overrides

OUT = Path("data/backtests/micro_scalp_learn.json")


def _synth(closes, side, ei, ep, i, delta=0.5, theta=0.015):
    ds = closes[i] - closes[ei]
    signed = ds if side == "CE" else -ds
    return max(5.0, ep + delta * signed - theta * (i - ei))


def _hhmm(i: int) -> str:
    t = 9 * 60 + 15 + i
    return f"{t // 60:02d}:{t % 60:02d}"


def run_exit(closes, side, ei, ep, lots, spot_tgt, t_pct, sl_pct, max_hold=15):
    peak = ep
    for j in range(ei + 1, min(len(closes), ei + max_hold + 1)):
        px = _synth(closes, side, ei, ep, j)
        peak = max(peak, px)
        fav = (closes[j] - closes[ei]) if side == "CE" else (closes[ei] - closes[j])
        if fav >= spot_tgt and px >= ep:
            return {
                "net": round((px - ep) * 75 * lots - 280),
                "held": j - ei,
                "spot": round(fav, 1),
                "reason": f"spot+{spot_tgt}",
            }
        dec = manage_percent_exit(
            entry=ep,
            px=px,
            peak=peak,
            held_minutes=float(j - ei),
            cfg={
                "use_percent_exits": True,
                "stop_loss_pct": sl_pct,
                "target_pct_low": t_pct,
                "target_pct_high": 8,
                "lock_profit_pct": 0.4,
                "trail_giveback_pct": 1.2,
                "time_stop_minutes": max_hold,
                "exit_at_first_target": True,
                "stop_grace_minutes": 1,
                "stop_grace_pct": 3.0,
            },
        )
        if dec.get("action") == "exit":
            return {
                "net": round((px - ep) * 75 * lots - 280),
                "held": j - ei,
                "spot": round(fav, 1),
                "reason": str(dec.get("reason")),
            }
    j = min(len(closes) - 1, ei + max_hold)
    px = _synth(closes, side, ei, ep, j)
    fav = (closes[j] - closes[ei]) if side == "CE" else (closes[ei] - closes[j])
    return {
        "net": round((px - ep) * 75 * lots - 280),
        "held": j - ei,
        "spot": round(fav, 1),
        "reason": "time",
    }


def pb_resume(
    bars,
    *,
    drive_n=45,
    min_drive=40,
    after=55,
    until=180,
    pb_lo=0.30,
    pb_hi=0.70,
    spot_tgt=12,
    lots=4,
    t_pct=4.0,
    sl_pct=2.2,
    both=True,
    confirm=1,
):
    closes = [float(b["close"]) for b in bars]
    if len(closes) < drive_n + 10:
        return None
    o = closes[0]
    drive = closes[drive_n - 1] - o
    if abs(drive) < min_drive:
        return None
    side = "CE" if drive > 0 else "PE"
    if not both and side != "CE":
        return None
    extreme = max(closes[:drive_n]) if side == "CE" else min(closes[:drive_n])
    streak = 0
    for i in range(after, min(until, len(closes) - 5)):
        if side == "CE":
            pb = extreme - closes[i]
            if not (pb_lo * abs(drive) <= pb <= pb_hi * abs(drive)):
                streak = 0
                continue
            ok = closes[i] > closes[i - 1] and (
                confirm < 2 or closes[i - 1] >= closes[i - 2] - 2
            )
        else:
            pb = closes[i] - extreme
            if not (pb_lo * abs(drive) <= pb <= pb_hi * abs(drive)):
                streak = 0
                continue
            ok = closes[i] < closes[i - 1] and (
                confirm < 2 or closes[i - 1] <= closes[i - 2] + 2
            )
        if not ok:
            streak = 0
            continue
        streak += 1
        if streak < confirm:
            continue
        out = run_exit(closes, side, i, 135.0, lots, spot_tgt, t_pct, sl_pct)
        return {
            "side": side,
            "t": _hhmm(i),
            "drive": round(drive, 1),
            **out,
        }
    return None


def main() -> int:
    as_of = date(2026, 11, 3)
    days = _load_month_days(as_of=as_of, calendar_days=200, all_available=True)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(100_000, 1.0))
    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e5, balance=1e5, deploy_pct=1.0, deploy_cap=0.0)
    flat = []
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
        if int(row.get("setups") or 0) == 0 and len(bars) >= 80:
            flat.append(d)
    print(f"flat after baseline {len(flat)}/{len(days)}")

    grid = []
    for min_drive in (35, 45, 55, 70):
        for spot in (10, 12, 15):
            for lots in (4, 6):
                for both in (True, False):
                    for confirm in (1, 2):
                        for until in (150, 180, 210):
                            tag = (
                                f"dr{min_drive}_s{spot}_L{lots}_"
                                f"{'both' if both else 'CE'}_c{confirm}_u{until}"
                            )
                            hits = []
                            for d in flat:
                                r = pb_resume(
                                    _load_bars(d["minutes_path"]),
                                    min_drive=min_drive,
                                    spot_tgt=spot,
                                    lots=lots,
                                    both=both,
                                    confirm=confirm,
                                    until=until,
                                )
                                if r:
                                    hits.append(r)
                            if len(hits) < 5:
                                continue
                            nets = [h["net"] for h in hits]
                            w = sum(1 for n in nets if n > 0)
                            l = sum(1 for n in nets if n < 0)
                            grid.append(
                                {
                                    "tag": tag,
                                    "n": len(hits),
                                    "wins": w,
                                    "losses": l,
                                    "net": round(sum(nets)),
                                    "avg": round(sum(nets) / len(nets)),
                                    "wr": round(100.0 * w / len(hits), 1),
                                    "avg_spot": round(
                                        sum(h["spot"] for h in hits) / len(hits), 1
                                    ),
                                    "sample": hits[:5],
                                }
                            )

    grid.sort(key=lambda x: (x["net"], x["wr"]), reverse=True)
    print("TOP 12 by net:")
    for g in grid[:12]:
        print(
            f"  {g['tag']}: n={g['n']} W/L {g['wins']}/{g['losses']} "
            f"WR {g['wr']}% net {g['net']:+.0f} avg {g['avg']:+.0f} "
            f"spot {g['avg_spot']}"
        )
    best_wr = sorted(
        [g for g in grid if g["net"] >= 2000],
        key=lambda x: (x["wr"], x["net"]),
        reverse=True,
    )[:8]
    print("Best WR among net>=2k:")
    for g in best_wr:
        print(
            f"  {g['tag']}: n={g['n']} WR {g['wr']}% net {g['net']:+.0f}"
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {"flat": len(flat), "top": grid[:20], "best_wr": best_wr},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"JSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

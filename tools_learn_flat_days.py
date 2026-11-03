"""
Learn from non-traded days: which soft / micro entries would have cashed
small moves without wrecking expectancy.

Flat days usually have large ranges but are classified as CHOP by the primary
path — so recipes must either disable block_structure_in_chop or use a custom
price-structure micro path that does not call analyze_market's chop gate.
"""
from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
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
from foxtrade.market_direction import analyze_market
from tools_backtest_week import aggressive_overrides

OUT = Path("data/backtests/flat_day_learn.json")


def _hhmm(i: int) -> str:
    tot = 9 * 60 + 15 + i
    return f"{tot // 60:02d}:{tot % 60:02d}"


def _synth(closes, side, entry_i, entry_px, i, delta=0.5, theta=0.015):
    ds = closes[i] - closes[entry_i]
    signed = ds if side == "CE" else -ds
    return max(5.0, entry_px + delta * signed - theta * (i - entry_i))


def _manage_open(open_pos, closes, i, scfg, day, soft):
    px = _synth(closes, open_pos["side"], open_pos["i"], open_pos["px"], i)
    open_pos["peak"] = max(open_pos["peak"], px)
    held = float(i - open_pos["i"])
    dec = manage_percent_exit(
        entry=open_pos["px"],
        px=px,
        peak=open_pos["peak"],
        held_minutes=held,
        cfg=scfg,
    )
    if str(dec.get("action") or "") != "exit":
        return None
    net = (px - open_pos["px"]) * 75 * open_pos["lots"]
    net -= 350  # rough charges
    return {
        "day": day,
        "side": open_pos["side"],
        "entry_t": open_pos["t"],
        "exit_t": _hhmm(i),
        "event": open_pos["event"],
        "net": round(net, 0),
        "held": int(held),
        "reason": dec.get("reason"),
        "recipe": soft.get("recipe"),
    }


def micro_sim_analyze(
    bars: list[dict],
    cfg: dict,
    *,
    day: str,
    soft: dict,
) -> dict | None:
    """Soft entry via analyze_market with chopped gates loosened."""
    if len(bars) < 40:
        return None
    closes = [float(b["close"]) for b in bars]
    scfg = {**cfg, **soft}
    entry_prem = (float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))) / 2
    persist_need = int(soft.get("signal_persist_minutes", 1))
    persist_side = None
    persist_n = 0
    open_pos = None
    max_i = min(len(closes) - 1, int(soft.get("no_entry_after_minutes", cfg.get("no_entry_after_minutes", 300))))

    for i in range(20, max_i + 1):
        if open_pos is not None:
            done = _manage_open(open_pos, closes, i, scfg, day, soft)
            if done:
                return done
            continue

        a = analyze_market(closes[: i + 1], cfg=scfg, is_expiry_day=False)
        allow = bool(a.get("allow_entry")) and a.get("side") in ("CE", "PE")
        side = a.get("side")
        event = str(a.get("event") or "")
        if not allow:
            persist_side, persist_n = None, 0
            continue
        if persist_need > 0:
            if side == persist_side:
                persist_n += 1
            else:
                persist_side, persist_n = side, 1
            if persist_n < persist_need:
                continue
        open_pos = {
            "side": side,
            "i": i,
            "px": entry_prem,
            "peak": entry_prem,
            "lots": int(soft.get("micro_lots", 8)),
            "t": _hhmm(i),
            "event": event,
        }
    return None


def _signal_orb15_hold(closes: list[float], i: int, soft: dict) -> tuple[str, str] | None:
    """15m OR break + N-bar hold beyond level."""
    orb_n = int(soft.get("orb_n", 15))
    hold_n = int(soft.get("hold_n", 2))
    buf = float(soft.get("break_buf", 8))
    if i < orb_n + hold_n + 1:
        return None
    or_hi = max(closes[:orb_n])
    or_lo = min(closes[:orb_n])
    recent = closes[i - hold_n + 1 : i + 1]
    spot = closes[i]
    vs = spot - closes[0]
    min_vs = float(soft.get("min_vs_open", 25))
    if all(x >= or_hi + buf for x in recent) and vs >= min_vs:
        return "CE", "ORB15_HOLD"
    if all(x <= or_lo - buf for x in recent) and vs <= -min_vs:
        return "PE", "ORB15_HOLD"
    return None


def _signal_pullback_resume(closes: list[float], i: int, soft: dict) -> tuple[str, str] | None:
    """Morning drive direction → pullback → resume."""
    drive_n = int(soft.get("drive_n", 45))
    if i < drive_n + 8:
        return None
    o = closes[0]
    drive = closes[drive_n - 1] - o
    min_drive = float(soft.get("min_drive", 40))
    if abs(drive) < min_drive:
        return None
    side = "CE" if drive > 0 else "PE"
    # pullback depth vs drive extreme
    window = closes[drive_n:i]
    if len(window) < 5:
        return None
    if side == "CE":
        peak = max(closes[: i + 1])
        pb = peak - closes[i]
        # need some pullback then resume up last 2 bars
        if pb < abs(drive) * float(soft.get("pb_frac_lo", 0.35)):
            return None
        if pb > abs(drive) * float(soft.get("pb_frac_hi", 0.75)):
            return None
        if closes[i] > closes[i - 1] >= closes[i - 2] - 2:
            return "CE", "PB_RESUME"
    else:
        trough = min(closes[: i + 1])
        pb = closes[i] - trough
        if pb < abs(drive) * float(soft.get("pb_frac_lo", 0.35)):
            return None
        if pb > abs(drive) * float(soft.get("pb_frac_hi", 0.75)):
            return None
        if closes[i] < closes[i - 1] <= closes[i - 2] + 2:
            return "PE", "PB_RESUME"
    return None


def _signal_vwap_reclaim(closes: list[float], i: int, soft: dict) -> tuple[str, str] | None:
    if i < 40:
        return None
    # proxy VWAP ≈ cumulative mean of closes
    vwap = sum(closes[: i + 1]) / (i + 1)
    spot = closes[i]
    look = int(soft.get("vwap_look", 6))
    below = sum(1 for x in closes[i - look : i] if x < vwap)
    above = sum(1 for x in closes[i - look : i] if x > vwap)
    vs = spot - closes[0]
    min_vs = float(soft.get("min_vs_open", 20))
    if below >= look - 1 and spot > vwap + float(soft.get("vwap_buf", 3)) and vs >= min_vs:
        if closes[i] > closes[i - 1]:
            return "CE", "VWAP_RECLAIM"
    if above >= look - 1 and spot < vwap - float(soft.get("vwap_buf", 3)) and vs <= -min_vs:
        if closes[i] < closes[i - 1]:
            return "PE", "VWAP_REJECT"
    return None


def _signal_midday_box(closes: list[float], i: int, soft: dict) -> tuple[str, str] | None:
    """Break of 10:30–12:00 box after 12:05."""
    box_a = int(soft.get("box_start", 75))   # ~10:30
    box_b = int(soft.get("box_end", 165))    # ~12:00
    start_i = int(soft.get("trade_after", 170))
    if i < start_i or len(closes) <= box_b:
        return None
    box = closes[box_a:box_b]
    if len(box) < 20:
        return None
    hi, lo = max(box), min(box)
    if hi - lo > float(soft.get("max_box_w", 120)):
        return None  # too wide = trend already happened
    if hi - lo < float(soft.get("min_box_w", 25)):
        return None
    hold_n = int(soft.get("hold_n", 2))
    buf = float(soft.get("break_buf", 6))
    recent = closes[i - hold_n + 1 : i + 1]
    vs = closes[i] - closes[0]
    min_vs = float(soft.get("min_vs_open", 15))
    if all(x >= hi + buf for x in recent) and vs >= min_vs:
        return "CE", "MID_BOX_BRK"
    if all(x <= lo - buf for x in recent) and vs <= -min_vs:
        return "PE", "MID_BOX_BRK"
    return None


def micro_sim_struct(
    bars: list[dict],
    cfg: dict,
    *,
    day: str,
    soft: dict,
) -> dict | None:
    """Custom structure signals — ignore primary chop gate."""
    if len(bars) < 50:
        return None
    closes = [float(b["close"]) for b in bars]
    scfg = {**cfg, **soft}
    entry_prem = (float(cfg.get("premium_low", 125)) + float(cfg.get("premium_high", 145))) / 2
    open_pos = None
    max_i = min(len(closes) - 1, int(soft.get("no_entry_after_minutes", 280)))
    start_i = int(soft.get("entry_from", 25))
    kind = soft.get("struct", "orb15")

    for i in range(start_i, max_i + 1):
        if open_pos is not None:
            done = _manage_open(open_pos, closes, i, scfg, day, soft)
            if done:
                return done
            continue

        hit = None
        if kind == "orb15":
            hit = _signal_orb15_hold(closes, i, soft)
        elif kind == "pullback":
            hit = _signal_pullback_resume(closes, i, soft)
        elif kind == "vwap":
            hit = _signal_vwap_reclaim(closes, i, soft)
        elif kind == "midbox":
            hit = _signal_midday_box(closes, i, soft)
        if not hit:
            continue
        side, event = hit
        open_pos = {
            "side": side,
            "i": i,
            "px": entry_prem,
            "peak": entry_prem,
            "lots": int(soft.get("micro_lots", 6)),
            "t": _hhmm(i),
            "event": event,
        }
    return None


def _exit_scalp(**kw) -> dict:
    return {
        "stop_loss_pct": kw.get("sl", 2.5),
        "target_pct_low": kw.get("tlo", 3.5),
        "target_pct_high": kw.get("thi", 10.0),
        "lock_profit_pct": 0.8,
        "trail_giveback_pct": 2.0,
        "time_stop_minutes": kw.get("ts", 12),
        "exit_at_first_target": True,
        "stop_grace_minutes": kw.get("grace", 2),
        "stop_grace_pct": kw.get("grace_pct", 4.0),
        "micro_lots": kw.get("lots", 6),
        "use_percent_exits": True,
    }


def recipes() -> list[dict]:
    """Candidate soft / structural profiles to cash small daily moves."""
    return [
        # --- analyze_market with chop unlocked ---
        {
            "recipe": "chop_unlock_sr",
            "mode": "analyze",
            **_exit_scalp(sl=3.0, tlo=4.0, thi=12.0, ts=14, lots=8),
            "block_structure_in_chop": False,
            "ce_min_vs_open": 40,
            "pe_min_vs_open": 40,
            "prefer_sr_dip_entry": True,
            "enable_sr_reclaim": True,
            "enable_sr_reject": True,
            "enable_orb_breakout": True,
            "enable_orb_breakdown": True,
            "block_orb_chase_without_retest": True,
            "breakout_max_extension_pts": 45,
            "min_path_efficiency": 0.12,
            "require_session_extreme": False,
            "directional_min_confidence": 0.60,
            "directional_min_margin": 0.10,
            "signal_persist_minutes": 1,
        },
        {
            "recipe": "chop_unlock_tight",
            "mode": "analyze",
            **_exit_scalp(sl=2.5, tlo=3.5, thi=8.0, ts=10, lots=6, grace=3, grace_pct=5.0),
            "block_structure_in_chop": False,
            "ce_min_vs_open": 35,
            "pe_min_vs_open": 35,
            "prefer_sr_dip_entry": True,
            "enable_sr_reclaim": True,
            "enable_sr_reject": True,
            "enable_orb_breakout": True,
            "enable_orb_breakdown": True,
            "block_orb_chase_without_retest": True,
            "block_ce_mid_chase": False,
            "breakout_max_extension_pts": 50,
            "min_path_efficiency": 0.10,
            "require_session_extreme": False,
            "require_3bar_continue": True,
            "directional_min_confidence": 0.58,
            "directional_min_margin": 0.08,
            "signal_persist_minutes": 1,
        },
        # --- structural micros (bypass analyze_market) ---
        {
            "recipe": "struct_orb15_hold",
            "mode": "struct",
            "struct": "orb15",
            **_exit_scalp(sl=2.5, tlo=4.0, thi=10.0, ts=12, lots=6),
            "orb_n": 15,
            "hold_n": 2,
            "break_buf": 8,
            "min_vs_open": 30,
            "entry_from": 20,
            "no_entry_after_minutes": 240,
        },
        {
            "recipe": "struct_orb15_strict",
            "mode": "struct",
            "struct": "orb15",
            **_exit_scalp(sl=2.2, tlo=3.5, thi=8.0, ts=10, lots=5, grace=2),
            "orb_n": 15,
            "hold_n": 3,
            "break_buf": 12,
            "min_vs_open": 45,
            "entry_from": 25,
            "no_entry_after_minutes": 200,
        },
        {
            "recipe": "struct_pullback_resume",
            "mode": "struct",
            "struct": "pullback",
            **_exit_scalp(sl=2.5, tlo=4.0, thi=10.0, ts=14, lots=6),
            "drive_n": 45,
            "min_drive": 45,
            "pb_frac_lo": 0.30,
            "pb_frac_hi": 0.70,
            "entry_from": 55,
            "no_entry_after_minutes": 220,
        },
        {
            "recipe": "struct_vwap_reclaim",
            "mode": "struct",
            "struct": "vwap",
            **_exit_scalp(sl=2.5, tlo=3.5, thi=9.0, ts=12, lots=6),
            "vwap_look": 6,
            "vwap_buf": 4,
            "min_vs_open": 25,
            "entry_from": 40,
            "no_entry_after_minutes": 250,
        },
        {
            "recipe": "struct_midbox_break",
            "mode": "struct",
            "struct": "midbox",
            **_exit_scalp(sl=2.5, tlo=4.0, thi=10.0, ts=12, lots=6),
            "box_start": 75,
            "box_end": 165,
            "trade_after": 170,
            "min_box_w": 30,
            "max_box_w": 110,
            "hold_n": 2,
            "break_buf": 6,
            "min_vs_open": 15,
            "entry_from": 170,
            "no_entry_after_minutes": 280,
        },
    ]


def main() -> int:
    as_of = date(2026, 11, 3)
    days = _load_month_days(as_of=as_of, calendar_days=200, all_available=True)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(1_000_000, 1.0))

    seed = _seed_decay_days_before(days[0]["date"])
    dm = DecayMemory(persist=False, seed_days=seed)
    st = LedgerState(initial=1e6, balance=1e6, deploy_pct=1.0, deploy_cap=0.0)
    flat_days = []
    traded = 0
    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        row = simulate_day_detailed(
            bars, cfg, st, day=d["date"], is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=dm, ce_open=ce0, pe_open=pe0, verbose=False,
        )
        dm.finalize_day(d["date"])
        if int(row.get("setups") or 0) > 0:
            traded += 1
        elif len(bars) >= 40:
            flat_days.append(d)

    print(f"Primary traded {traded}/{len(days)}; flat candidates {len(flat_days)}")

    summary = {}
    details = defaultdict(list)
    for soft in recipes():
        name = soft["recipe"]
        hits = []
        for d in flat_days:
            bars = _load_bars(d["minutes_path"])
            if soft.get("mode") == "struct":
                r = micro_sim_struct(bars, cfg, day=d["date"], soft=soft)
            else:
                r = micro_sim_analyze(bars, cfg, day=d["date"], soft=soft)
            if r:
                hits.append(r)
                details[name].append(r)
        nets = [h["net"] for h in hits]
        wins = sum(1 for n in nets if n > 0)
        losses = sum(1 for n in nets if n < 0)
        summary[name] = {
            "entries": len(hits),
            "wins": wins,
            "losses": losses,
            "flat": len(hits) - wins - losses,
            "net": round(sum(nets), 0),
            "avg": round(sum(nets) / len(nets), 0) if nets else 0,
            "coverage_pct": round(100.0 * len(hits) / max(1, len(flat_days)), 1),
            "win_rate": round(100.0 * wins / max(1, len(hits)), 1) if hits else 0,
        }
        print(
            f"{name}: entries {len(hits)}/{len(flat_days)} "
            f"W/L {wins}/{losses} WR {summary[name]['win_rate']:.0f}% "
            f"net {summary[name]['net']:+.0f} avg {summary[name]['avg']:+.0f}"
        )

    # Prefer positive expectancy; among those, max net; else least-negative with coverage
    profitable = [(k, v) for k, v in summary.items() if v["net"] > 0 and v["entries"] >= 5]
    if profitable:
        best = max(profitable, key=lambda kv: (kv[1]["net"], kv[1]["win_rate"]))
    else:
        best = max(summary.items(), key=lambda kv: (kv[1]["net"], kv[1]["entries"])) if summary else None
    print("\nBest recipe:", best[0] if best else None, best[1] if best else None)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "flat_days": len(flat_days),
                "primary_traded": traded,
                "summary": summary,
                "best": best[0] if best else None,
                "sample_trades": {k: v[:10] for k, v in details.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"JSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

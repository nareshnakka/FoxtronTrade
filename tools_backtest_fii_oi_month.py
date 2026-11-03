"""
Replay FII/OI scanner on last ~1 month of Nifty futures OI minutes.
Reports daily regimes, actions, and how algo trades would have been gated.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_detailed import _day_prem_opens, _load_bars, _seed_decay_days_before, simulate_day_detailed
from backtest_month_ledger import OUT, LedgerState, _inr, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from foxtrade.fii_oi_scanner import _actions_for, classify_oi_price
from tools_backtest_week import aggressive_overrides

IST = ZoneInfo("Asia/Kolkata")
OI_DIR = Path("data/expiries/last_3_months/oi_minutes")
REPORT = OUT / "fii_oi_month_replay.json"


def _load_oi_day(day: str) -> list[dict]:
    p = OI_DIR / f"nifty_fut_{day}.json"
    if not p.exists():
        return []
    rows = json.loads(p.read_text(encoding="utf-8"))
    return rows if isinstance(rows, list) else []


def _hhmm(ts: str) -> str:
    if "T" in ts:
        return ts[11:16]
    return str(ts)[:5]


def _minutes_of(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _sustained_short_buildup(
    minute_reg: dict[str, dict], entry_hhmm: str, need_min: int
) -> bool:
    """True if short_buildup held continuously for need_min ending at entry."""
    et = _minutes_of(entry_hhmm)
    if et < 0:
        return False
    held = 0
    for mins in range(et, et - need_min - 1, -1):
        hh = f"{mins // 60:02d}:{mins % 60:02d}"
        reg = (minute_reg.get(hh) or {}).get("regime")
        if reg == "short_buildup":
            held += 1
        else:
            break
    return held >= need_min


def replay_day_oi(day: str, rows: list[dict], cfg: dict) -> dict | None:
    if len(rows) < 30:
        return None
    open_px = float(rows[0].get("open") or rows[0].get("close") or 0)
    open_oi = int(rows[0].get("oi") or 0)
    if open_px <= 0 or open_oi <= 0:
        return None

    price_eps = float(cfg.get("fii_oi_price_eps", 20.0))
    oi_eps = float(cfg.get("fii_oi_chg_eps", 5000))

    # Sample at key clocks + EOD
    checkpoints = ("10:00", "11:15", "12:30", "14:00", "15:15")
    samples = []
    last_reg = None
    for r in rows:
        t = _hhmm(str(r.get("ts") or ""))
        px = float(r.get("close") or 0)
        oi = int(r.get("oi") or 0)
        if t not in checkpoints and t != _hhmm(str(rows[-1].get("ts") or "")):
            continue
        # dedupe same clock
        if samples and samples[-1]["t"] == t:
            samples[-1] = None  # replace
            samples.pop()
        dpx = px - open_px
        doi = oi - open_oi
        reg = classify_oi_price(
            price_chg=dpx, oi_chg=doi, price_eps=price_eps, oi_eps=oi_eps
        )
        acts = _actions_for(reg, fii_net=None, fii_net_chg=None, cfg=cfg)
        samples.append(
            {
                "t": t,
                "px": round(px, 1),
                "oi": oi,
                "dpx": round(dpx, 1),
                "doi": doi,
                "regime": reg,
                "block_ce": acts["block_ce"],
                "prefer_pe": acts["prefer_pe"],
                "prefer_ce": acts["prefer_ce"],
                "caution_ce": acts["caution_ce"],
                "side_hint": acts["side_hint"],
            }
        )
        last_reg = reg

    eod = rows[-1]
    eod_px = float(eod.get("close") or 0)
    eod_oi = int(eod.get("oi") or 0)
    eod_reg = classify_oi_price(
        price_chg=eod_px - open_px,
        oi_chg=eod_oi - open_oi,
        price_eps=price_eps,
        oi_eps=oi_eps,
    )
    eod_acts = _actions_for(eod_reg, fii_net=None, fii_net_chg=None, cfg=cfg)

    # Dominant intraday regime (most time in last half of day samples)
    reg_counts = Counter(s["regime"] for s in samples if s["regime"] != "chop")
    dominant = reg_counts.most_common(1)[0][0] if reg_counts else eod_reg

    return {
        "date": day,
        "open_px": round(open_px, 1),
        "close_px": round(eod_px, 1),
        "open_oi": open_oi,
        "close_oi": eod_oi,
        "dpx": round(eod_px - open_px, 1),
        "doi": eod_oi - open_oi,
        "eod_regime": eod_reg,
        "dominant_regime": dominant,
        "eod_actions": eod_acts,
        "samples": samples,
    }


def simulate_with_oi_gate(
    days: list[dict],
    oi_by_day: dict[str, dict],
    *,
    capital: float,
    deploy: float,
) -> dict:
    """Causal month backtest; block CE when OI regime at entry minute says so."""
    cfg = month_cfg()
    cfg.update(aggressive_overrides(capital, deploy))
    state = LedgerState(
        initial=capital, balance=capital, deploy_pct=deploy, deploy_cap=0.0
    )
    seed = _seed_decay_days_before(days[0]["date"])
    decay_mem = DecayMemory(persist=False, seed_days=seed)

    daily = []
    blocked = []
    allowed_align = []

    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        day = d["date"]
        oi_meta = oi_by_day.get(day)

        # Inject treat flags from EOD dominant for manage; live gate via macro monkeypatch
        # We wrap simulate by patching cfg with a callable check — simpler: post-filter
        # Actually intercept: run simulate, then also walk to see if CE entries conflict.

        # Precompute minute→regime from oi file
        oi_rows = _load_oi_day(day)
        minute_reg: dict[str, dict] = {}
        if oi_rows:
            opx = float(oi_rows[0].get("open") or oi_rows[0].get("close") or 0)
            ooi = int(oi_rows[0].get("oi") or 0)
            for r in oi_rows:
                t = _hhmm(str(r.get("ts") or ""))
                px = float(r.get("close") or 0)
                oi = int(r.get("oi") or 0)
                reg = classify_oi_price(
                    price_chg=px - opx,
                    oi_chg=oi - ooi,
                    price_eps=float(cfg.get("fii_oi_price_eps", 20)),
                    oi_eps=float(cfg.get("fii_oi_chg_eps", 5000)),
                )
                acts = _actions_for(reg, fii_net=None, fii_net_chg=None, cfg=cfg)
                minute_reg[t] = {
                    "regime": reg,
                    "dpx": round(px - opx, 1),
                    "doi": oi - ooi,
                    **acts,
                }

        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=day,
            is_expiry_day=bool(d.get("is_expiry_day")),
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
            verbose=False,
        )
        decay_mem.finalize_day(day)

        # Annotate trades with OI regime at entry; estimate blocked PnL if CE into short_buildup
        gated_trades = []
        for t in row.get("trades") or []:
            et = str(t.get("entry_time") or "")
            info = minute_reg.get(et) or (oi_meta or {}).get("eod_actions") or {}
            reg = (minute_reg.get(et) or {}).get("regime") or (oi_meta or {}).get(
                "eod_regime"
            )
            side = t.get("side")
            is_post = "post_decay" in str(t.get("event") or "").lower()
            mr = minute_reg.get(et) or {}
            sustained_need = int(cfg.get("fii_oi_sustained_short_min", 45))
            price_eps = float(cfg.get("fii_oi_price_eps", 20.0))
            firmly_down = float(mr.get("dpx") or 0) <= -(2.0 * price_eps)
            sustained = (
                reg == "short_buildup"
                and _sustained_short_buildup(minute_reg, et, sustained_need)
                and firmly_down
            )
            # Match live: hard-block CE on long_unwinding or sustained+firmly-down
            # short_buildup; reclaiming price → haircut only; post_decay exempt
            would_block = bool(
                side == "CE"
                and not is_post
                and (reg == "long_unwinding" or sustained)
            )
            would_haircut = bool(
                side == "CE"
                and not is_post
                and reg == "short_buildup"
                and not would_block
            )
            gated_trades.append(
                {
                    "date": day,
                    "side": side,
                    "event": t.get("event"),
                    "entry_time": et,
                    "net": round(float(t.get("net") or 0), 0),
                    "oi_regime_at_entry": reg,
                    "would_block_ce": would_block,
                    "would_haircut": would_haircut,
                    "post_decay_exempt": is_post and reg == "short_buildup",
                }
            )
            if would_block:
                blocked.append(
                    {
                        "date": day,
                        "entry_time": et,
                        "net": float(t.get("net") or 0),
                        "regime": reg,
                        "event": t.get("event"),
                    }
                )
            elif side == "CE" and reg == "long_buildup":
                allowed_align.append({"date": day, "net": float(t.get("net") or 0)})

        row_out = {
            "date": day,
            "net": round(float(row.get("net") or 0), 0),
            "setups": row.get("setups"),
            "oi": {
                "eod_regime": (oi_meta or {}).get("eod_regime"),
                "dominant": (oi_meta or {}).get("dominant_regime"),
                "dpx": (oi_meta or {}).get("dpx"),
                "doi": (oi_meta or {}).get("doi"),
            }
            if oi_meta
            else None,
            "trades": gated_trades,
        }
        daily.append(row_out)

    # Counterfactual: remove blocked CE trade nets
    raw_net = sum(float(r["net"]) for r in daily)
    blocked_net = sum(b["net"] for b in blocked)
    # If we blocked a loser, we gain; if blocked a winner, we lose that profit
    adj_net = raw_net - blocked_net

    return {
        "raw_net": round(raw_net, 0),
        "blocked_trades": blocked,
        "blocked_count": len(blocked),
        "blocked_net_sum": round(blocked_net, 0),
        "net_if_oi_gates": round(adj_net, 0),
        "delta_vs_raw": round(adj_net - raw_net, 0),
        "aligned_ce_long_build_n": len(allowed_align),
        "aligned_ce_long_build_net": round(sum(a["net"] for a in allowed_align), 0),
        "daily": daily,
        "final_balance_raw": round(state.balance, 0),
    }


def main() -> int:
    as_of = date(2026, 11, 2)
    days = _load_month_days(as_of=as_of, calendar_days=31, all_available=False)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(1_000_000, 1.0))

    oi_reports = []
    oi_by_day: dict[str, dict] = {}
    missing_oi = []
    for d in days:
        rows = _load_oi_day(d["date"])
        if not rows:
            missing_oi.append(d["date"])
            continue
        rep = replay_day_oi(d["date"], rows, cfg)
        if rep:
            oi_reports.append(rep)
            oi_by_day[d["date"]] = rep

    regime_eod = Counter(r["eod_regime"] for r in oi_reports)
    regime_dom = Counter(r["dominant_regime"] for r in oi_reports)

    print(f"Sessions in window: {len(days)}")
    print(f"With fut OI minutes: {len(oi_reports)}  missing: {len(missing_oi)}")
    print("EOD regime counts:", dict(regime_eod))
    print("Dominant regime counts:", dict(regime_dom))
    print()
    print("Day-by-day OI matrix:")
    for r in oi_reports:
        a = r["eod_actions"]
        flag = ""
        if a.get("block_ce"):
            flag = " BLOCK_CE"
        elif a.get("prefer_ce"):
            flag = " prefer_CE"
        elif a.get("prefer_pe"):
            flag = " prefer_PE"
        elif a.get("caution_ce"):
            flag = " caution_CE"
        print(
            f"  {r['date']}  dpx={r['dpx']:+7.1f}  doi={r['doi']:+9d}  "
            f"{r['eod_regime']:16s}{flag}"
        )

    print("\nRunning causal algo @10L d100 with OI gate annotation...")
    sim = simulate_with_oi_gate(
        days, oi_by_day, capital=1_000_000, deploy=1.0
    )
    # Haircut / exempt stats from trade annotations
    haircuts = []
    exempts = []
    for drow in sim.get("daily") or []:
        for t in drow.get("trades") or []:
            if t.get("would_haircut"):
                haircuts.append(t)
            if t.get("post_decay_exempt"):
                exempts.append(t)

    print(
        f"Raw algo net: {_inr(sim['raw_net'])}  |  "
        f"With soft OI gates (block only unwind/fresh-short): {_inr(sim['net_if_oi_gates'])}  "
        f"(Δ {_inr(sim['delta_vs_raw'])})"
    )
    print(
        f"CE hard-blocked: {sim['blocked_count']}  "
        f"net sum {_inr(sim['blocked_net_sum'])}  |  "
        f"CE haircut (short_buildup): {len(haircuts)}  |  "
        f"post_decay exempt: {len(exempts)}"
    )
    if sim["blocked_trades"]:
        print("Blocked CE detail:")
        for b in sim["blocked_trades"]:
            print(
                f"  {b['date']} {b['entry_time']} {b['event']} "
                f"reg={b['regime']} net={_inr(b['net'])}"
            )
    if exempts:
        print("post_decay exempt (kept despite short_buildup):")
        for t in exempts:
            print(
                f"  {t.get('date')} {t.get('entry_time')} {t.get('event')} "
                f"reg={t.get('oi_regime_at_entry')} net={_inr(t.get('net'))}"
            )
    if haircuts:
        print("CE short_buildup haircut (not blocked):")
        for t in haircuts:
            print(
                f"  {t.get('date')} {t.get('entry_time')} {t.get('event')} "
                f"reg={t.get('oi_regime_at_entry')} net={_inr(t.get('net'))}"
            )
    print(
        f"CE entries aligned with long_buildup: {sim['aligned_ce_long_build_n']}  "
        f"net {_inr(sim['aligned_ce_long_build_net'])}"
    )

    out = {
        "generated_at": datetime.now(IST).isoformat(),
        "range": {"start": days[0]["date"], "end": days[-1]["date"]},
        "sessions": len(days),
        "oi_days": len(oi_reports),
        "missing_oi_days": missing_oi,
        "regime_eod_counts": dict(regime_eod),
        "regime_dominant_counts": dict(regime_dom),
        "oi_daily": oi_reports,
        "algo_overlay": sim,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nJSON {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

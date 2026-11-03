"""
Causal minute-by-minute backtest (no lookahead).

Replays one session as if live: at each minute only data up to that minute
is visible. Fetches the day's bars once, then walks 09:15→session_end.

Usage:
  .venv\\Scripts\\python.exe backtest_day.py
  .venv\\Scripts\\python.exe backtest_day.py --date 2026-10-29
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.config_store import load_config
from foxtrade.scanner import (
    _as_date,
    is_converged,
    primary_trade_week,
    trade_expiries_for_scan,
    _refresh_symbol_expiry_cache,
)
from foxtrade.session_trend import (
    apply_trend_to_scores,
    compute_session_trend,
    counter_trend_blocked,
)
from foxtrade.expert_filters import (
    daily_loss_halted,
    manage_percent_exit,
    size_lots,
    stop_price,
    target_price,
)
from foxtrade.market_direction import analyze_market, pick_leg_for_side
from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "backtests"


@dataclass
class SimTrade:
    algo: str
    side: str
    symbol: str
    expiry: str
    entry_ts: str
    entry_px: float
    exit_ts: str | None = None
    exit_px: float | None = None
    exit_reason: str | None = None
    qty: int = 75
    note: str = ""
    stop_px: float | None = None
    target_px: float | None = None
    peak_px: float | None = None

    @property
    def pnl(self) -> float | None:
        if self.exit_px is None:
            return None
        return round((self.exit_px - self.entry_px) * self.qty, 2)


@dataclass
class SimState:
    open_trades: list[SimTrade] = field(default_factory=list)
    closed: list[SimTrade] = field(default_factory=list)
    last_close_ts: datetime | None = None
    setups_today: int = 0
    losses_today: int = 0
    last_loss_side: str | None = None
    premium_hist: dict[str, list[float]] = field(default_factory=dict)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = map(int, str(s).split(":")[:2])
    return h, m


def _session_bounds(day: date, cfg: dict[str, Any]) -> tuple[datetime, datetime]:
    sh, sm = _parse_hhmm(cfg.get("session_start", "09:15"))
    eh, em = _parse_hhmm(cfg.get("session_end", "13:30"))
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    return start, end


def _fetch_minutes(kite: Any, token: int, start: datetime, end: datetime) -> dict[datetime, float]:
    rows = kite.historical_data(token, start, end, "minute", continuous=False, oi=False)
    out: dict[datetime, float] = {}
    for row in rows:
        dt = row["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        else:
            dt = dt.replace(tzinfo=IST)
        key = dt.replace(second=0, microsecond=0)
        out[key] = float(row["close"])
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _predict_side(
    nifty_closes: list[float],
    ce_px: float,
    pe_px: float,
    cfg: dict[str, Any] | None = None,
) -> tuple[str, float, str]:
    """Live-aligned side pick: RSI bounce map + session trend veto."""
    cfg = cfg or {}
    rsi = _rsi(nifty_closes)
    reasons: list[str] = []
    # Match live prior lightly; session trend dominates on strong days
    prior_pe = float(cfg.get("prior_pe", 0.5))
    score_pe = prior_pe
    score_ce = 1.0 - prior_pe
    # Live mapping: RSI low → CE bounce; RSI high → PE fade
    if rsi is not None:
        if rsi <= 40:
            score_ce += 0.08
            reasons.append(f"RSI {rsi:.0f} low→CE")
        elif rsi >= 60:
            score_pe += 0.08
            reasons.append(f"RSI {rsi:.0f} high→PE")
    if len(nifty_closes) >= 10:
        if nifty_closes[-1] < nifty_closes[-10]:
            score_pe += 0.04
            reasons.append("spot soft 10m→PE")
        else:
            score_ce += 0.04
            reasons.append("spot firm 10m→CE")
    if pe_px < ce_px - 1:
        score_pe += 0.03
        reasons.append("PE cheaper")
    elif ce_px < pe_px - 1:
        score_ce += 0.03
        reasons.append("CE cheaper")

    day_open = nifty_closes[0] if nifty_closes else None
    spot = nifty_closes[-1] if nifty_closes else None
    trend = compute_session_trend(
        spot=spot, day_open=day_open, closes=nifty_closes, cfg=cfg
    )
    score_ce, score_pe, tags = apply_trend_to_scores(score_ce, score_pe, trend)
    reasons.extend(tags)
    shake = detect_opening_shakeout(nifty_closes, cfg=cfg)
    if shake.get("active") and shake.get("prefer_side") == "CE":
        score_ce += 0.18
        score_pe -= 0.12
        reasons.append(str(shake.get("reason")))
    elif shake.get("active") and shake.get("prefer_side") == "PE":
        score_pe += 0.18
        score_ce -= 0.12
        reasons.append(str(shake.get("reason")))

    if score_pe >= score_ce:
        side, conf = "PE", score_pe / max(score_ce + score_pe, 1e-6)
    else:
        side, conf = "CE", score_ce / max(score_ce + score_pe, 1e-6)

    if cfg.get("trend_block_counter", True) and counter_trend_blocked(side, trend):
        side = str(trend.get("prefer_side") or side)
        conf = max(conf, 0.55 + float(trend.get("strength") or 0) * 0.2)
        reasons.append(f"forced {side} (block counter-trend)")
    if shake.get("active") and shake.get("block_side") == side and shake.get("prefer_side"):
        side = str(shake["prefer_side"])
        conf = max(conf, 0.6)
        reasons.append(f"shakeout flip→{side}")

    return side, min(0.95, conf), "; ".join(reasons) or "balanced"


def _build_universe(kite: Any, day: date, spot: float, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    atm = round(spot / 50) * 50
    window = float(cfg.get("strike_window", 600))
    inst = kite.instruments("NFO")
    nifty = [i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    _refresh_symbol_expiry_cache(nifty)
    cur, _nxt, trade_exps, eday = trade_expiries_for_scan(nifty, day)
    print(f"  trade week={cur} expiry_day={eday} ATM≈{atm:.0f} spot0={spot:.1f}")
    out: list[dict[str, Any]] = []
    for i in nifty:
        exp = _as_date(i["expiry"])
        if exp not in trade_exps:
            continue
        if abs(float(i["strike"]) - atm) > window:
            continue
        out.append(
            {
                "tradingsymbol": i["tradingsymbol"],
                "instrument_token": int(i["instrument_token"]),
                "instrument_type": i["instrument_type"],
                "strike": float(i["strike"]),
                "expiry": str(exp),
            }
        )
    return out


def _pairs_at_minute(
    universe: list[dict[str, Any]],
    px: dict[str, float],
    low: float,
    high: float,
    tol: float,
) -> list[dict[str, Any]]:
    by_exp: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for u in universe:
        lp = px.get(u["tradingsymbol"])
        if lp is None or not (low - 10 <= lp <= high + 10):
            continue
        leg = {**u, "ltp": lp}
        by_exp.setdefault(u["expiry"], {"CE": [], "PE": []})[u["instrument_type"]].append(leg)

    pairs: list[dict[str, Any]] = []
    for exp, sides in by_exp.items():
        # same-strike first
        by_k: dict[float, dict[str, dict[str, Any]]] = {}
        for side in ("CE", "PE"):
            for c in sides[side]:
                by_k.setdefault(float(c["strike"]), {})[side] = c
        for strike, legs in by_k.items():
            if "CE" not in legs or "PE" not in legs:
                continue
            ce, pe = legs["CE"], legs["PE"]
            if not (
                (low - 10) <= ce["ltp"] <= (high + 10)
                and (low - 10) <= pe["ltp"] <= (high + 10)
            ):
                continue
            mid = (ce["ltp"] + pe["ltp"]) / 2
            if not (low - 15 <= mid <= high + 15):
                continue
            gap = abs(ce["ltp"] - pe["ltp"])
            pairs.append(
                {
                    "expiry": exp,
                    "strike": strike,
                    "ce": ce,
                    "pe": pe,
                    "gap": gap,
                    "mid": mid,
                    "same_strike": True,
                    "converged": is_converged(ce["ltp"], pe["ltp"], tol, low, high),
                    "id": f"{ce['tradingsymbol']}|{pe['tradingsymbol']}",
                }
            )
        # cross-strike premium match
        if len(pairs) < 3:
            for ce in sides["CE"]:
                for pe in sides["PE"]:
                    if float(ce["strike"]) == float(pe["strike"]):
                        continue
                    mid = (ce["ltp"] + pe["ltp"]) / 2
                    gap = abs(ce["ltp"] - pe["ltp"])
                    if not (low <= mid <= high) or gap > 25:
                        continue
                    pairs.append(
                        {
                            "expiry": exp,
                            "strike": None,
                            "ce": ce,
                            "pe": pe,
                            "gap": gap,
                            "mid": mid,
                            "same_strike": False,
                            "converged": is_converged(ce["ltp"], pe["ltp"], tol, low, high),
                            "id": f"{ce['tradingsymbol']}|{pe['tradingsymbol']}",
                        }
                    )
    pairs.sort(key=lambda p: (0 if p["converged"] else 1, 0 if p["same_strike"] else 1, p["gap"], -p["mid"]))
    return pairs


def run_backtest(day: date) -> dict[str, Any]:
    cfg = load_config()
    kite = get_authenticated_kite()
    start, end = _session_bounds(day, cfg)
    low = float(cfg["premium_low"])
    high = float(cfg["premium_high"])
    tol = float(cfg.get("convergence_tol", 2.0))
    target = float(cfg.get("target_low", 169))
    stop = float(cfg.get("stop_loss", 120))
    lot = int(cfg.get("lot_size", 75))
    a1_lots = int(cfg.get("algo1_lots", 1))
    a2_lots = int(cfg.get("algo2_lots", 1))
    max_setups = int(cfg.get("max_trades_per_day", 4))
    cooldown_m = int(cfg.get("entry_cooldown_minutes", 60))
    min_conf = float(cfg.get("algo1_min_confidence", 0.52))

    print(f"\n=== Causal backtest {day}  {start.strftime('%H:%M')}→{end.strftime('%H:%M')} IST ===")
    print(f"  band {low}-{high} tol={tol} target≥{target} stop≤{stop} (no lookahead)")

    nifty_map = _fetch_minutes(kite, NIFTY_TOKEN, start, end)
    if not nifty_map:
        raise RuntimeError(f"No Nifty minute bars for {day}")
    first_ts = min(nifty_map)
    spot0 = nifty_map[first_ts]
    universe = _build_universe(kite, day, spot0, cfg)
    print(f"  universe legs={len(universe)}")

    # Prefetch all candidate minutes (OK — sim only reads ≤ current minute)
    hist: dict[str, dict[datetime, float]] = {}
    for i, u in enumerate(universe):
        hist[u["tradingsymbol"]] = _fetch_minutes(
            kite, int(u["instrument_token"]), start, end
        )
        if (i + 1) % 20 == 0:
            print(f"  fetched {i+1}/{len(universe)} option series…")
    print(f"  fetched {len(universe)} option series")

    minutes = sorted(nifty_map.keys())
    state = SimState()
    timeline: list[dict[str, Any]] = []

    nifty_path: list[float] = []
    for ts in minutes:
        # ---- only data up to ts ----
        nifty_path.append(nifty_map[ts])
        px = {
            sym: series[ts]
            for sym, series in hist.items()
            if ts in series
        }

        # Track option premium paths for firmness filter
        for sym, cur_px in px.items():
            state.premium_hist.setdefault(sym, []).append(float(cur_px))

        spot_now = nifty_map[ts]
        day_open = nifty_path[0] if nifty_path else spot_now
        trend = compute_session_trend(
            spot=spot_now, day_open=day_open, closes=nifty_path, cfg=cfg
        )
        prefer = trend.get("prefer_side")
        day_pnl = sum(t.pnl or 0 for t in state.closed)

        # Manage open trades first (causal exits) — per-trade stop/target + dual bleed cut
        still_open: list[SimTrade] = []
        dual_legs = [t for t in state.open_trades if t.algo == "algo2"]
        for tr in state.open_trades:
            cur = px.get(tr.symbol)
            if cur is None:
                still_open.append(tr)
                continue
            reason = None
            peak = max(float(tr.peak_px or tr.entry_px), cur, tr.entry_px)
            tr.peak_px = peak
            held_m = 0.0
            try:
                held_m = (ts - datetime.fromisoformat(tr.entry_ts)).total_seconds() / 60.0
            except Exception:
                held_m = 0.0
            if cfg.get("use_percent_exits", True) and tr.algo == "algo1":
                dec = manage_percent_exit(
                    entry=tr.entry_px, px=cur, peak=peak, cfg=cfg, held_minutes=held_m
                )
                tr.stop_px = float(dec.get("stop_px") or tr.stop_px or stop)
                tr.target_px = float(dec.get("target_px") or tr.target_px or target)
                if dec.get("action") == "exit":
                    reason = str(dec.get("reason") or "exit")
            else:
                sl = tr.stop_px if tr.stop_px is not None else stop
                tg = tr.target_px if tr.target_px is not None else target
                if prefer and float(trend.get("strength") or 0) >= 0.45 and tr.side == prefer:
                    tg = max(tg, float(cfg.get("target_high", 175)), target_price(tr.entry_px, cfg, trail=True))
                if (
                    tr.algo == "algo2"
                    and prefer
                    and tr.side != prefer
                    and len(dual_legs) >= 1
                    and trend.get("block_counter_trend")
                ):
                    soft_stop = float(cfg.get("algo2_trend_fallen_sl_pts", 6))
                    if cur <= tr.entry_px - soft_stop or cur <= sl:
                        reason = f"trend cut {prefer} day"
                if reason is None:
                    time_stop_m = float(cfg.get("time_stop_minutes", 15))
                    if held_m >= time_stop_m and cur < tr.entry_px:
                        reason = f"time stop {time_stop_m:g}m fail"
                    elif cur >= tg:
                        reason = f"target {tg:g}"
                    elif cur <= sl:
                        reason = f"stop {sl:g}"
            if reason:
                tr.exit_ts = ts.isoformat()
                tr.exit_px = cur
                tr.exit_reason = reason
                state.closed.append(tr)
                state.last_close_ts = ts
                if (tr.pnl or 0) < 0:
                    state.losses_today += 1
                    state.last_loss_side = tr.side
                timeline.append(
                    {
                        "ts": ts.isoformat(),
                        "event": "EXIT",
                        "algo": tr.algo,
                        "symbol": tr.symbol,
                        "px": cur,
                        "reason": reason,
                        "pnl": tr.pnl,
                    }
                )
            else:
                still_open.append(tr)
        state.open_trades = still_open

        in_entry_window = start <= ts <= end
        flat = len(state.open_trades) == 0
        if not in_entry_window or not flat:
            continue
        if state.setups_today >= max_setups:
            continue
        if state.last_close_ts is not None:
            if (ts - state.last_close_ts).total_seconds() < cooldown_m * 60:
                continue

        if daily_loss_halted(day_pnl, cfg):
            continue

        # --- Directional one-side: market analysis only (no convergence / dual) ---
        if not cfg.get("algo1_enabled", True):
            continue
        if cfg.get("directional_mode", True):
            analysis = analyze_market(nifty_path, cfg=cfg)
            if not analysis.get("allow_entry"):
                continue
            side = analysis.get("side")
            if side not in ("CE", "PE"):
                continue
            legs_now = []
            for u in universe:
                if u.get("instrument_type") != side:
                    continue
                sym = u["tradingsymbol"]
                if sym not in px:
                    continue
                row = dict(u)
                row["ltp"] = px[sym]
                legs_now.append(row)
            pick = pick_leg_for_side(legs_now, side, premium_low=low, premium_high=high)
            if not pick:
                continue
            if state.losses_today >= int(cfg.get("max_losses_before_halt", 1)):
                continue
            if cfg.get("bank_after_win", True) and day_pnl > 0 and state.setups_today >= 1:
                continue
            entry_px = float(pick["ltp"])
            lots = size_lots(cfg, entry=entry_px)
            if state.losses_today >= 2:
                lots = max(1, lots // 2)
            tr = SimTrade(
                algo="algo1",
                side=side,
                symbol=pick["tradingsymbol"],
                expiry=str(pick.get("expiry") or ""),
                entry_ts=ts.isoformat(),
                entry_px=entry_px,
                qty=lots * lot,
                stop_px=stop_price(entry_px, cfg),
                target_px=target_price(entry_px, cfg, trail=True),
                peak_px=entry_px,
                note=(
                    f"DIR {analysis.get('event')} conf={float(analysis.get('confidence') or 0):.0%} "
                    f"lots={lots} | {analysis.get('reason')}"
                ),
            )
            state.open_trades.append(tr)
            state.setups_today += 1
            timeline.append(
                {
                    "ts": ts.isoformat(),
                    "event": "ENTRY",
                    "algo": "algo1",
                    "symbol": tr.symbol,
                    "px": tr.entry_px,
                    "side": side,
                    "reason": tr.note,
                }
            )
            continue

    # Force EOD flat at session end (last available px)
    last_ts = minutes[-1]
    for tr in list(state.open_trades):
        cur = hist.get(tr.symbol, {}).get(last_ts) or tr.entry_px
        tr.exit_ts = last_ts.isoformat()
        tr.exit_px = float(cur)
        tr.exit_reason = "session end flat"
        state.closed.append(tr)
        timeline.append(
            {
                "ts": last_ts.isoformat(),
                "event": "EXIT",
                "algo": tr.algo,
                "symbol": tr.symbol,
                "px": tr.exit_px,
                "reason": tr.exit_reason,
                "pnl": tr.pnl,
            }
        )
    state.open_trades.clear()

    pnls = [t.pnl for t in state.closed if t.pnl is not None]
    report = {
        "day": day.isoformat(),
        "mode": "causal_minute",
        "lookahead": False,
        "session": {"start": start.isoformat(), "end": end.isoformat()},
        "spot_open": spot0,
        "spot_close": nifty_map[minutes[-1]],
        "trade_week": str(primary_trade_week(day) or ""),
        "minutes": len(minutes),
        "universe_legs": len(universe),
        "setups": state.setups_today,
        "trades": [
            {
                "algo": t.algo,
                "side": t.side,
                "symbol": t.symbol,
                "expiry": t.expiry,
                "entry_ts": t.entry_ts,
                "entry_px": t.entry_px,
                "exit_ts": t.exit_ts,
                "exit_px": t.exit_px,
                "exit_reason": t.exit_reason,
                "qty": t.qty,
                "pnl": t.pnl,
                "note": t.note,
            }
            for t in state.closed
        ],
        "pnl_total": round(sum(pnls), 2) if pnls else 0.0,
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "timeline": timeline,
        "config_snapshot": {
            "premium_low": low,
            "premium_high": high,
            "target_low": target,
            "stop_loss": stop,
            "convergence_tol": tol,
            "max_trades_per_day": max_setups,
            "entry_cooldown_minutes": cooldown_m,
        },
    }
    return report


def _inr(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "−" if n < 0 else ("+" if n > 0 else "")
    return f"{sign}₹{abs(n):,.0f}"


def _hhmm(ts: str | None) -> str:
    if not ts:
        return "—"
    return ts[11:16] if len(ts) >= 16 else ts


def write_html_report(report: dict[str, Any], path: Path) -> None:
    """Self-contained HTML report with SVG charts from a causal backtest dict."""
    day = report["day"]
    cfg = report.get("config_snapshot") or {}
    trades = report.get("trades") or []
    timeline = report.get("timeline") or []
    pnl_total = float(report.get("pnl_total") or 0)
    wins = int(report.get("wins") or 0)
    losses = int(report.get("losses") or 0)
    spot0 = float(report.get("spot_open") or 0)
    spot1 = float(report.get("spot_close") or 0)
    spot_chg = spot1 - spot0
    sess = report.get("session") or {}
    sess_start = _hhmm(sess.get("start"))
    sess_end = _hhmm(sess.get("end"))

    # Cumulative series from timeline exits (+ session open/close anchors)
    cum_pts: list[tuple[str, float, str]] = [(sess_start, 0.0, "Session open")]
    cum = 0.0
    for ev in timeline:
        if ev.get("event") == "EXIT" and ev.get("pnl") is not None:
            cum += float(ev["pnl"])
            cum_pts.append((_hhmm(ev.get("ts")), cum, f"{ev.get('symbol')} {ev.get('reason')}"))
        elif ev.get("event") == "ENTRY":
            cum_pts.append((_hhmm(ev.get("ts")), cum, f"ENTRY {ev.get('symbol')}"))
    if cum_pts[-1][0] != sess_end:
        cum_pts.append((sess_end, cum, "Session end"))

    # Line chart geometry
    n = max(len(cum_pts), 1)
    y_vals = [p[1] for p in cum_pts]
    y_abs = max(max(abs(v) for v in y_vals), 500.0)
    pad_l, pad_r, pad_t, pad_b = 70, 40, 30, 50
    w, h = 1000, 280
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    mid_y = pad_t + plot_h / 2

    def x_at(i: int) -> float:
        if n == 1:
            return pad_l
        return pad_l + i * plot_w / (n - 1)

    def y_at(v: float) -> float:
        return mid_y - (v / y_abs) * (plot_h / 2)

    poly = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, (_, v, _) in enumerate(cum_pts))
    circles = []
    xlabels = []
    for i, (t, v, _) in enumerate(cum_pts):
        fill = "#26a69a" if v > 0 else ("#ef5350" if v < 0 else "#3d9cf0")
        circles.append(
            f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="4" fill="{fill}" />'
        )
        if i == 0 or i == n - 1 or abs(v) > 1 or i % max(1, n // 6) == 0:
            circles.append(
                f'<text x="{x_at(i):.1f}" y="{y_at(v) - 10:.1f}" fill="{fill}" '
                f'font-size="10" text-anchor="middle">{_inr(v)}</text>'
            )
        xlabels.append(
            f'<text x="{x_at(i):.1f}" y="{h - 18}" fill="#8fa0b8" '
            f'font-size="11" text-anchor="middle">{t}</text>'
        )

    # Bar chart for trades
    bar_w, bar_h = 480, 260
    bar_pad_l, bar_pad_t = 50, 30
    bar_zero = 120
    bar_scale = 90 / max(max((abs(float(t.get("pnl") or 0)) for t in trades), default=1), 1)
    bars_svg = []
    slot = (bar_w - bar_pad_l - 20) / max(len(trades), 1)
    for i, t in enumerate(trades):
        pnl = float(t.get("pnl") or 0)
        bh = abs(pnl) * bar_scale
        cx = bar_pad_l + i * slot + slot / 2
        bw = min(48, slot * 0.55)
        fill = "#26a69a" if pnl >= 0 else "#ef5350"
        if pnl >= 0:
            y = bar_zero - bh
        else:
            y = bar_zero
        bars_svg.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" '
            f'fill="{fill}" rx="3" />'
            f'<text x="{cx:.1f}" y="{(y - 6 if pnl >= 0 else y + bh + 14):.1f}" fill="{fill}" '
            f'font-size="10" text-anchor="middle">{_inr(pnl)}</text>'
            f'<text x="{cx:.1f}" y="230" fill="#8fa0b8" font-size="10" text-anchor="middle">'
            f'{t.get("algo","")} {t.get("side","")}</text>'
        )

    # Win/loss bars
    wl_max = max(wins, losses, 1)
    win_w = 270 * wins / wl_max
    loss_w = 270 * losses / wl_max

    trade_rows = []
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        cls = "win" if pnl > 0 else ("loss" if pnl < 0 else "")
        trade_rows.append(
            "<tr class='" + cls + "'>"
            f"<td>{_hhmm(t.get('entry_ts'))}→{_hhmm(t.get('exit_ts'))}</td>"
            f"<td>{t.get('algo','')}</td><td>{t.get('side','')}</td>"
            f"<td>{t.get('symbol','')}</td>"
            f"<td class='num'>{float(t.get('entry_px') or 0):.2f}</td>"
            f"<td class='num'>{float(t.get('exit_px') or 0):.2f}</td>"
            f"<td class='num {'up' if pnl > 0 else 'down'}'>{_inr(pnl)}</td>"
            f"<td>{t.get('exit_reason') or ''}</td>"
            f"<td>{(t.get('note') or '')[:80]}</td>"
            "</tr>"
        )

    event_rows = []
    cum_e = 0.0
    for ev in timeline:
        if ev.get("event") == "EXIT" and ev.get("pnl") is not None:
            cum_e += float(ev["pnl"])
            detail = (
                f"{ev.get('symbol')} {ev.get('reason')} → "
                f"{ev.get('px')} ({_inr(float(ev['pnl']))})"
            )
        else:
            detail = f"{ev.get('algo','')} {ev.get('side','')} {ev.get('symbol','')} · {ev.get('reason') or ''}"
        cls = ""
        if ev.get("event") == "EXIT":
            p = float(ev.get("pnl") or 0)
            cls = "win" if p > 0 else ("loss" if p < 0 else "")
        event_rows.append(
            f"<tr class='{cls}'>"
            f"<td>{_hhmm(ev.get('ts'))}</td>"
            f"<td>{ev.get('event','')}</td>"
            f"<td>{detail}</td>"
            f"<td class='num {'up' if cum_e > 0 else ('down' if cum_e < 0 else '')}'>{_inr(cum_e)}</td>"
            "</tr>"
        )

    tone = "down" if pnl_total < 0 else ("up" if pnl_total > 0 else "")
    banner_cls = "banner down" if pnl_total < 0 else ("banner up" if pnl_total > 0 else "banner")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>FoxtronTrade — Causal backtest {day}</title>
  <style>
    :root {{
      --bg: #0b1017; --panel: #141c27; --line: #243041; --text: #e8eef7;
      --muted: #8fa0b8; --accent: #3d9cf0; --up: #26a69a; --down: #ef5350;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1a2740 0%, var(--bg) 55%);
      color: var(--text); min-height: 100vh;
    }}
    header {{
      display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
      padding: 14px 20px; border-bottom: 1px solid var(--line);
      background: #101826cc; position: sticky; top: 0; z-index: 5;
    }}
    h1 {{ margin: 0; font-size: 1.15rem; font-weight: 650; }}
    .meta {{ color: var(--muted); font-size: 0.8rem; }}
    .chip {{
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 0.7rem; border: 1px solid var(--line); color: var(--muted);
    }}
    .chip.ok {{ color: var(--accent); border-color: #2a5a8a; }}
    .chip.loss {{ color: var(--down); border-color: #8a3a3a; }}
    .chip.win {{ color: var(--up); border-color: #1a6a5a; }}
    main {{ padding: 18px 20px 48px; max-width: 1100px; margin: 0 auto; }}
    .cards {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px; margin-bottom: 18px;
    }}
    .card {{
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; padding: 14px 16px;
    }}
    .card .label {{
      color: var(--muted); font-size: 0.72rem;
      text-transform: uppercase; letter-spacing: 0.04em;
    }}
    .card .value {{
      font-size: 1.35rem; font-weight: 700; margin-top: 6px;
      font-variant-numeric: tabular-nums;
    }}
    .up {{ color: var(--up); }} .down {{ color: var(--down); }}
    .panel {{
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; overflow: hidden; margin-bottom: 14px;
    }}
    .panel h2 {{
      margin: 0; padding: 10px 14px; font-size: 0.85rem; color: var(--muted);
      border-bottom: 1px solid var(--line); font-weight: 600;
    }}
    .panel .body {{ padding: 14px; }}
    .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    @media (max-width: 860px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
    th, td {{
      padding: 8px 10px; border-bottom: 1px solid var(--line);
      text-align: left; white-space: nowrap;
    }}
    th {{ color: var(--muted); background: #101826; }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    tr.win td {{ background: rgba(38, 166, 154, 0.06); }}
    tr.loss td {{ background: rgba(239, 83, 80, 0.06); }}
    .note {{ color: var(--muted); font-size: 0.75rem; margin-top: 8px; }}
    .banner {{
      margin-bottom: 16px; padding: 12px 14px; border-radius: 10px;
      border: 1px solid var(--line); background: #1a2330; font-size: 0.9rem;
    }}
    .banner.down {{ border-color: #6a3030; background: #2a1818; color: #f0c0c0; }}
    .banner.up {{ border-color: #1a6a5a; background: #142820; color: #b8e8d8; }}
    svg.chart {{ width: 100%; height: auto; display: block; }}
    .legend {{
      display: flex; gap: 14px; flex-wrap: wrap;
      font-size: 0.75rem; color: var(--muted); margin-top: 8px;
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Causal backtest log · {day}</h1>
      <div class="meta">
        Minute walk {sess_start}→{sess_end} IST · trade week {report.get('trade_week','')} ·
        band {cfg.get('premium_low')}–{cfg.get('premium_high')} ·
        target ≥{cfg.get('target_low')} · stop ≤{cfg.get('stop_loss')}
      </div>
    </div>
    <span style="flex:1"></span>
    <span class="chip ok">no lookahead</span>
    <span class="chip">paper sim</span>
    <span class="chip {'win' if wins >= losses else 'loss'}">{wins}W / {losses}L</span>
  </header>
  <main>
    <div class="{banner_cls}">
      Day PnL <strong>{_inr(pnl_total)}</strong> · spot
      {spot0:,.1f} → {spot1:,.1f} ({_inr(spot_chg).replace('₹','')}).
      Causal minute replay — decisions used only bars ≤ current minute.
    </div>
    <div class="cards">
      <div class="card"><div class="label">Net PnL</div>
        <div class="value {tone}">{_inr(pnl_total)}</div></div>
      <div class="card"><div class="label">Wins / losses</div>
        <div class="value">{wins} / {losses}</div></div>
      <div class="card"><div class="label">Legs filled</div>
        <div class="value">{len(trades)}</div></div>
      <div class="card"><div class="label">Spot change</div>
        <div class="value {'up' if spot_chg >= 0 else 'down'}">{spot_chg:+.0f}</div></div>
      <div class="card"><div class="label">Setups</div>
        <div class="value">{report.get('setups', 0)}</div></div>
      <div class="card"><div class="label">Minutes replayed</div>
        <div class="value">{report.get('minutes', 0)}</div></div>
    </div>

    <div class="panel">
      <h2>Cumulative PnL through the session (₹)</h2>
      <div class="body">
        <svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Cumulative PnL">
          <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" stroke="#243041" />
          <line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{w - pad_r}" y2="{pad_t + plot_h}" stroke="#243041" />
          <line x1="{pad_l}" y1="{mid_y}" x2="{w - pad_r}" y2="{mid_y}" stroke="#3a4558" stroke-dasharray="4 4" />
          <text x="{pad_l - 8}" y="{mid_y + 4}" fill="#8fa0b8" font-size="11" text-anchor="end">0</text>
          <text x="{pad_l - 8}" y="{pad_t + 8}" fill="#8fa0b8" font-size="11" text-anchor="end">{_inr(y_abs)}</text>
          <text x="{pad_l - 8}" y="{pad_t + plot_h}" fill="#8fa0b8" font-size="11" text-anchor="end">{_inr(-y_abs)}</text>
          <polyline fill="none" stroke="#ef5350" stroke-width="2.5" points="{poly}" />
          {''.join(circles)}
          {''.join(xlabels)}
        </svg>
        <div class="legend">Cumulative after each timeline event · breakeven dashed</div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <h2>PnL by leg (₹)</h2>
        <div class="body">
          <svg class="chart" viewBox="0 0 {bar_w} {bar_h}" role="img" aria-label="Per-leg PnL">
            <line x1="{bar_pad_l}" y1="20" x2="{bar_pad_l}" y2="200" stroke="#243041" />
            <line x1="{bar_pad_l}" y1="{bar_zero}" x2="450" y2="{bar_zero}" stroke="#3a4558" stroke-dasharray="4 4" />
            {''.join(bars_svg) if bars_svg else '<text x="240" y="130" fill="#8fa0b8" text-anchor="middle">No trades</text>'}
          </svg>
        </div>
      </div>
      <div class="panel">
        <h2>Win vs loss count</h2>
        <div class="body">
          <svg class="chart" viewBox="0 0 480 200" role="img" aria-label="Win loss">
            <text x="20" y="60" fill="#8fa0b8" font-size="13">Wins</text>
            <rect x="80" y="40" width="{win_w:.1f}" height="28" fill="#26a69a" rx="4" />
            <text x="{80 + win_w + 10:.1f}" y="60" fill="#26a69a" font-size="14" font-weight="700">{wins}</text>
            <text x="20" y="120" fill="#8fa0b8" font-size="13">Losses</text>
            <rect x="80" y="100" width="{loss_w:.1f}" height="28" fill="#ef5350" rx="4" />
            <text x="{80 + loss_w + 10:.1f}" y="120" fill="#ef5350" font-size="14" font-weight="700">{losses}</text>
          </svg>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>Trade blotter</h2>
      <table>
        <thead>
          <tr>
            <th>Window</th><th>Algo</th><th>Side</th><th>Symbol</th>
            <th class="num">Entry</th><th class="num">Exit</th>
            <th class="num">PnL</th><th>Reason</th><th>Note</th>
          </tr>
        </thead>
        <tbody>
          {''.join(trade_rows) if trade_rows else '<tr><td colspan="9">No trades</td></tr>'}
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>Event log</h2>
      <table>
        <thead>
          <tr><th>Time</th><th>Event</th><th>Detail</th><th class="num">Cum PnL</th></tr>
        </thead>
        <tbody>
          {''.join(event_rows) if event_rows else '<tr><td colspan="4">No events</td></tr>'}
        </tbody>
      </table>
    </div>

    <p class="note">
      Config: premium {cfg.get('premium_low')}–{cfg.get('premium_high')} ·
      tol {cfg.get('convergence_tol')} · max {cfg.get('max_trades_per_day')} setups/day ·
      cooldown {cfg.get('entry_cooldown_minutes')}m.
      JSON: data/backtests/backtest_{day}.json
    </p>
  </main>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Causal minute-by-minute algo backtest")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today IST, or last weekday)")
    args = ap.parse_args()
    today = datetime.now(IST).date()
    if args.date:
        day = date.fromisoformat(args.date)
    else:
        day = today
        # If after hours or weekend with no bars, still try today first
        if day.weekday() >= 5:
            while day.weekday() >= 5:
                day -= timedelta(days=1)

    report = run_backtest(day)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"backtest_{day.isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    html_path = OUT_DIR / f"backtest_{day.isoformat()}_report.html"
    write_html_report(report, html_path)

    print("\n--- Results (causal, no lookahead) ---")
    print(f"  day={report['day']}  trade_week={report['trade_week']}")
    print(f"  spot {report['spot_open']:.1f} → {report['spot_close']:.1f}")
    print(f"  setups={report['setups']}  trades={len(report['trades'])}  "
          f"W{report['wins']}/L{report['losses']}  PnL₹ {report['pnl_total']}")
    for t in report["trades"]:
        print(
            f"  {t['entry_ts'][11:16]}→{(t['exit_ts'] or '')[11:16]} "
            f"{t['algo']:5} {t['side']:2} {t['symbol']:22} "
            f"{t['entry_px']:.1f}→{t['exit_px']:.1f} "
            f"₹{t['pnl']:+.0f}  {t['exit_reason']}"
        )
    print(f"\nSaved {out_path}")
    print(f"Report {html_path}")


if __name__ == "__main__":
    main()

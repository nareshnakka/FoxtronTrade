"""
Dual live book: scan Nifty + Sensex every tick, allocate Zerodha cash,
enter the better setup (fallback to the other if preferred has no entry).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from foxtrade.algo1_breakout import Algo1Breakout
from foxtrade.capital_allocator import (
    allocate_dual,
    fetch_zerodha_available,
    lots_for_budget,
    open_premium_locked,
)
from foxtrade.market_direction import analyze_market
from foxtrade.scanner import scan_premium_band_underlying
from foxtrade import trade_store
from foxtrade.sensex_calendar import (
    is_sensex_expiry_day,
    is_sensex_trade_day,
    sensex_day_role,
)
from foxtrade.underlyings import cfg_for_underlying, list_underlyings


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def score_opportunity(
    closes: list[float],
    cfg: dict[str, Any],
    *,
    is_expiry_day: bool = False,
) -> dict[str, Any]:
    """Causal analyze_market score without placing an order."""
    if len(closes) < 40:
        return {
            "score": 0.0,
            "allow": False,
            "side": None,
            "conf": 0.0,
            "event": None,
            "reason": "warming",
        }
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    a = analyze_market(
        closes,
        cfg=cfg,
        ema9=ema9,
        ema21=ema21,
        is_expiry_day=is_expiry_day,
    )
    allow = bool(a.get("allow_entry")) and a.get("side") in ("CE", "PE")
    conf = float(a.get("confidence") or 0)
    # Soft secondary paths still count as opportunity (micro/PA) — lighter weight
    score = conf if allow else 0.0
    if not allow:
        # Probe micro / PA if enabled so Sensex quality book can compete
        try:
            from foxtrade.micro_scalp import detect_pullback_resume, micro_enabled
            from foxtrade.daily_pa import detect_daily_pa, daily_pa_enabled

            if micro_enabled(cfg):
                ms = detect_pullback_resume(closes, cfg)
                if ms.get("allow_entry"):
                    score = max(score, float(ms.get("confidence") or 0.55) * 0.85)
                    allow = True
                    a = {**a, **ms, "allow_entry": True}
            if not allow and daily_pa_enabled(cfg):
                pa = detect_daily_pa(closes, cfg)
                if pa.get("allow_entry"):
                    score = max(score, float(pa.get("confidence") or 0.55) * 0.80)
                    allow = True
                    a = {**a, **pa, "allow_entry": True}
        except Exception:
            pass
    return {
        "score": round(float(score), 4),
        "allow": bool(allow and score > 0),
        "side": a.get("side"),
        "conf": conf,
        "event": a.get("event"),
        "reason": str(a.get("reason") or "")[:160],
        "analysis": a,
    }


class DualBook:
    """Owns one Algo1Breakout per underlying + capital router."""

    def __init__(self) -> None:
        self.books: dict[str, Algo1Breakout] = {
            "NIFTY": Algo1Breakout(algo_id="algo1", underlying="NIFTY"),
            "SENSEX": Algo1Breakout(algo_id="algo1_sx", underlying="SENSEX"),
        }
        self.last_alloc: dict[str, Any] = {}
        self.last_scores: dict[str, Any] = {}
        self.last_scans: dict[str, Any] = {}
        self.last_message: str = "dual book idle"

    def restore(self, runtime: dict[str, Any] | None = None) -> list[str]:
        restored = []
        for uid, algo in self.books.items():
            if algo.restore_from_store(runtime):
                restored.append(uid)
        return restored

    def runtime_snapshot(self) -> dict[str, Any]:
        return {algo.algo_id: algo.runtime_snapshot() for algo in self.books.values()}

    def open_count_by_underlying(self) -> dict[str, int]:
        out = {"NIFTY": 0, "SENSEX": 0}
        for t in trade_store.open_trades():
            if str(t.get("status") or "").upper() != "OPEN":
                continue
            feat = t.get("entry_features") or {}
            uid = str(feat.get("underlying") or "").upper()
            if not uid:
                sym = str(t.get("symbol") or "")
                uid = "SENSEX" if sym.startswith("SENSEX") else "NIFTY"
            if uid in out:
                out[uid] += 1
            algo = str(t.get("algo") or "")
            if algo == "algo1_sx":
                out["SENSEX"] = max(out["SENSEX"], 1)
            elif algo == "algo1":
                out["NIFTY"] = max(out["NIFTY"], 1)
        return out

    def scan_all(self, base_cfg: dict[str, Any]) -> dict[str, Any]:
        scans: dict[str, Any] = {}
        for spec in list_underlyings(base_cfg):
            uid = spec["id"]
            try:
                ucfg = cfg_for_underlying(base_cfg, uid)
                scans[uid] = scan_premium_band_underlying(
                    spec,
                    premium_low=float(ucfg.get("premium_low") or spec["premium_low"]),
                    premium_high=float(ucfg.get("premium_high") or spec["premium_high"]),
                    strike_window=float(spec.get("strike_window") or 600),
                    wide_low=float(spec.get("wide_low") or 40),
                    wide_high=float(spec.get("wide_high") or 220),
                )
            except Exception as exc:  # noqa: BLE001
                scans[uid] = {
                    "underlying": uid,
                    "error": str(exc),
                    "candidates": [],
                    "pair": None,
                    "spot": None,
                }
        self.last_scans = scans
        return scans

    def on_tick(
        self,
        *,
        base_cfg: dict[str, Any],
        quotes: dict[str, float],
        spot_closes: dict[str, list[float]],
        index_meta: dict[str, dict[str, Any]],
        ctx_common: dict[str, Any],
        try_acquire: Callable[[str], bool],
        release: Callable[[str], None],
    ) -> None:
        """
        1) Manage open legs on both books.
        2) Score opportunities.
        3) Allocate Zerodha cash.
        4) Arm preferred underlying (fallback to other) and tick its algo.
        """
        if not base_cfg.get("dual_mode", False):
            # Single-book: Nifty only
            nifty = self.books["NIFTY"]
            cfg = cfg_for_underlying(base_cfg, "NIFTY")
            scan = self.last_scans.get("NIFTY") or ctx_common.get("scan") or {}
            closes = spot_closes.get("NIFTY") or []
            idx = dict(index_meta.get("NIFTY") or {})
            if closes and "spot" not in idx:
                idx["spot"] = closes[-1]
                idx["spot_closes"] = closes
            ctx = {
                **ctx_common,
                "config": cfg,
                "scan": scan,
                "pair": (scan or {}).get("pair"),
                "quotes": quotes,
                "index": idx,
                "try_acquire": try_acquire,
                "release": release,
            }
            nifty.on_tick(ctx)
            return

        # Always manage open positions first
        for uid, algo in self.books.items():
            cfg = cfg_for_underlying(base_cfg, uid)
            scan = self.last_scans.get(uid) or {}
            closes = spot_closes.get(uid) or []
            idx = dict(index_meta.get(uid) or {})
            if closes:
                idx["spot"] = closes[-1]
                idx["spot_closes"] = closes
            ctx = {
                **ctx_common,
                "config": cfg,
                "scan": scan,
                "pair": scan.get("pair"),
                "quotes": quotes,
                "index": idx,
                "try_acquire": try_acquire,
                "release": release,
            }
            # Manage-only path: if open, on_tick manages and returns
            if algo.state.open_trade_id or algo.state.phase == "IN_TRADE":
                algo.on_tick(ctx)

        opens = self.open_count_by_underlying()
        max_open = int(base_cfg.get("dual_max_open", 2))
        total_open = sum(opens.values())

        scores: dict[str, float] = {}
        details: dict[str, Any] = {}
        for uid in ("NIFTY", "SENSEX"):
            cfg = cfg_for_underlying(base_cfg, uid)
            closes = spot_closes.get(uid) or []
            scan = self.last_scans.get(uid) or {}
            # Sensex: Tue/Wed always; Thu only on gamma blast; Mon cutoff → Nifty capital
            if uid == "SENSEX" and not is_sensex_trade_day(cfg=cfg, closes=closes):
                role = sensex_day_role(cfg=cfg, closes=closes)
                det = {
                    "score": 0.0,
                    "allow": False,
                    "side": None,
                    "conf": 0.0,
                    "event": None,
                    "reason": f"sensex calendar OFF ({role})",
                }
                scores[uid] = 0.0
                details[uid] = det
                continue
            if uid == "SENSEX":
                from foxtrade.sensex_calendar import is_sensex_gamma_conditional_day
                from foxtrade.sensex_gamma import detect_sensex_gamma_blast

                is_exp = is_sensex_expiry_day(cfg=cfg)
                # Thursday gamma blast → same fast expiry-style playbook
                if is_sensex_gamma_conditional_day(cfg=cfg):
                    blast = detect_sensex_gamma_blast(closes, cfg=cfg)
                    is_exp = bool(blast.get("allow"))
            else:
                is_exp = bool(scan.get("expiry_day"))
            det = score_opportunity(
                closes,
                cfg,
                is_expiry_day=is_exp,
            )
            # No capital chase if already open on that underlying
            if opens.get(uid, 0) > 0:
                det = {**det, "score": 0.0, "allow": False, "reason": "already open"}
            scores[uid] = float(det["score"]) if det.get("allow") else 0.0
            details[uid] = det
        self.last_scores = details

        # Margins — paper uses configured capital; live uses Zerodha cash
        # capped at capital_inr so the ₹1L production book cannot oversize.
        paper = not bool(base_cfg.get("live_trading"))
        book_cap = float(base_cfg.get("capital_inr") or 100_000)
        if paper:
            avail = book_cap
            z = {"available": avail, "ok": 1.0, "cash": avail, "source": "paper"}
        else:
            z = fetch_zerodha_available()
            avail = float(z.get("available") or 0)
            if avail <= 0:
                avail = book_cap
            elif book_cap > 0:
                avail = min(avail, book_cap)
            z = {**z, "book_cap": book_cap, "deployable": avail}

        locked = open_premium_locked(trade_store.open_trades())
        alloc = allocate_dual(
            available=avail,
            cfg=base_cfg,
            scores=scores,
            open_by_underlying=opens,
            locked_premium=locked,
        )
        alloc["zerodha"] = z
        self.last_alloc = alloc

        if total_open >= max_open:
            self.last_message = (
                f"dual max open {total_open}/{max_open} · {alloc.get('reason')}"
            )
            return

        # Order: preferred first, then fallback
        order: list[str] = []
        pref = alloc.get("preferred")
        if pref:
            order.append(str(pref))
        for uid in ("NIFTY", "SENSEX"):
            if uid not in order and scores.get(uid, 0) > 0:
                order.append(uid)

        if not order:
            self.last_message = f"dual scanning · no entry · {alloc.get('reason')}"
            return

        for uid in order:
            if opens.get(uid, 0) > 0:
                continue
            if total_open >= max_open:
                break
            budget = float((alloc.get("budgets") or {}).get(uid) or 0)
            if budget < float(alloc.get("min_ticket") or 8000):
                continue
            det = details.get(uid) or {}
            if not det.get("allow"):
                continue

            algo = self.books[uid]
            cfg = cfg_for_underlying(base_cfg, uid)
            cfg["capital_inr"] = budget
            cfg["deploy_pct_of_balance"] = 1.0  # budget already sized
            scan = self.last_scans.get(uid) or {}
            # Estimate lots from ATM mid for override
            pair = scan.get("pair") or {}
            side = det.get("side")
            entry_est = None
            if side == "CE" and pair.get("ce"):
                entry_est = float(pair["ce"].get("ltp") or 0)
            elif side == "PE" and pair.get("pe"):
                entry_est = float(pair["pe"].get("ltp") or 0)
            if not entry_est and scan.get("candidates"):
                for c in scan["candidates"]:
                    if c.get("instrument_type") == side:
                        entry_est = float(c.get("ltp") or 0)
                        break
            lot_size = int(cfg.get("lot_size") or 75)
            if entry_est and entry_est > 0:
                lots = lots_for_budget(
                    budget=budget, entry=entry_est, lot_size=lot_size, cfg=cfg
                )
                if lots <= 0:
                    self.last_message = (
                        f"{uid} signal but budget ₹{budget:,.0f} "
                        f"< 1 lot @ {entry_est:.1f}"
                    )
                    continue
                cfg["lots_override"] = lots
                cfg["min_lots_per_trade"] = min(lots, int(cfg.get("min_lots_per_trade") or 1))

            closes = spot_closes.get(uid) or []
            idx = dict(index_meta.get(uid) or {})
            if closes:
                idx["spot"] = closes[-1]
                idx["spot_closes"] = closes
            ctx = {
                **ctx_common,
                "config": cfg,
                "scan": scan,
                "pair": scan.get("pair"),
                "quotes": quotes,
                "index": idx,
                "try_acquire": try_acquire,
                "release": release,
            }
            before = algo.state.open_trade_id
            algo.on_tick(ctx)
            after = algo.state.open_trade_id
            if after and after != before:
                # Tag underlying on the new trade features if possible
                try:
                    from foxtrade import trade_store as ts

                    row = next(
                        (t for t in ts.open_trades() if t.get("id") == after),
                        None,
                    )
                    if row is not None:
                        feats = dict(row.get("entry_features") or {})
                        feats["underlying"] = uid
                        feats["dual_budget"] = budget
                        feats["dual_score"] = scores.get(uid)
                        ts.update_trade(after, {"entry_features": feats, "underlying": uid})
                except Exception:
                    pass
                self.last_message = (
                    f"ENTERED {uid} {det.get('side')} "
                    f"score={scores.get(uid):.2f} budget=₹{budget:,.0f} "
                    f"lots={cfg.get('lots_override')}"
                )
                total_open += 1
                # One fresh entry per tick — reassess next second
                break
            self.last_message = (
                f"tried {uid}: {algo.state.last_message} · next fallback"
            )
        else:
            if not self.last_message.startswith("ENTERED"):
                self.last_message = (
                    f"dual no fill · scores "
                    f"N={scores.get('NIFTY', 0):.2f} S={scores.get('SENSEX', 0):.2f} · "
                    f"{alloc.get('reason')}"
                )

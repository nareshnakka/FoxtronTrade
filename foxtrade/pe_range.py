"""
PE premium range strategy.

Detects a trading range on the ATM Put option and buys PE when:
  1. EDGE  — premium sits at the lower edge of the range (load for move to top)
  2. BREAK — premium breaks out above the upper edge (dump continuation)

Supports ascending channels and flat/horizontal ranges. Descending PE
channels are skipped for longs (falling knife).

Anti-spam: signal persist, real stop room, min-hold, persisted cooldown, day cap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from foxtrade import broker
from foxtrade import trade_store
from foxtrade.atm_channel import (
    _atm_exit_cfg,
    _long_stop,
    _reentry_blocked,
    _set_reentry_cooldown,
    _target_for_entry,
    atm_channel_should_exit,
    detect_parallel_channel,
    pick_atm_legs,
    seed_minute_ohlc,
)
from foxtrade.position_integrity import can_open_new, open_for_algo

IST = ZoneInfo("Asia/Kolkata")
ALGO_ID = "pe_range"


def pe_range_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("pe_range_enabled", False))


def detect_pe_premium_range(
    candles: list[dict[str, float]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Range on PE premium → BUY_EDGE at lower, BUY_BREAK on upside breakout.
    Flat ranges allowed (ATM channel alone rejects them as 'flat channel').
    """
    empty = {
        "ok": False,
        "bias": None,
        "signal": None,
        "side_action": None,
        "reason": "warming",
        "upper": None,
        "lower": None,
        "width": None,
        "slope": None,
        "pos_pct": None,
        "confidence": 0.0,
        "range_kind": None,
    }
    if len(candles) < 25:
        return empty

    # Softer slope floor so gentle ASC PE channels still qualify
    ch_cfg = {
        **cfg,
        "atm_channel_min_abs_slope": float(
            cfg.get("pe_range_min_abs_slope", cfg.get("atm_channel_min_abs_slope", 0.006))
        ),
        "atm_channel_edge_pct": float(
            cfg.get("pe_range_edge_pct", cfg.get("atm_channel_edge_pct", 20.0))
        ),
        "atm_channel_break_buffer_pct": float(
            cfg.get("pe_range_break_buffer_pct", cfg.get("atm_channel_break_buffer_pct", 0.35))
        ),
        "atm_channel_lookback_bars": int(
            cfg.get("pe_range_lookback_bars", cfg.get("atm_channel_lookback_bars", 90))
        ),
    }
    det = detect_parallel_channel(candles, ch_cfg)
    if det.get("ok") and det.get("bias") == "ASC":
        return {**det, "range_kind": "ASC_CHANNEL", "side": "PE"}
    if det.get("ok") and det.get("bias") == "DESC":
        return {
            **det,
            "ok": True,
            "side_action": None,
            "signal": det.get("signal"),
            "reason": "DESC PE channel — no long edge (knife)",
            "range_kind": "DESC_CHANNEL",
            "side": "PE",
        }

    # Flat / horizontal Donchian-style range on PE premium
    lookback = int(ch_cfg["atm_channel_lookback_bars"])
    window = candles[-lookback:]
    highs = [float(c["high"]) for c in window]
    lows = [float(c["low"]) for c in window]
    px = float(window[-1]["close"])
    # Exclude last 2 bars from rail build so a live breakout is not baked into the range
    rail = window[:-2] if len(window) > 10 else window
    upper = max(float(c["high"]) for c in rail)
    lower = min(float(c["low"]) for c in rail)
    width = upper - lower
    mid = (upper + lower) / 2.0
    if mid <= 0 or width <= 0:
        return {**empty, "reason": "bad flat width"}
    width_pct = 100.0 * width / mid
    min_w = float(cfg.get("pe_range_min_width_pct", 2.0))
    max_w = float(cfg.get("pe_range_max_width_pct", 28.0))
    if width_pct < min_w or width_pct > max_w:
        return {
            **empty,
            "reason": f"flat width {width_pct:.1f}%",
            "upper": round(upper, 2),
            "lower": round(lower, 2),
        }

    edge_pct = float(ch_cfg["atm_channel_edge_pct"])
    break_buf = width * (float(ch_cfg["atm_channel_break_buffer_pct"]) / 100.0) + px * 0.001
    pos_pct = 100.0 * (px - lower) / width if width else 50.0

    signal = "IN_RANGE"
    side_action = None
    reason = "PE flat range"
    if px > upper + break_buf:
        signal = "BREAK_UP"
        side_action = "BUY_BREAK"
        reason = "PE range breakout ↑ — buy put momentum"
    elif px < lower - break_buf:
        signal = "BREAK_DOWN"
        side_action = "EXIT_BREAK"
        reason = "PE range break ↓ — leave / invalid"
    elif pos_pct <= edge_pct:
        signal = "EDGE_BOTTOM"
        side_action = "BUY_EDGE"
        reason = "PE range lower edge → trail to top"
    elif pos_pct >= (100.0 - edge_pct):
        signal = "EDGE_TOP"
        side_action = "HOLD_TRAIL"
        reason = "PE range top — trail / bank"

    # Touches: how often price probed rails
    touch_band = width * 0.12
    lo_touches = sum(1 for c in rail if float(c["low"]) <= lower + touch_band)
    up_touches = sum(1 for c in rail if float(c["high"]) >= upper - touch_band)
    min_touches = int(cfg.get("pe_range_min_touches", 2))
    if lo_touches < min_touches and side_action == "BUY_EDGE":
        return {
            **empty,
            "ok": False,
            "reason": f"need lower touches {lo_touches}/{min_touches}",
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "pos_pct": round(pos_pct, 1),
        }

    conf = 0.5 + min(0.2, 0.04 * (lo_touches + up_touches))
    if side_action == "BUY_BREAK":
        conf = max(conf, 0.72)
    if side_action == "BUY_EDGE":
        conf = max(conf, 0.62)

    return {
        "ok": True,
        "bias": "FLAT",
        "signal": signal,
        "side_action": side_action,
        "reason": reason,
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "width": round(width, 2),
        "width_pct": round(width_pct, 2),
        "slope": 0.0,
        "pos_pct": round(pos_pct, 1),
        "confidence": round(min(0.95, conf), 3),
        "up_touches": up_touches,
        "lo_touches": lo_touches,
        "price": round(px, 2),
        "range_kind": "FLAT_RANGE",
        "side": "PE",
        "target_px": round(
            _target_for_entry(px, upper, width, is_break=(side_action == "BUY_BREAK"))
            if side_action in ("BUY_EDGE", "BUY_BREAK", "HOLD_TRAIL")
            else lower,
            2,
        ),
        "stop_px": round(
            lower - break_buf
            if side_action in ("BUY_EDGE", "BUY_BREAK")
            else upper + break_buf,
            2,
        ),
    }


def detect_pe_range_entry(
    pe_candles: list[dict[str, float]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Offline / backtest entry pick (PE only)."""
    empty: dict[str, Any] = {
        "allow_entry": False,
        "side": "PE",
        "event": None,
        "reason": "pe_range: no signal",
        "confidence": 0.0,
        "channel_mode": None,
        "stop_px": None,
        "target_px": None,
        "det": {},
    }
    if not pe_range_enabled(cfg) or not bool(cfg.get("pe_range_entries", True)):
        return {**empty, "reason": "pe_range: disabled"}
    det = detect_pe_premium_range(pe_candles, cfg)
    empty["det"] = det
    if not det.get("ok"):
        return {**empty, "reason": f"pe_range: {det.get('reason')}"}
    action = det.get("side_action")
    allow_edge = bool(cfg.get("pe_range_allow_edge", True))
    allow_break = bool(cfg.get("pe_range_allow_break", True))
    if action == "BUY_EDGE" and allow_edge:
        mode = "EDGE"
    elif action == "BUY_BREAK" and allow_break:
        mode = "BREAK"
    else:
        return {**empty, "reason": f"pe_range: {det.get('reason')}"}
    return {
        "allow_entry": True,
        "side": "PE",
        "event": det.get("signal"),
        "reason": det.get("reason"),
        "confidence": float(det.get("confidence") or 0),
        "channel_mode": mode,
        "stop_px": det.get("stop_px"),
        "target_px": det.get("target_px"),
        "det": det,
    }


@dataclass
class PeRangeState:
    last_message: str = "idle"
    phase: str = "SCAN"
    leg: dict[str, Any] = field(default_factory=dict)
    last_signal: dict[str, Any] = field(default_factory=dict)
    open_trade_id: str | None = None
    mode: str | None = None


class PeRangeBook:
    """ATM Put range: lower-edge load + upside breakout."""

    def __init__(self) -> None:
        self.state = PeRangeState()
        self._candles: list[dict[str, float]] = []
        self._seeded_sym: str | None = None
        self._reentry_until: dict[str, float] = {}
        self._signal_persist: dict[str, Any] = {}

    def status_line(self) -> str:
        d = self.state.last_signal or {}
        if not d:
            return self.state.last_message or "PE range idle"
        if not d.get("ok"):
            return f"PE range {d.get('reason') or '—'}"
        return (
            f"PE {d.get('range_kind') or d.get('bias')} {d.get('signal')} "
            f"pos{d.get('pos_pct')}% {d.get('side_action')} · {d.get('reason')}"
        )

    def instruments_to_watch(self) -> list[dict[str, Any]]:
        leg = self.state.leg
        if isinstance(leg, dict) and leg.get("tradingsymbol"):
            return [dict(leg)]
        return []

    def _append_candle(self, *, sym: str, token: int, quote: float | None, cfg: dict[str, Any]) -> None:
        if quote is None or quote <= 0:
            return
        now = time.time()
        minute = int(now // 60) * 60
        if not self._candles:
            self._candles = [
                {
                    "time": float(minute),
                    "open": float(quote),
                    "high": float(quote),
                    "low": float(quote),
                    "close": float(quote),
                }
            ]
            return
        last = self._candles[-1]
        if int(last["time"]) == minute:
            last["high"] = max(float(last["high"]), float(quote))
            last["low"] = min(float(last["low"]), float(quote))
            last["close"] = float(quote)
        else:
            self._candles.append(
                {
                    "time": float(minute),
                    "open": float(quote),
                    "high": float(quote),
                    "low": float(quote),
                    "close": float(quote),
                }
            )
        keep = int(cfg.get("pe_range_lookback_bars", 120)) + 30
        if len(self._candles) > keep:
            self._candles = self._candles[-keep:]
        # One-shot seed
        if self._seeded_sym != sym and token and cfg.get("pe_range_seed_history", True):
            self._seeded_sym = sym

            def _bg() -> None:
                try:
                    seeded = seed_minute_ohlc(int(token))
                    if seeded and len(seeded) > len(self._candles):
                        self._candles = seeded
                except Exception:
                    pass

            import threading

            threading.Thread(target=_bg, name="pe-range-seed", daemon=True).start()

    def on_tick(
        self,
        *,
        cfg: dict[str, Any],
        quotes: dict[str, float],
        candidates: list[dict[str, Any]],
        spot: float | None,
        option_series_fn: Callable[[str], list[dict[str, float]]],
        mode: str,
        notifications: list[dict[str, Any]] | None = None,
        pair: dict[str, Any] | None = None,
        atm_hint: float | None = None,
    ) -> None:
        if not pe_range_enabled(cfg):
            self.state.phase = "OFF"
            self.state.last_message = "pe_range disabled"
            return

        try:
            from foxtrade.learner import apply_learned_weights

            cfg = apply_learned_weights(cfg)
        except Exception:
            pass

        legs = pick_atm_legs(
            candidates, spot, cfg, pair=pair, atm_hint=atm_hint
        )
        pe = legs.get("PE")
        if not pe or not pe.get("tradingsymbol"):
            self.state.phase = "SCAN"
            self.state.last_message = "pe_range: no ATM PE"
            self.state.last_signal = {"ok": False, "reason": "no ATM PE"}
            return

        sym = str(pe["tradingsymbol"])
        token = int(pe.get("instrument_token") or 0)
        q = quotes.get(sym)
        if q is None and pe.get("ltp") is not None:
            q = float(pe["ltp"])
        self.state.leg = {
            "symbol": sym,
            "strike": pe.get("strike"),
            "ltp": q,
            "token": token,
            "expiry": pe.get("expiry"),
        }
        series = option_series_fn(sym) or []
        # Prefer live minute candles; series used only to warm if empty
        self._append_candle(sym=sym, token=token, quote=float(q) if q else None, cfg=cfg)
        candles = list(self._candles)
        if len(candles) < 20 and series:
            # Merge series closes as coarse bars if seed still loading
            for row in series[-80:]:
                try:
                    px = float(row.get("close") or row.get("ltp") or 0)
                    ts = float(row.get("time") or row.get("ts") or time.time())
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                candles.append(
                    {
                        "time": ts,
                        "open": px,
                        "high": px,
                        "low": px,
                        "close": px,
                    }
                )

        det = detect_pe_premium_range(candles, cfg)
        det["symbol"] = sym
        det["bars"] = len(candles)
        self.state.last_signal = det
        self.state.last_message = self.status_line()
        self.state.phase = "SCAN"

        self._manage_open(
            cfg=cfg,
            quotes=quotes,
            det=det,
            notifications=notifications,
        )

        if not bool(cfg.get("pe_range_entries", True)):
            return
        ok, why = can_open_new(ALGO_ID, cfg=cfg)
        if not ok:
            self.state.last_message = f"{self.status_line()} · blocked:{why[:48]}"
            return

        max_day = int(cfg.get("pe_range_max_trades_per_day", 3))
        if max_day > 0:
            today = datetime.now(IST).strftime("%Y-%m-%d")
            day_n = sum(
                1
                for t in trade_store.list_trades()
                if str(t.get("algo") or "") == ALGO_ID
                and str(t.get("opened_at") or "").startswith(today)
                and str(t.get("ledger_kind") or "") != "live_attempt"
            )
            if day_n >= max_day:
                self.state.last_message = f"{self.status_line()} · day cap {day_n}/{max_day}"
                return

        action = det.get("side_action")
        allow_edge = bool(cfg.get("pe_range_allow_edge", True))
        allow_break = bool(cfg.get("pe_range_allow_break", True))
        if action == "BUY_EDGE" and allow_edge:
            need = max(1, int(cfg.get("pe_range_edge_persist_ticks", 3)))
            mode_ch = "EDGE"
        elif action == "BUY_BREAK" and allow_break:
            need = max(1, int(cfg.get("pe_range_break_persist_ticks", 2)))
            mode_ch = "BREAK"
        else:
            self._signal_persist = {}
            return

        prev = self._signal_persist
        if prev.get("action") == action and prev.get("sym") == sym:
            ticks = int(prev.get("ticks") or 0) + 1
        else:
            ticks = 1
        self._signal_persist = {"action": action, "sym": sym, "ticks": ticks}
        if ticks < need:
            self.state.last_message = (
                f"{self.status_line()} · waiting {action} {ticks}/{need}"
            )
            return

        if time.time() < self._reentry_until.get(sym, 0) or _reentry_blocked(sym):
            self.state.last_message = f"{self.status_line()} · reentry cooldown"
            return

        self._signal_persist = {}
        self._enter(
            cfg=cfg,
            mode=mode,
            mode_ch=mode_ch,
            det=det,
            leg=pe,
            quotes=quotes,
            notifications=notifications,
        )

    def _manage_open(
        self,
        *,
        cfg: dict[str, Any],
        quotes: dict[str, float],
        det: dict[str, Any],
        notifications: list[dict[str, Any]] | None,
    ) -> None:
        opens = open_for_algo(ALGO_ID)
        if not opens:
            self.state.open_trade_id = None
            return
        lot_size = int(cfg.get("lot_size", 75))
        for trade in opens:
            tid = str(trade.get("id") or "")
            sym = str(trade.get("symbol") or "")
            px = float(quotes.get(sym) or trade.get("live_price") or trade.get("entry_price") or 0)
            if px <= 0:
                continue
            try:
                trade_store.update_trade(
                    tid,
                    {
                        "live_price": px,
                        "live_pnl_rupees": round(
                            (px - float(trade["entry_price"])) * int(trade.get("qty") or 0),
                            2,
                        ),
                    },
                )
            except Exception:
                pass
            feats = trade.get("entry_features") or {}
            target = float(feats.get("channel_target") or trade.get("target_px") or 0)
            stop = float(feats.get("channel_stop") or trade.get("stop_px") or 0)
            entry_px = float(trade.get("entry_price") or 0)
            entry_mode = str(feats.get("channel_mode") or "EDGE")
            held_min = 0.0
            opened = trade.get("opened_at")
            if opened:
                try:
                    opened_dt = datetime.fromisoformat(str(opened))
                    if opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=IST)
                    held_min = max(
                        0.0, (datetime.now(IST) - opened_dt).total_seconds() / 60.0
                    )
                except Exception:
                    held_min = 0.0
            try:
                conf = (
                    float(feats.get("confidence"))
                    if feats.get("confidence") is not None
                    else None
                )
            except (TypeError, ValueError):
                conf = None
            # PE-side pace: shorter hold / tighter trail via side=PE
            exit_cfg = {
                **_atm_exit_cfg(cfg),
                "atm_channel_min_hold_minutes": float(
                    cfg.get("pe_range_min_hold_minutes", cfg.get("atm_channel_min_hold_minutes", 5))
                ),
            }
            ch_ex = atm_channel_should_exit(
                px=px,
                entry_px=entry_px,
                peak=float(trade.get("peak_px") or px),
                stop_px=stop,
                target_px=target,
                channel_mode=entry_mode,
                side_det=det,
                cfg=exit_cfg,
                held_minutes=held_min,
                confidence=conf,
                side="PE",
            )
            if ch_ex.get("peak") is not None:
                try:
                    trade_store.update_trade(tid, {"peak_px": float(ch_ex["peak"])})
                except Exception:
                    pass
            reason = (
                str(ch_ex.get("reason") or "")
                if ch_ex.get("action") == "exit"
                else None
            )
            if reason:
                closed = broker.close_long(tid, px, reason, lot_size)
                cd_min = float(
                    cfg.get("pe_range_reentry_minutes")
                    or cfg.get("atm_channel_reentry_minutes")
                    or 20
                )
                if "leave" in reason.lower() or "SL" in reason:
                    cd_min = max(
                        cd_min,
                        float(cfg.get("pe_range_scratch_cooldown_minutes", 25)),
                    )
                cd_sec = max(60.0, cd_min * 60.0)
                self._reentry_until[sym] = time.time() + cd_sec
                _set_reentry_cooldown(sym, cd_sec)
                if closed and notifications is not None:
                    notifications.append(
                        {
                            "ts": datetime.now(IST).isoformat(),
                            "level": "info",
                            "message": f"PE range EXIT {sym}: {reason}",
                        }
                    )
                self.state.open_trade_id = None
                self.state.phase = "FLAT"
                self.state.last_message = f"exited {sym}: {reason}"

    def _enter(
        self,
        *,
        cfg: dict[str, Any],
        mode: str,
        mode_ch: str,
        det: dict[str, Any],
        leg: dict[str, Any],
        quotes: dict[str, float],
        notifications: list[dict[str, Any]] | None,
    ) -> None:
        sym = str(leg.get("tradingsymbol") or "")
        px = float(quotes.get(sym) or leg.get("ltp") or 0)
        if not sym or px <= 0:
            return
        is_break = mode_ch == "BREAK"
        base_lots = int(cfg.get("algo1_lots") or cfg.get("pe_range_lots") or 1)
        if is_break:
            lots = max(base_lots, int(cfg.get("pe_range_break_lots") or max(base_lots * 2, 2)))
            lots = min(lots, int(cfg.get("pe_range_break_max_lots", 6)))
            sl_pct = float(cfg.get("pe_range_break_sl_pct", 2.2))
        else:
            lots = max(1, int(cfg.get("pe_range_edge_lots") or base_lots))
            lots = min(lots, int(cfg.get("pe_range_edge_max_lots", 3)))
            sl_pct = float(cfg.get("pe_range_edge_sl_pct", 1.8))
        min_dist = float(cfg.get("pe_range_min_stop_dist_pct", 1.5))
        sl_pct = max(sl_pct, min_dist)
        stop = _long_stop(px, float(det.get("stop_px") or 0), sl_pct)
        target = _target_for_entry(
            px,
            float(det.get("upper") or 0),
            float(det.get("width") or 0),
            is_break=is_break,
        )
        lot_size = int(cfg.get("lot_size", 75))
        trade = broker.open_long(
            mode=mode,
            algo=ALGO_ID,
            strategy="PeRange",
            symbol=sym,
            side="PE",
            entry_price=px,
            lots=lots,
            lot_size=lot_size,
            cfg=cfg,
            allow_add=False,
            extra={
                "expiry": leg.get("expiry"),
                "strike": leg.get("strike"),
                "instrument_token": int(leg.get("instrument_token") or 0),
                "underlying": "NIFTY",
                "note": det.get("reason"),
                "leg_role": "primary",
                "stop_px": stop,
                "target_px": target,
                "peak_px": px,
                "entry_features": {
                    "tags": [
                        "pe_range",
                        det.get("range_kind"),
                        det.get("signal"),
                        mode_ch,
                    ],
                    "channel_mode": mode_ch,
                    "channel_bias": det.get("bias"),
                    "channel_signal": det.get("signal"),
                    "channel_target": target,
                    "channel_stop": stop,
                    "channel_upper": det.get("upper"),
                    "channel_lower": det.get("lower"),
                    "confidence": det.get("confidence"),
                    "range_kind": det.get("range_kind"),
                },
            },
        )
        if trade.get("status") != "OPEN":
            self.state.last_message = (
                f"pe_range rejected: {trade.get('message') or trade.get('note')}"
            )
            return
        self.state.open_trade_id = trade.get("id")
        self.state.mode = mode_ch
        self.state.phase = "IN_TRADE"
        self.state.last_message = (
            f"ENTER PE {mode_ch} {sym} @ {px:.1f} "
            f"SL {stop:.1f} TG {target:.1f} · {det.get('reason')}"
        )
        if notifications is not None:
            notifications.append(
                {
                    "ts": datetime.now(IST).isoformat(),
                    "level": "info",
                    "message": self.state.last_message,
                }
            )

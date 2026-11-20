"""Paper / Live order bridge for NFO + BFO options."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite
from foxtrade import trade_store
from foxtrade.learner import record_closed_example

IST = ZoneInfo("Asia/Kolkata")


def _weekend() -> bool:
    return datetime.now(IST).weekday() >= 5


def _await_order_fill(
    kite: Any, order_id: str, *, timeout: float = 4.0
) -> dict[str, Any]:
    """Poll Kite's order book until the order completes or the timeout expires."""
    deadline = time.time() + max(0.5, float(timeout))
    last: dict[str, Any] = {}
    first = True
    while time.time() < deadline:
        try:
            hist = kite.order_history(order_id) or []
            if hist:
                last = hist[-1] or {}
                status = str(last.get("status") or "").upper()
                filled = int(last.get("filled_quantity") or 0)
                if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                    break
                if filled > 0 and status not in (
                    "OPEN",
                    "TRIGGER PENDING",
                    "PUT ORDER REQ RECEIVED",
                ):
                    break
        except Exception as exc:  # noqa: BLE001
            last = {"status": "UNKNOWN", "status_message": str(exc)}
        # First check is immediate; then poll tightly for market fills.
        if first:
            first = False
            continue
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(0.12, remaining))
    return last


_ACTIVE_ORDER_STATUS = (
    "OPEN",
    "TRIGGER PENDING",
    "PUT ORDER REQ RECEIVED",
    "MODIFY PENDING",
    "VALIDATION PENDING",
)


def _cancel_remainder(kite: Any, order_id: str, *, timeout: float = 1.5) -> dict[str, Any]:
    """Cancel an unfilled remainder and return the final order state."""
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    except Exception:
        pass
    return _await_order_fill(kite, order_id, timeout=timeout)


def _settle_order(kite: Any, order_id: str, *, timeout: float) -> dict[str, Any]:
    """
    Wait for an order to finish; if it is still working at the timeout, cancel
    the remainder and re-read, so a retry can never place a duplicate order.
    """
    fill = _await_order_fill(kite, order_id, timeout=timeout)
    if str(fill.get("status") or "").upper() in _ACTIVE_ORDER_STATUS:
        fill = _cancel_remainder(kite, order_id)
    return fill


def _opt_exchange_name(symbol: str, extra: dict[str, Any] | None = None, trade: dict[str, Any] | None = None) -> str:
    """Nifty weeklies → NFO; Sensex weeklies → BFO."""
    und = ""
    if extra:
        und = str(extra.get("underlying") or "")
    if not und and trade:
        und = str(
            trade.get("underlying")
            or (trade.get("entry_features") or {}).get("underlying")
            or ""
        )
    sym = str(symbol or "").upper()
    if und.upper() == "SENSEX" or sym.startswith("SENSEX"):
        return "BFO"
    return "NFO"


def _kite_exchange(kite: Any, exchange_name: str) -> str:
    if exchange_name == "BFO":
        return getattr(kite, "EXCHANGE_BFO", "BFO")
    return kite.EXCHANGE_NFO


def open_long(
    *,
    mode: str,
    algo: str,
    strategy: str,
    symbol: str,
    side: str,
    entry_price: float,
    lots: int,
    lot_size: int,
    extra: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    allow_add: bool = False,
) -> dict[str, Any]:
    from foxtrade.scanner import reject_same_day_expiry
    from foxtrade.position_integrity import reject_duplicate_open
    from foxtrade.config_store import load_config

    # Absolute hard stop: no 0DTE, and on expiry day no skip-week (next-next week)
    block = reject_same_day_expiry(
        symbol=symbol,
        expiry=(extra or {}).get("expiry") if extra else None,
    )
    if block:
        return {
            "id": None,
            "status": "REJECTED",
            "algo": algo,
            "mode": mode,
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "broker_status": "blocked_bad_expiry",
            "note": block,
            "message": block,
        }

    # No fresh entry over unsquared OPEN (restart-safe; trades.json is source of truth)
    if not allow_add:
        dup = reject_duplicate_open(algo, cfg=cfg or load_config())
        if dup:
            return {
                "id": None,
                "status": "REJECTED",
                "algo": algo,
                "mode": mode,
                "strategy": strategy,
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "broker_status": "blocked_open_exists",
                "note": dup,
                "message": dup,
            }

    qty = lots * lot_size
    payload: dict[str, Any] = {
        "algo": algo,
        "mode": mode,
        "strategy": strategy,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "lots": lots,
        "qty": qty,
        "live_price": entry_price,
        "live_pnl_rupees": 0.0,
        "order_id": None,
        "broker_status": "paper",
    }
    if extra:
        payload.update(extra)

    live_fail_diag: dict[str, Any] | None = None
    live_fail_status = ""
    live_exch: str | None = None
    live_filled_qty = 0
    signal_price = float(entry_price or 0)

    # Scanner LTPs can be 60-90s old by the time a signal fires. Refresh the
    # quote, refuse to chase an extended move, and fill paper at the real price.
    if signal_price > 0 and bool((cfg or {}).get("realtime_parity", True)):
        try:
            fresh_px = _market_ltp(symbol, fallback=None, extra=extra)
        except Exception:
            fresh_px = None
        if fresh_px and fresh_px > 0:
            max_slip_pct = float(
                (cfg or {}).get("entry_max_slippage_pct", 2.0) or 0
            ) / 100.0
            if max_slip_pct > 0 and fresh_px > signal_price * (1.0 + max_slip_pct):
                msg = (
                    f"chase blocked: {symbol} signal {signal_price:.2f} → "
                    f"{fresh_px:.2f} (+{(fresh_px/signal_price-1)*100:.1f}%)"
                )
                return {
                    "id": None,
                    "status": "REJECTED",
                    "algo": algo,
                    "mode": mode,
                    "strategy": strategy,
                    "symbol": symbol,
                    "side": side,
                    "entry_price": signal_price,
                    "broker_status": "blocked_chase",
                    "note": msg,
                    "message": msg,
                }
            if mode != "live":
                entry_price = float(fresh_px)
                payload["entry_price"] = entry_price
                payload["live_price"] = entry_price
                payload["signal_price"] = round(signal_price, 2)

    if mode == "live":
        if _weekend():
            # Keep paper path alive on weekends so dual book / dry-runs still ledger
            live_fail_diag = diagnose_broker_error("weekend market closed")
            live_fail_status = "live_skipped_weekend_paper_kept"
            payload["mode"] = "paper"
            payload["broker_status"] = live_fail_status
            payload["broker_confirmed"] = False
            payload["live_attempted"] = True
            payload["live_buy_diagnosis"] = live_fail_diag
            note = str(payload.get("note") or "")
            payload["note"] = (
                f"{note} | LIVE skipped weekend — paper kept".strip(" |")
            )
        else:
            try:
                kite = get_authenticated_kite()
                live_exch = _opt_exchange_name(symbol, extra=extra)
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=_kite_exchange(kite, live_exch),
                    tradingsymbol=symbol,
                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    product=kite.PRODUCT_MIS,
                    order_type=kite.ORDER_TYPE_MARKET,
                    tag=f"FOX{algo[-1]}",
                )
                fill = _settle_order(
                    kite,
                    str(order_id),
                    timeout=float((cfg or {}).get("live_fill_timeout_sec", 2.5) or 2.5),
                )
                filled = int(fill.get("filled_quantity") or 0)
                avg = float(fill.get("average_price") or 0)
                fstatus = str(fill.get("status") or "").upper()
                if filled <= 0 or avg <= 0:
                    raise RuntimeError(
                        f"no confirmed fill (status={fstatus or 'unknown'}"
                        f"{', ' + str(fill.get('status_message')) if fill.get('status_message') else ''})"
                    )
                live_filled_qty = filled
                payload["order_id"] = str(order_id)
                payload["broker_status"] = "live_bought"
                payload["broker_confirmed"] = True
                payload["live_attempted"] = True
                payload["ledger"] = "live"
                payload["exchange"] = live_exch
                payload["signal_price"] = round(signal_price, 2)
                payload["entry_price"] = round(avg, 2)
                payload["fill_average_price"] = round(avg, 2)
                payload["peak_px"] = round(avg, 2)
                if filled != qty:
                    payload["qty"] = filled
                    payload["lots"] = max(1, filled // max(1, lot_size))
                    payload["partial_fill"] = True
            except Exception as exc:  # noqa: BLE001
                live_fail_diag = diagnose_broker_error(exc)
                live_fail_status = f"live_buy_failed:{exc}"
                live_exch = _opt_exchange_name(symbol, extra=extra)
                policy = str((cfg or {}).get("live_fail_policy") or "reject").lower()
                msg = f"LIVE BUY failed — {exc}"
                if policy == "paper":
                    # Explicit opt-in only: never silently fabricate a fill.
                    payload["mode"] = "paper"
                    payload["broker_status"] = f"live_buy_failed_paper_kept:{exc}"
                    payload["broker_confirmed"] = False
                    payload["live_attempted"] = True
                    payload["live_buy_diagnosis"] = live_fail_diag
                    payload["exchange"] = live_exch
                    note = str(payload.get("note") or "")
                    tip = (live_fail_diag.get("tips") or [None])[0]
                    payload["note"] = (
                        f"{note} | LIVE BUY failed — paper kept"
                        + (f" · {tip}" if tip else "")
                    ).strip(" |")
                    payload["message"] = f"LIVE BUY failed — paper fill kept: {exc}"
                else:
                    trade_store.record_live_attempt(
                        paper_trade={
                            "algo": algo,
                            "strategy": strategy,
                            "symbol": symbol,
                            "side": side,
                            "underlying": (extra or {}).get("underlying"),
                            "entry_price": signal_price,
                            "lots": lots,
                            "qty": qty,
                        },
                        reason=msg,
                        diagnosis=live_fail_diag,
                        broker_status=live_fail_status,
                        exchange=live_exch,
                    )
                    return {
                        "id": None,
                        "status": "REJECTED",
                        "algo": algo,
                        "mode": mode,
                        "strategy": strategy,
                        "symbol": symbol,
                        "side": side,
                        "entry_price": signal_price,
                        "broker_status": live_fail_status,
                        "note": msg,
                        "message": msg,
                        "diagnosis": live_fail_diag,
                    }

    trade = trade_store.open_trade(payload, allow_add=allow_add)
    # A confirmed live fill that could not be booked must be squared immediately.
    if live_filled_qty > 0 and not trade.get("id"):
        try:
            kite = get_authenticated_kite()
            kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=_kite_exchange(kite, live_exch or "NFO"),
                tradingsymbol=symbol,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=live_filled_qty,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
                tag=f"FOX{algo[-1]}R",
            )
        except Exception:
            pass
        return {
            "id": None,
            "status": "REJECTED",
            "algo": algo,
            "mode": mode,
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "entry_price": payload.get("entry_price"),
            "broker_status": "live_fill_unbooked_squared",
            "note": "live fill could not be booked — emergency sell sent",
            "message": "live fill could not be booked — emergency sell sent",
        }
    # Mirror failed real attempts into Live ledger so Reports → Live only shows them
    if trade.get("id") and live_fail_diag is not None:
        try:
            trade_store.record_live_attempt(
                paper_trade=trade,
                reason=str(payload.get("note") or live_fail_status),
                diagnosis=live_fail_diag,
                broker_status=live_fail_status,
                exchange=live_exch,
            )
        except Exception:
            pass
    return trade


def diagnose_broker_error(exc: Any) -> dict[str, Any]:
    """Root-cause hints for Kite order failures (buy/sell/square)."""
    msg = str(exc or "").lower()
    tips: list[str] = []
    code = "unknown"
    if "token" in msg or "session" in msg or "login" in msg or "api_key" in msg:
        code = "auth"
        tips += ["Kite session expired — login again / check TOTP auto-login", "Verify access_token.json is today's"]
    elif "margin" in msg or "insufficient" in msg or "funds" in msg:
        code = "margin"
        tips += ["Insufficient funds/margin on Zerodha", "Reduce lots or free cash"]
    elif "market" in msg and ("clos" in msg or "not open" in msg):
        code = "market_hours"
        tips += ["Market closed or pre-open — wait for cash session"]
    elif "reject" in msg or "invalid" in msg or "does not exist" in msg:
        code = "order_reject"
        tips += ["Symbol/expiry may be wrong or illiquid", "Check NFO tradingsymbol on Kite"]
    elif "network" in msg or "timeout" in msg or "connection" in msg or "resolve" in msg:
        code = "network"
        tips += ["Network/API timeout — retry square-off", "Check internet / Zerodha status"]
    elif "weekend" in msg:
        code = "weekend"
        tips += ["No live NFO orders on weekends"]
    else:
        tips += ["Check Kite order book for pending/rejected", "Retry square-off from dashboard"]
    return {"code": code, "error": str(exc), "tips": tips}


def close_long(
    trade_id: str,
    exit_price: float,
    reason: str,
    lot_size: int,
) -> dict[str, Any] | None:
    rows = trade_store.list_trades()
    trade = next((t for t in rows if t["id"] == trade_id and t.get("status") == "OPEN"), None)
    if not trade:
        return None

    # Live square-off: any live-bought leg (or still has entry order_id) gets a Kite MARKET SELL
    broker_st = str(trade.get("broker_status") or "")
    needs_live_exit = trade.get("mode") == "live" and (
        broker_st == "live_bought"
        or broker_st.startswith("live_bought")
        or bool(trade.get("order_id"))
    )
    exit_diag: dict[str, Any] | None = None
    broker_confirmed = False
    if needs_live_exit and not _weekend():
        # Do not spam orders; retry every few seconds while the position is open.
        last_ts = float(trade.get("last_exit_attempt_ts") or 0)
        # Allow fast retries — market exits should re-fire within ~1s, not 5s.
        if last_ts and time.time() - last_ts < 0.8:
            return None
        cfg: dict[str, Any] = {}
        try:
            from foxtrade.config_store import load_config

            cfg = load_config()
        except Exception:
            pass
        kite = None
        try:
            kite = get_authenticated_kite()
            exch = _opt_exchange_name(str(trade.get("symbol") or ""), trade=trade)
            qty_total = int(trade.get("qty") or 0)
            already = int(trade.get("exit_filled_qty") or 0)
            notional = float(trade.get("exit_notional") or 0.0)
            remaining = max(0, qty_total - already)
            if remaining <= 0:
                exit_price = (notional / already) if already else float(exit_price)
                broker_confirmed = True
            else:
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=_kite_exchange(kite, exch),
                    tradingsymbol=trade["symbol"],
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=remaining,
                    product=kite.PRODUCT_MIS,
                    order_type=kite.ORDER_TYPE_MARKET,
                    tag=f"FOX{str(trade.get('algo'))[-1]}X",
                )
                fill = _settle_order(
                    kite,
                    str(order_id),
                    timeout=float(cfg.get("live_fill_timeout_sec", 2.5) or 2.5),
                )
                filled = int(fill.get("filled_quantity") or 0)
                avg = float(fill.get("average_price") or 0)
                fstatus = str(fill.get("status") or "").upper()
                if filled <= 0 or avg <= 0:
                    raise RuntimeError(
                        f"no confirmed exit fill (status={fstatus or 'unknown'})"
                    )
                already += filled
                notional += avg * filled
                trade_store.update_trade(
                    trade_id,
                    {
                        "exit_order_id": str(order_id),
                        "exit_filled_qty": already,
                        "exit_notional": round(notional, 2),
                        "last_exit_attempt_ts": time.time(),
                    },
                )
                if already < qty_total:
                    # Partial exit: keep the row OPEN and sell the remainder next tick.
                    trade_store.update_trade(
                        trade_id,
                        {
                            "broker_status": f"live_exit_partial:{already}/{qty_total}",
                            "broker_confirmed": False,
                        },
                    )
                    return None
                exit_price = notional / already if already else float(exit_price)
                trade_store.update_trade(
                    trade_id,
                    {
                        "broker_status": "live_sold",
                        "broker_confirmed": True,
                        "exit_filled_qty": already,
                        "exit_notional": round(notional, 2),
                    },
                )
                broker_confirmed = True
        except Exception as exc:  # noqa: BLE001
            exit_diag = diagnose_broker_error(exc)
            # If the broker already shows us flat (manual exit / prior retry),
            # close the ledger at LTP instead of retrying a non-existent position.
            flat_at_broker = False
            if kite is not None:
                try:
                    net_rows = (kite.positions() or {}).get("net") or []
                    net_qty = sum(
                        int(p.get("quantity") or 0)
                        for p in net_rows
                        if p.get("tradingsymbol") == trade.get("symbol")
                    )
                    flat_at_broker = net_qty <= 0
                except Exception:
                    flat_at_broker = False
            trade_store.update_trade(
                trade_id,
                {
                    "broker_status": (
                        "live_exit_flat_at_broker"
                        if flat_at_broker
                        else f"live_exit_failed:{exc}"
                    ),
                    "broker_confirmed": False,
                    "exit_diagnosis": exit_diag,
                    "exit_attempts": int(trade.get("exit_attempts") or 0) + 1,
                    "last_exit_attempt_ts": time.time(),
                },
            )
            if not flat_at_broker:
                return None
            reason = f"{reason} | broker flat — booked @LTP"
    elif needs_live_exit and _weekend():
        exit_diag = diagnose_broker_error("weekend")
        trade_store.update_trade(
            trade_id,
            {
                "broker_status": "live_exit_skipped_weekend",
                "broker_confirmed": False,
                "exit_diagnosis": exit_diag,
                "last_exit_attempt_ts": time.time(),
            },
        )
        # Never mark a live position flat when the exchange cannot sell it.
        return None

    closed = trade_store.close_trade(trade_id, exit_price, reason, lot_size)
    if closed:
        if exit_diag is not None:
            trade_store.update_trade(
                trade_id,
                {
                    "exit_diagnosis": exit_diag,
                    "broker_confirmed": broker_confirmed,
                    "pnl_booked_without_broker_confirm": not broker_confirmed,
                },
            )
            closed = {**closed, "exit_diagnosis": exit_diag, "broker_confirmed": broker_confirmed}
        try:
            record_closed_example(closed)
        except Exception:
            pass
    return closed


def _ltp_with_timeout(kite: Any, keys: list[str], *, timeout: float = 2.5) -> dict[str, Any]:
    """Kite LTP with a hard timeout — a hung socket must not freeze the engine."""
    box: dict[str, Any] = {"q": {}, "err": None}

    def _call() -> None:
        try:
            box["q"] = kite.ltp(keys) or {}
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    th = threading.Thread(target=_call, name="broker-ltp", daemon=True)
    th.start()
    th.join(timeout=max(0.5, float(timeout)))
    if th.is_alive():
        raise RuntimeError("ltp timeout")
    if box["err"] is not None:
        raise box["err"]
    return box["q"]


def _hub_ltp(symbol: str, *, max_age_sec: float = 1.25) -> float | None:
    """Use in-memory market-hub quote when fresh — avoids an extra Kite round-trip on fire."""
    try:
        from foxtrade.engine import ENGINE

        m = getattr(ENGINE, "market", None)
        if m is None:
            return None
        px = (getattr(m, "last_quotes", {}) or {}).get(symbol)
        ts = float((getattr(m, "quote_ts", {}) or {}).get(symbol) or 0)
        if px is None or ts <= 0:
            return None
        if (time.time() - ts) > float(max_age_sec):
            return None
        return float(px)
    except Exception:
        return None


def _market_ltp(
    symbol: str,
    fallback: float | None = None,
    *,
    trade: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> float:
    hub = _hub_ltp(symbol)
    if hub is not None and hub > 0:
        return hub
    try:
        kite = get_authenticated_kite()
        exch = _opt_exchange_name(symbol, extra=extra, trade=trade)
        key = f"{exch}:{symbol}"
        data = _ltp_with_timeout(kite, [key], timeout=1.2).get(key) or {}
        px = data.get("last_price")
        if px is not None:
            return float(px)
        # Fallback opposite venue if symbol routing was wrong
        alt = "NFO" if exch == "BFO" else "BFO"
        alt_key = f"{alt}:{symbol}"
        data = _ltp_with_timeout(kite, [alt_key], timeout=1.0).get(alt_key) or {}
        px = data.get("last_price")
        if px is not None:
            return float(px)
    except Exception:
        pass
    if fallback is not None:
        return float(fallback)
    raise RuntimeError(f"No LTP for {symbol}")


def reconcile_open_with_broker(*, min_age_sec: float = 120.0) -> list[dict[str, Any]]:
    """
    Close ledger rows whose live leg no longer exists at Zerodha.

    A manual square-off or broker auto-square while the engine was down leaves
    a phantom OPEN row, which blocks every new entry (duplicate guard) until
    someone squares it again. Only reconciles rows we sent to the broker and
    that are older than min_age_sec, and only when positions() says flat.
    """
    rows = trade_store.open_trades()
    candidates = [
        t
        for t in rows
        if t.get("mode") == "live"
        and (
            bool(t.get("order_id"))
            or str(t.get("broker_status") or "").startswith("live")
        )
    ]
    if not candidates:
        return []
    def _snapshot() -> dict[str, int] | None:
        try:
            kite = get_authenticated_kite()
            net = (kite.positions() or {}).get("net")
            if not isinstance(net, list):
                return None
        except Exception:
            return None
        out: dict[str, int] = {}
        for p in net:
            sym = str(p.get("tradingsymbol") or "")
            if sym:
                out[sym] = out.get(sym, 0) + int(p.get("quantity") or 0)
        return out

    first = _snapshot()
    if first is None:
        return []
    # Two confirmations: a single empty snapshot must never orphan a real fill.
    time.sleep(0.4)
    second = _snapshot()
    if second is None:
        return []
    held: dict[str, int] = {}
    for sym in set(first) | set(second):
        held[sym] = max(abs(first.get(sym, 0)), abs(second.get(sym, 0)))
    now = datetime.now(IST)
    fixed: list[dict[str, Any]] = []
    for t in candidates:
        try:
            opened = datetime.fromisoformat(
                str(t.get("opened_at") or "").replace("Z", "+00:00")
            )
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=IST)
            age = (now - opened.astimezone(IST)).total_seconds()
        except Exception:
            age = 0.0
        if age < float(min_age_sec):
            continue
        sym = str(t.get("symbol") or "")
        if not sym or held.get(sym, 0) != 0:
            continue
        fb = t.get("live_price") or t.get("entry_price")
        try:
            px = _market_ltp(sym, float(fb) if fb is not None else None, trade=t)
        except Exception:
            continue
        lot_size = max(
            1, int(t.get("qty") or 0) // max(1, int(t.get("lots") or 1))
        ) or 75
        closed = trade_store.close_trade(
            str(t["id"]), float(px), "broker flat — startup reconcile", lot_size
        )
        if not closed:
            continue
        trade_store.update_trade(
            str(t["id"]),
            {
                "broker_status": "live_flat_reconciled",
                "broker_confirmed": False,
                "pnl_booked_without_broker_confirm": True,
            },
        )
        fixed.append(closed)
    return fixed


def square_off_market(
    trade_id: str,
    *,
    reason: str = "manual square-off",
    lot_size: int | None = None,
) -> dict[str, Any]:
    """Square one OPEN trade at market. Skips CLOSED / missing ids."""
    rows = trade_store.list_trades()
    trade = next((t for t in rows if t["id"] == trade_id), None)
    if not trade:
        return {"ok": False, "id": trade_id, "error": "trade not found"}
    if trade.get("status") != "OPEN":
        return {"ok": False, "id": trade_id, "error": "not open — skipped", "status": trade.get("status")}

    sym = trade.get("symbol") or ""
    fallback = trade.get("live_price") or trade.get("entry_price")
    try:
        px = _market_ltp(
            sym,
            float(fallback) if fallback is not None else None,
            trade=trade,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "id": trade_id, "error": f"ltp: {exc}"}

    ls = int(lot_size or trade.get("qty") or 75)
    # lot_size arg to close_trade is unused for qty (qty already on trade) but kept for API
    if trade.get("lots") and trade.get("qty"):
        ls = max(1, int(trade["qty"]) // max(1, int(trade["lots"])))

    closed = close_long(trade_id, px, reason, ls)
    if not closed:
        return {
            "ok": False,
            "id": trade_id,
            "error": "close failed",
            "diagnosis": diagnose_broker_error("close_trade returned None"),
        }
    # Refresh after close for diagnosis fields
    row = next((t for t in trade_store.list_trades() if t.get("id") == trade_id), closed)
    return {
        "ok": True,
        "id": trade_id,
        "symbol": sym,
        "exit_price": px,
        "mode": row.get("mode"),
        "broker_status": row.get("broker_status"),
        "broker_confirmed": bool(row.get("broker_confirmed")),
        "pnl_booked_without_broker_confirm": bool(row.get("pnl_booked_without_broker_confirm")),
        "diagnosis": row.get("exit_diagnosis"),
        "pnl_rupees": row.get("pnl_rupees"),
        "net_pnl_rupees": row.get("net_pnl_rupees"),
        "invested": row.get("invested"),
        "lots": row.get("lots"),
    }


def square_off_many(
    trade_ids: list[str],
    *,
    reason: str = "manual square-off",
    lot_size: int | None = None,
) -> dict[str, Any]:
    """Square multiple trades; only OPEN legs are touched."""
    results = [square_off_market(tid, reason=reason, lot_size=lot_size) for tid in trade_ids]
    return {
        "ok": all(r.get("ok") for r in results) if results else True,
        "squared": [r for r in results if r.get("ok")],
        "skipped": [r for r in results if not r.get("ok")],
        "count_ok": sum(1 for r in results if r.get("ok")),
        "count_skip": sum(1 for r in results if not r.get("ok")),
    }

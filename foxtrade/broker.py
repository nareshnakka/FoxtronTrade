"""Paper / Live order bridge for NFO + BFO options."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite
from foxtrade import trade_store
from foxtrade.learner import record_closed_example

IST = ZoneInfo("Asia/Kolkata")


def _weekend() -> bool:
    return datetime.now(IST).weekday() >= 5


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

    if mode == "live":
        if _weekend():
            # Keep paper path alive on weekends so dual book / dry-runs still ledger
            payload["mode"] = "paper"
            payload["broker_status"] = "live_skipped_weekend_paper_kept"
            payload["broker_confirmed"] = False
            payload["live_attempted"] = True
            payload["live_buy_diagnosis"] = diagnose_broker_error("weekend market closed")
            note = str(payload.get("note") or "")
            payload["note"] = (
                f"{note} | LIVE skipped weekend — paper kept".strip(" |")
            )
        else:
            try:
                kite = get_authenticated_kite()
                exch = _opt_exchange_name(symbol, extra=extra)
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=_kite_exchange(kite, exch),
                    tradingsymbol=symbol,
                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    product=kite.PRODUCT_MIS,
                    order_type=kite.ORDER_TYPE_MARKET,
                    tag=f"FOX{algo[-1]}",
                )
                payload["order_id"] = str(order_id)
                payload["broker_status"] = "live_bought"
                payload["broker_confirmed"] = True
                payload["live_attempted"] = True
                payload["exchange"] = exch
            except Exception as exc:  # noqa: BLE001
                # Live may fail (margin/auth/reject) — still book PAPER so algo
                # keeps managing + dashboard retains paper + live ledgers.
                diag = diagnose_broker_error(exc)
                payload["mode"] = "paper"
                payload["broker_status"] = f"live_buy_failed_paper_kept:{exc}"
                payload["broker_confirmed"] = False
                payload["live_attempted"] = True
                payload["live_buy_diagnosis"] = diag
                note = str(payload.get("note") or "")
                tip = (diag.get("tips") or [None])[0]
                payload["note"] = (
                    f"{note} | LIVE BUY failed — paper kept"
                    + (f" · {tip}" if tip else "")
                ).strip(" |")
                payload["message"] = (
                    f"LIVE BUY failed — paper fill kept: {exc}"
                )

    return trade_store.open_trade(payload, allow_add=allow_add)


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
        try:
            kite = get_authenticated_kite()
            exch = _opt_exchange_name(str(trade.get("symbol") or ""), trade=trade)
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=_kite_exchange(kite, exch),
                tradingsymbol=trade["symbol"],
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=int(trade["qty"]),
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
                tag=f"FOX{str(trade.get('algo'))[-1]}X",
            )
            trade_store.update_trade(
                trade_id,
                {
                    "exit_order_id": str(order_id),
                    "broker_status": "live_sold",
                    "broker_confirmed": True,
                },
            )
            broker_confirmed = True
        except Exception as exc:  # noqa: BLE001
            exit_diag = diagnose_broker_error(exc)
            # Still book P/L from algo/LTP so ledger is not stuck OPEN forever
            trade_store.update_trade(
                trade_id,
                {
                    "broker_status": f"live_exit_failed:{exc}",
                    "broker_confirmed": False,
                    "exit_diagnosis": exit_diag,
                    "pnl_booked_without_broker_confirm": True,
                },
            )
            reason = f"{reason} | LIVE SELL failed — booked @LTP · {exit_diag.get('code')}"
    elif needs_live_exit and _weekend():
        exit_diag = diagnose_broker_error("weekend")
        trade_store.update_trade(
            trade_id,
            {
                "broker_status": "live_exit_skipped_weekend",
                "broker_confirmed": False,
                "exit_diagnosis": exit_diag,
                "pnl_booked_without_broker_confirm": True,
            },
        )
        reason = f"{reason} | weekend — booked @LTP without Kite SELL"

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


def _market_ltp(
    symbol: str,
    fallback: float | None = None,
    *,
    trade: dict[str, Any] | None = None,
) -> float:
    try:
        kite = get_authenticated_kite()
        exch = _opt_exchange_name(symbol, trade=trade)
        key = f"{exch}:{symbol}"
        data = kite.ltp([key]).get(key) or {}
        px = data.get("last_price")
        if px is not None:
            return float(px)
        # Fallback opposite venue if symbol routing was wrong
        alt = "NFO" if exch == "BFO" else "BFO"
        alt_key = f"{alt}:{symbol}"
        data = kite.ltp([alt_key]).get(alt_key) or {}
        px = data.get("last_price")
        if px is not None:
            return float(px)
    except Exception:
        pass
    if fallback is not None:
        return float(fallback)
    raise RuntimeError(f"No LTP for {symbol}")


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

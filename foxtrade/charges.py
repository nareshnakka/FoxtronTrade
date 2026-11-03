"""Estimate Zerodha-style NFO options charges (buy + sell round trip)."""

from __future__ import annotations

from typing import Any


# Approx. Zerodha Equity Options (NFO) schedule — overridable via config "charges"
_DEFAULT_RATES: dict[str, float] = {
    "brokerage_per_order": 20.0,
    "brokerage_pct_cap": 0.0003,  # 0.03%
    "exchange_pct": 0.0003503,  # 0.03503% of premium turnover
    "sebi_pct": 0.000001,  # ₹10 / crore
    "stt_sell_pct": 0.001,  # 0.1% on sell premium
    "stamp_buy_pct": 0.00003,  # 0.003% on buy premium
    "gst_pct": 0.18,
}


def _rates(cfg: dict[str, Any] | None = None) -> dict[str, float]:
    out = dict(_DEFAULT_RATES)
    if cfg and isinstance(cfg.get("charges"), dict):
        for k, v in cfg["charges"].items():
            if k in out:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass
    return out


def _one_side(turnover: float, *, is_sell: bool, rates: dict[str, float]) -> dict[str, float]:
    turnover = max(0.0, float(turnover))
    # Zerodha FO: ₹20 per order OR 0.03% of turnover — whichever is lower
    brokerage = (
        min(rates["brokerage_per_order"], turnover * rates["brokerage_pct_cap"])
        if turnover > 0
        else 0.0
    )
    exchange = turnover * rates["exchange_pct"]
    sebi = turnover * rates["sebi_pct"]
    stt = turnover * rates["stt_sell_pct"] if is_sell else 0.0
    stamp = turnover * rates["stamp_buy_pct"] if not is_sell else 0.0
    gst = rates["gst_pct"] * (brokerage + exchange + sebi)
    total = brokerage + exchange + sebi + stt + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "stt": round(stt, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def estimate_option_charges(
    entry_price: float,
    exit_price: float,
    qty: int,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Round-trip charges for one long options leg (buy then sell)."""
    rates = _rates(cfg)
    qty = max(0, int(qty))
    buy_turn = float(entry_price) * qty
    sell_turn = float(exit_price) * qty
    buy = _one_side(buy_turn, is_sell=False, rates=rates)
    sell = _one_side(sell_turn, is_sell=True, rates=rates)
    total = round(buy["total"] + sell["total"], 2)
    return {
        "charges": total,
        "buy": buy,
        "sell": sell,
        "buy_turnover": round(buy_turn, 2),
        "sell_turnover": round(sell_turn, 2),
    }


def trade_charges(trade: dict[str, Any], cfg: dict[str, Any] | None = None) -> float:
    """Charges for a trade row — stored value, or estimate from prices."""
    if trade.get("charges") is not None:
        try:
            return round(float(trade["charges"]), 2)
        except (TypeError, ValueError):
            pass
    entry = float(trade.get("entry_price") or 0)
    exit_px = trade.get("exit_price")
    if exit_px is None:
        exit_px = trade.get("live_price") or entry
    qty = int(trade.get("qty") or 0)
    if entry <= 0 or qty <= 0:
        return 0.0
    return estimate_option_charges(entry, float(exit_px), qty, cfg)["charges"]

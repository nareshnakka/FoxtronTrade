"""
Keep ALL open trades watched + MTM-updated across restarts.

Algos restore their latest setup for full strategy logic; this guard
manages any leftover OPEN legs the in-memory algo is not tracking
(so a code restart never abandons a position).
"""

from __future__ import annotations

from typing import Any

from foxtrade import broker, trade_store
from foxtrade.pair_regime import manage_long_decision

_token_cache: dict[str, int] = {}


def _nfo_token_map() -> dict[str, int]:
    """Cache NIFTY option tradingsymbol → instrument_token for the day."""
    global _token_cache
    if _token_cache:
        return _token_cache
    try:
        from foxtrade.kite_cache import get_instruments

        inst = get_instruments("NFO")
        _token_cache = {
            str(i["tradingsymbol"]): int(i["instrument_token"])
            for i in inst
            if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")
        }
    except Exception:
        return _token_cache
    return _token_cache


def resolve_instrument_token(symbol: str, existing: Any = None) -> int:
    tok = int(existing or 0)
    if tok > 0:
        return tok
    if not symbol:
        return 0
    return int(_nfo_token_map().get(symbol) or 0)


def backfill_open_trade_tokens() -> int:
    """Persist missing instrument_token on OPEN trades (needed after restart / old entries)."""
    n = 0
    for t in trade_store.open_trades():
        sym = t.get("symbol")
        if not sym or int(t.get("instrument_token") or 0) > 0:
            continue
        tok = resolve_instrument_token(sym)
        if tok > 0:
            trade_store.update_trade(t["id"], {"instrument_token": tok})
            n += 1
    return n


def open_watch_instruments() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in trade_store.open_trades():
        sym = t.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        tok = resolve_instrument_token(sym, t.get("instrument_token"))
        if tok > 0 and int(t.get("instrument_token") or 0) <= 0:
            trade_store.update_trade(t["id"], {"instrument_token": tok})
        out.append(
            {
                "tradingsymbol": sym,
                "instrument_token": tok,
                "exchange": "NFO",
                "strike": t.get("strike"),
                "expiry": t.get("expiry"),
                "instrument_type": t.get("side"),
            }
        )
    return out


def _qty(t: dict[str, Any], cfg: dict[str, Any]) -> int:
    if t.get("qty"):
        return int(t["qty"])
    lots = int(t.get("lots") or 1)
    return lots * int(cfg.get("lot_size", 75))


def update_mtm(quotes: dict[str, float], cfg: dict[str, Any]) -> None:
    for t in trade_store.open_trades():
        sym = t.get("symbol")
        if not sym:
            continue
        px = quotes.get(sym)
        if px is None:
            continue
        entry = float(t.get("entry_price") or 0)
        qty = _qty(t, cfg)
        pnl = (px - entry) * qty if entry else 0.0
        mult = (px / entry) if entry else 1.0
        trade_store.update_trade(
            t["id"],
            {
                "live_price": px,
                "live_pnl_rupees": round(pnl, 2),
                "live_mult": round(mult, 2),
            },
        )


def manage_orphans(
    quotes: dict[str, float],
    cfg: dict[str, Any],
    managed_ids: set[str],
    pair_regime: Any = None,
) -> list[str]:
    """Exit OPEN trades not owned by live algo state (target / stop).

    Strategy roles (fallen / breakout / dual / primary) are never orphan-exited —
    they must be restored into algo state or squared manually. Orphans only cover
    truly detached legs with unknown/missing roles.
    """
    closed: list[str] = []
    lot_size = int(cfg.get("lot_size", 75))
    protected_roles = {
        "fallen",
        "breakout",
        "dual",
        "primary",
        "breakout_closed",
    }
    for t in trade_store.open_trades():
        tid = t.get("id")
        if not tid or tid in managed_ids:
            continue
        role = str(t.get("leg_role") or "")
        if role in protected_roles:
            continue
        sym = t.get("symbol")
        px = quotes.get(sym or "")
        if px is None:
            continue
        entry = float(t.get("entry_price") or 0)
        if entry <= 0:
            continue
        side = t.get("side") or ("CE" if str(sym).endswith("CE") else "PE")
        reason = None
        if pair_regime is not None:
            reason = manage_long_decision(
                side=side, entry=entry, px=px, regime=pair_regime, cfg=cfg
            )
        if reason is None:
            if px >= float(cfg.get("target_high", cfg.get("target_low", 169))):
                reason = "orphan target"
            elif px <= float(cfg.get("stop_loss", 120)):
                reason = f"orphan stop {cfg.get('stop_loss', 120)}"
        if reason:
            broker.close_long(tid, px, reason, lot_size)
            closed.append(tid)
    return closed

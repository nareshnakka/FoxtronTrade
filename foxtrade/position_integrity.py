"""
Hard position integrity — no memory loss, no cheat flat, no fresh entries over opens.

Rules:
  1. OPEN rows in data/trades.json are source of truth across restarts.
  2. In-memory algo state must re-bind to those rows every tick.
  3. New entries are banned while any OPEN exists for that algo (until squared).
  4. Never delete/wipe OPEN without going through close_trade / square_off.
"""

from __future__ import annotations

from typing import Any

from foxtrade import trade_store


def open_for_algo(algo: str) -> list[dict[str, Any]]:
    return [
        t
        for t in trade_store.open_trades()
        if str(t.get("algo") or "") == str(algo) and t.get("status") == "OPEN"
    ]


def any_open(*, algo: str | None = None) -> bool:
    if algo:
        return bool(open_for_algo(algo))
    return bool(trade_store.open_trades())


def can_open_new(algo: str, *, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Gate for fresh entries.
    Default: ban new algo1 while any algo1 OPEN; ban algo2 while any algo2 OPEN.
    Optional: block_new_while_any_open → ban while ANY strategy has OPEN.
    """
    cfg = cfg or {}
    if cfg.get("block_new_while_any_open", True) and any_open():
        opens = trade_store.open_trades()
        ids = ", ".join(f"{t.get('algo')}:{t.get('symbol')}" for t in opens[:4])
        return False, f"OPEN position(s) exist — square first ({ids})"
    mine = open_for_algo(algo)
    if mine:
        ids = ", ".join(f"{t.get('symbol')}@{t.get('entry_price')}" for t in mine[:3])
        return False, f"{algo} still OPEN ({ids}) — no fresh entry until squared"
    return True, "ok"


def reject_duplicate_open(algo: str, *, cfg: dict[str, Any] | None = None) -> str | None:
    """Return rejection note if open_long must not proceed; else None."""
    ok, reason = can_open_new(algo, cfg=cfg)
    if ok:
        return None
    return reason


def all_open_ids() -> set[str]:
    return {str(t["id"]) for t in trade_store.open_trades() if t.get("id")}


def bind_primary(opens: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest OPEN row as primary manage target."""
    if not opens:
        return None
    opens = sorted(opens, key=lambda t: str(t.get("opened_at") or ""), reverse=True)
    return opens[0]

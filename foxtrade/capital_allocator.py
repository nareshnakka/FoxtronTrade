"""
Split deployable cash across Nifty / Sensex from Zerodha margins + open exposure.
"""
from __future__ import annotations

from typing import Any


def fetch_zerodha_available(kite: Any | None = None) -> dict[str, float]:
    """
    Equity available cash / collateral from Kite margins.
    Falls back to zeros on error (caller may use config capital_inr).
    """
    out = {
        "available": 0.0,
        "cash": 0.0,
        "collateral": 0.0,
        "net": 0.0,
        "ok": 0.0,
    }
    try:
        if kite is None:
            from kite_client import get_authenticated_kite

            kite = get_authenticated_kite()
        m = kite.margins(segment="equity") or {}
        # Kite shape: {available: {live_balance, cash, collateral}, net, ...}
        avail = m.get("available") or {}
        live = float(
            avail.get("live_balance")
            or avail.get("cash")
            or m.get("net")
            or 0
        )
        cash = float(avail.get("cash") or live or 0)
        coll = float(avail.get("collateral") or 0)
        net = float(m.get("net") or live or 0)
        out.update(
            {
                "available": max(0.0, live),
                "cash": max(0.0, cash),
                "collateral": max(0.0, coll),
                "net": max(0.0, net),
                "ok": 1.0,
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)  # type: ignore[assignment]
    return out


def open_premium_locked(trades: list[dict[str, Any]] | None) -> float:
    """Approx premium locked in OPEN long option positions."""
    locked = 0.0
    for t in trades or []:
        if str(t.get("status") or "").upper() != "OPEN":
            continue
        entry = float(t.get("entry_price") or 0)
        qty = int(t.get("qty") or 0)
        if qty <= 0:
            lots = int(t.get("lots") or 0)
            lot_size = int(t.get("lot_size") or 75)
            qty = lots * lot_size
        locked += max(0.0, entry * qty)
    return locked


def allocate_dual(
    *,
    available: float,
    cfg: dict[str, Any],
    scores: dict[str, float],
    open_by_underlying: dict[str, int] | None = None,
    locked_premium: float = 0.0,
) -> dict[str, Any]:
    """
    Decide per-underlying deploy budgets.

    Rules:
      • Free cash = available − locked_premium (floor 0)
      • Deploy fraction of free cash (deploy_pct_of_balance, default 0.85)
      • If only one side has a live signal (score>0) → up to dual_winner_share (0.70)
      • If both signal → weight by score, clamp each to dual_max_share (0.55)
      • If neither signals → keep 50/50 standby budgets (no force entry)
      • Side already in a position gets 0 fresh budget unless dual_pyramid
    """
    open_by = open_by_underlying or {}
    free = max(0.0, float(available) - float(locked_premium))
    deploy_pct = float(
        cfg.get("deploy_pct_of_balance")
        if cfg.get("deploy_pct_of_balance") is not None
        else (cfg.get("dual_deploy_pct") or 1.0)
    )
    # Production: allow full cash deploy (100%)
    deploy_pct = min(1.0, max(0.25, deploy_pct))
    pool = free * deploy_pct

    winner_share = float(cfg.get("dual_winner_share", 0.70))
    max_share = float(cfg.get("dual_max_share", 0.55))
    min_ticket = float(cfg.get("dual_min_ticket_inr", 8_000))

    nifty_s = float(scores.get("NIFTY") or 0)
    sx_s = float(scores.get("SENSEX") or 0)
    nifty_open = int(open_by.get("NIFTY") or 0)
    sx_open = int(open_by.get("SENSEX") or 0)

    budgets = {"NIFTY": 0.0, "SENSEX": 0.0}
    reason = "standby 50/50"

    if nifty_open and sx_open:
        reason = "both legs open — no fresh budget"
    elif nifty_s > 0 and sx_s > 0 and not nifty_open and not sx_open:
        tot = nifty_s + sx_s
        budgets["NIFTY"] = pool * min(max_share, nifty_s / tot)
        budgets["SENSEX"] = pool * min(max_share, sx_s / tot)
        # normalize if sum > pool
        s = budgets["NIFTY"] + budgets["SENSEX"]
        if s > pool and s > 0:
            budgets["NIFTY"] *= pool / s
            budgets["SENSEX"] *= pool / s
        reason = f"split by score N={nifty_s:.2f} S={sx_s:.2f}"
    elif nifty_s > 0 and not nifty_open:
        budgets["NIFTY"] = pool * winner_share
        budgets["SENSEX"] = 0.0 if sx_open else pool * (1.0 - winner_share) * 0.25
        reason = "Nifty signal — winner share"
    elif sx_s > 0 and not sx_open:
        budgets["SENSEX"] = pool * winner_share
        budgets["NIFTY"] = 0.0 if nifty_open else pool * (1.0 - winner_share) * 0.25
        reason = "Sensex signal — winner share"
    else:
        # standby reserves (for UI / next tick readiness)
        if not nifty_open:
            budgets["NIFTY"] = pool * 0.50
        if not sx_open:
            budgets["SENSEX"] = pool * 0.50
        reason = "no signal — standby reserves"

    # Drop dust budgets
    for k in list(budgets):
        if budgets[k] < min_ticket:
            if scores.get(k, 0) <= 0:
                budgets[k] = 0.0

    preferred = None
    if nifty_s > 0 or sx_s > 0:
        preferred = "NIFTY" if nifty_s >= sx_s else "SENSEX"
        # if preferred already open / zero budget, flip
        if preferred and (open_by.get(preferred) or budgets.get(preferred, 0) < min_ticket):
            other = "SENSEX" if preferred == "NIFTY" else "NIFTY"
            if scores.get(other, 0) > 0 and not open_by.get(other) and budgets.get(other, 0) >= min_ticket:
                preferred = other
                reason += f" → fallback {preferred}"
            elif budgets.get(preferred, 0) < min_ticket:
                preferred = None

    return {
        "available": round(available, 2),
        "free": round(free, 2),
        "pool": round(pool, 2),
        "locked_premium": round(locked_premium, 2),
        "budgets": {k: round(v, 2) for k, v in budgets.items()},
        "scores": {"NIFTY": nifty_s, "SENSEX": sx_s},
        "preferred": preferred,
        "reason": reason,
        "min_ticket": min_ticket,
    }


def lots_for_budget(
    *,
    budget: float,
    entry: float,
    lot_size: int,
    cfg: dict[str, Any],
) -> int:
    """Max lots that fit budget at entry premium."""
    if entry <= 0 or lot_size <= 0 or budget <= 0:
        return 0
    per = float(entry) * int(lot_size)
    lots = int(budget // max(per, 1.0))
    max_lots = int(cfg.get("max_lots_per_trade", 20))
    min_lots = int(cfg.get("min_lots_per_trade", 1))
    if lots < min_lots:
        # allow 1 lot if budget covers it
        if budget >= per:
            lots = 1
        else:
            return 0
    return max(0, min(max_lots, lots))

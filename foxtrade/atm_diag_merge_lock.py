"""
ATM + diagonal merge gate → lock highest near-equal premiums in 125–145.

Merge means:
  1. ATM CE ≈ ATM PE
  2. OTM diagonals CE(ATM+d) ≈ PE(ATM-d) (and optionally ITM mirror)

Then among CE/PE premiums in [band_lo, band_hi] with CE≈PE, pick highest mid and lock.
"""
from __future__ import annotations

from typing import Any


def _leg(legs: dict[str, Any], side: str, strike: int) -> float | None:
    x = legs.get(f"{side}:{strike}")
    if not x:
        return None
    v = x.get("c", x.get("ltp"))
    return None if v is None else float(v)


def chain_merge_ok(
    *,
    atm: int,
    legs: dict[str, Any],
    step: int = 50,
    atm_tol: float = 3.0,
    diag_tol: float = 5.0,
    diag_depths: tuple[int, ...] = (1, 2),
    require_itm: bool = False,
) -> dict[str, Any]:
    """Return {ok, reason, atm_gap, otm_gaps, itm_gaps}."""
    ce_atm = _leg(legs, "CE", atm)
    pe_atm = _leg(legs, "PE", atm)
    empty = {
        "ok": False,
        "reason": "missing ATM",
        "atm_gap": None,
        "otm_gaps": [],
        "itm_gaps": [],
    }
    if ce_atm is None or pe_atm is None:
        return empty
    atm_gap = abs(ce_atm - pe_atm)
    if atm_gap > atm_tol:
        return {
            **empty,
            "reason": f"ATM gap {atm_gap:.1f} > {atm_tol:g}",
            "atm_gap": round(atm_gap, 2),
            "ce_atm": ce_atm,
            "pe_atm": pe_atm,
        }

    otm_gaps: list[float] = []
    itm_gaps: list[float] = []
    for d in diag_depths:
        ce_otm = _leg(legs, "CE", atm + step * d)
        pe_otm = _leg(legs, "PE", atm - step * d)
        if ce_otm is None or pe_otm is None:
            return {
                **empty,
                "reason": f"missing OTM+{d} diagonal",
                "atm_gap": round(atm_gap, 2),
            }
        g = abs(ce_otm - pe_otm)
        otm_gaps.append(g)
        if g > diag_tol:
            return {
                **empty,
                "reason": f"OTM+{d} diag gap {g:.1f} > {diag_tol:g}",
                "atm_gap": round(atm_gap, 2),
                "otm_gaps": [round(x, 2) for x in otm_gaps],
            }

        if require_itm:
            ce_itm = _leg(legs, "CE", atm - step * d)
            pe_itm = _leg(legs, "PE", atm + step * d)
            if ce_itm is None or pe_itm is None:
                return {
                    **empty,
                    "reason": f"missing ITM+{d} diagonal",
                    "atm_gap": round(atm_gap, 2),
                    "otm_gaps": [round(x, 2) for x in otm_gaps],
                }
            ig = abs(ce_itm - pe_itm)
            itm_gaps.append(ig)
            if ig > diag_tol:
                return {
                    **empty,
                    "reason": f"ITM+{d} diag gap {ig:.1f} > {diag_tol:g}",
                    "atm_gap": round(atm_gap, 2),
                    "otm_gaps": [round(x, 2) for x in otm_gaps],
                    "itm_gaps": [round(x, 2) for x in itm_gaps],
                }

    return {
        "ok": True,
        "reason": "ATM+diagonal merge",
        "atm_gap": round(atm_gap, 2),
        "otm_gaps": [round(x, 2) for x in otm_gaps],
        "itm_gaps": [round(x, 2) for x in itm_gaps],
        "ce_atm": ce_atm,
        "pe_atm": pe_atm,
    }


def cross_atm_x_ok(
    *,
    atm: int,
    legs: dict[str, Any],
    step: int = 50,
    tol: float = 5.0,
    depths: tuple[int, ...] = (1, 2, 3, 4),
    band_lo: float = 125.0,
    band_hi: float = 145.0,
    prefer_band: bool = True,
) -> dict[str, Any]:
    """
    Cross-ATM X merge: CE(ATM+d) ≈ PE(ATM−d) without needing ATM CE≈PE.

    This is the pattern on expiry→next-week when ATM same-strike is still
    wide but the wings form an X (e.g. CE25450 ≈ PE25250 around ATM 25350).
    Also accepts asymmetric straddles (CE>ATM>PE) in the premium band.
    """
    empty: dict[str, Any] = {
        "ok": False,
        "reason": "no cross-ATM X",
        "atm": atm,
        "hits": [],
        "pair": None,
    }
    hits: list[dict[str, Any]] = []

    def _sym(side: str, strike: int) -> str:
        row = legs.get(f"{side}:{strike}") or {}
        return str(row.get("sym") or "")

    # 1) Symmetric X: CE(ATM+d) ≈ PE(ATM−d)
    for d in depths:
        ce_k = atm + step * d
        pe_k = atm - step * d
        ce = _leg(legs, "CE", ce_k)
        pe = _leg(legs, "PE", pe_k)
        if ce is None or pe is None:
            continue
        gap = abs(ce - pe)
        mid = (ce + pe) / 2.0
        if gap > tol:
            continue
        in_band = band_lo <= ce <= band_hi and band_lo <= pe <= band_hi
        hits.append(
            {
                "kind": "symmetric_x",
                "depth": d,
                "ce_strike": ce_k,
                "pe_strike": pe_k,
                "ce": round(ce, 2),
                "pe": round(pe, 2),
                "gap": round(gap, 2),
                "mid": round(mid, 2),
                "in_band": in_band,
                "ce_sym": _sym("CE", ce_k),
                "pe_sym": _sym("PE", pe_k),
            }
        )

    # 2) Asymmetric straddle X: any CE>ATM and PE<ATM in/near band, CE≈PE
    ces: list[tuple[int, float]] = []
    pes: list[tuple[int, float]] = []
    for key, row in legs.items():
        side, _, strike_s = key.partition(":")
        try:
            strike = int(float(strike_s))
            px = float(row.get("c", row.get("ltp") or 0))
        except (TypeError, ValueError):
            continue
        if side == "CE" and strike > atm:
            ces.append((strike, px))
        elif side == "PE" and strike < atm:
            pes.append((strike, px))
    for ce_k, ce in ces:
        for pe_k, pe in pes:
            # skip already covered symmetric depths
            if (ce_k - atm) == (atm - pe_k) and (ce_k - atm) % step == 0:
                continue
            gap = abs(ce - pe)
            mid = (ce + pe) / 2.0
            if gap > tol:
                continue
            in_band = band_lo <= ce <= band_hi and band_lo <= pe <= band_hi
            if prefer_band and not in_band:
                # still record near-band straddles
                if not (
                    band_lo - 10 <= ce <= band_hi + 10
                    and band_lo - 10 <= pe <= band_hi + 10
                ):
                    continue
            hits.append(
                {
                    "kind": "straddle_x",
                    "depth": None,
                    "ce_strike": ce_k,
                    "pe_strike": pe_k,
                    "ce": round(ce, 2),
                    "pe": round(pe, 2),
                    "gap": round(gap, 2),
                    "mid": round(mid, 2),
                    "in_band": in_band,
                    "ce_sym": _sym("CE", ce_k),
                    "pe_sym": _sym("PE", pe_k),
                }
            )

    if not hits:
        return empty

    # Prefer in-band, then tighter gap, then mid nearest band centre, symmetric first
    band_mid = (band_lo + band_hi) / 2.0

    def _rank(h: dict[str, Any]) -> tuple:
        return (
            0 if h.get("in_band") else 1,
            0 if h.get("kind") == "symmetric_x" else 1,
            float(h.get("gap") or 99),
            abs(float(h.get("mid") or 0) - band_mid),
        )

    hits.sort(key=_rank)
    best = hits[0]
    pair = {
        "ce_strike": best["ce_strike"],
        "pe_strike": best["pe_strike"],
        "ce": best["ce"],
        "pe": best["pe"],
        "gap": best["gap"],
        "mid": best["mid"],
        "ce_sym": best.get("ce_sym") or "",
        "pe_sym": best.get("pe_sym") or "",
        "x_kind": best.get("kind"),
        "x_depth": best.get("depth"),
    }
    return {
        "ok": True,
        "reason": (
            f"cross-ATM X {best['kind']} "
            f"CE{best['ce_strike']}@{best['ce']}≈PE{best['pe_strike']}@{best['pe']} "
            f"gap={best['gap']}"
        ),
        "atm": atm,
        "hits": hits[:8],
        "pair": pair,
        "ce_atm": _leg(legs, "CE", atm),
        "pe_atm": _leg(legs, "PE", atm),
    }


def pick_highest_band_pair(
    *,
    legs: dict[str, Any],
    band_lo: float = 125.0,
    band_hi: float = 145.0,
    equal_tol: float = 3.0,
) -> dict[str, Any] | None:
    """
    Among CE/PE in band with |CE-PE|<=equal_tol, pick highest mid.
    Allows different strikes (cross-strike convergence).
    """
    ces: list[tuple[int, float, str]] = []
    pes: list[tuple[int, float, str]] = []
    for key, row in legs.items():
        side, _, strike_s = key.partition(":")
        try:
            strike = int(float(strike_s))
        except ValueError:
            continue
        px = float(row.get("c", row.get("ltp") or 0))
        if not (band_lo <= px <= band_hi):
            continue
        sym = str(row.get("sym") or "")
        if side == "CE":
            ces.append((strike, px, sym))
        elif side == "PE":
            pes.append((strike, px, sym))

    best = None
    best_key = None
    for cs, cp, csym in ces:
        for ps, pp, psym in pes:
            gap = abs(cp - pp)
            if gap > equal_tol:
                continue
            mid = (cp + pp) / 2.0
            if not (band_lo <= mid <= band_hi):
                continue
            # highest mid, then tighter gap, then higher strikes
            key = (-mid, gap, -(cs + ps))
            if best_key is None or key < best_key:
                best_key = key
                best = {
                    "ce_strike": cs,
                    "pe_strike": ps,
                    "ce": round(cp, 2),
                    "pe": round(pp, 2),
                    "gap": round(gap, 2),
                    "mid": round(mid, 2),
                    "ce_sym": csym,
                    "pe_sym": psym,
                }
    return best


def try_lock_pair(
    *,
    atm: int,
    legs: dict[str, Any],
    band_lo: float = 125.0,
    band_hi: float = 145.0,
    atm_tol: float = 3.0,
    diag_tol: float = 5.0,
    equal_tol: float = 3.0,
    step: int = 50,
    require_itm: bool = False,
    require_atm_gate: bool = True,
    allow_cross_atm_x: bool = True,
    cross_x_tol: float | None = None,
    cross_x_depths: tuple[int, ...] = (1, 2, 3, 4),
) -> dict[str, Any]:
    """
    Lock near-equal CE/PE in premium band (cross-strike OK).

    Order:
      1) ATM+diagonal merge (classic)
      2) If ATM not merged: cross-ATM X  CE(ATM+d)≈PE(ATM−d)
         — used on expiry→next-week when ATM same-strike is still wide
      3) If require_atm_gate=False: any clear in-band pair (band-only)
    """
    gate = chain_merge_ok(
        atm=atm,
        legs=legs,
        step=step,
        atm_tol=atm_tol,
        diag_tol=diag_tol,
        require_itm=require_itm,
    )
    x_tol = float(cross_x_tol if cross_x_tol is not None else max(equal_tol, diag_tol))
    x_gate = (
        cross_atm_x_ok(
            atm=atm,
            legs=legs,
            step=step,
            tol=x_tol,
            depths=cross_x_depths,
            band_lo=band_lo,
            band_hi=band_hi,
        )
        if allow_cross_atm_x
        else {"ok": False, "reason": "cross-X disabled", "pair": None}
    )
    band_pair = pick_highest_band_pair(
        legs=legs, band_lo=band_lo, band_hi=band_hi, equal_tol=equal_tol
    )

    def _legs_in_band(p: dict[str, Any] | None, *, soft: float = 0.0) -> bool:
        if not p:
            return False
        try:
            ce = float(p["ce"])
            pe = float(p["pe"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            band_lo - soft <= ce <= band_hi + soft
            and band_lo - soft <= pe <= band_hi + soft
        )

    # Prefer X legs when they sit in band; else highest band mid.
    # Never lock a pair whose CE/PE are outside 125–145 (no mid~194 ATM junk).
    pair = None
    how = None
    if gate.get("ok") and band_pair is not None and _legs_in_band(band_pair):
        pair, how = band_pair, "ATM+diag"
    elif x_gate.get("ok") and x_gate.get("pair") is not None:
        xp = x_gate["pair"]
        if _legs_in_band(xp, soft=0.0):
            pair, how = xp, f"cross-ATM X/{xp.get('x_kind')}"
        elif band_pair is not None and _legs_in_band(band_pair):
            pair, how = band_pair, "cross-X ok · band pick"
        # else: ignore out-of-band X — wait for real 125–145 merge
    elif not require_atm_gate and band_pair is not None and _legs_in_band(band_pair):
        pair, how = band_pair, "band-only"
    elif require_atm_gate and not gate.get("ok") and not x_gate.get("ok"):
        return {
            "ok": False,
            "locked": False,
            "gate": {**gate, "cross_x": x_gate},
            "pair": band_pair if _legs_in_band(band_pair) else None,
            "reason": (
                f"ATM {gate.get('reason')}; X {x_gate.get('reason')}"
            ),
        }

    if pair is None or not _legs_in_band(pair):
        return {
            "ok": False,
            "locked": False,
            "gate": {**gate, "cross_x": x_gate},
            "pair": None,
            "reason": (
                "no CE/PE pair inside premium band"
                if gate.get("ok") or x_gate.get("ok")
                else f"no band/X pair · ATM {gate.get('reason')}"
            ),
        }
    return {
        "ok": True,
        "locked": True,
        "gate": {**gate, "cross_x": x_gate},
        "pair": pair,
        "reason": (
            f"LOCK ({how}) mid={pair['mid']} gap={pair['gap']} "
            f"CE{pair['ce_strike']}@{pair['ce']} PE{pair['pe_strike']}@{pair['pe']}"
        ),
    }

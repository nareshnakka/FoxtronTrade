"""
Early winner lean — bid one side BEFORE the CE/PE premium gap blows out.

Noise-resistant side ID (learned 13-Nov-2026 lock-pair flips):
  Prefer short persistence of the *current* leader; never majority / cum-area
  (those remember the wrong early side when the lead flips).

While still merged (gap <= merge_gap), fire when either:
  A) Persist path: same clear leader for >= persist bars + spread slope agrees
  B) Last+ATM path: last lead + ATM CE/PE lead agree + spread slope agrees

Spot ROC is a soft veto (hard disagree blocks); not required by default.

Spot 1m candle (learned 13-Nov-2026): at the break bar, green→CE / red→PE
hit 15/15. Prior bar alone is weak — use *current* (forming or last) candle
as confirm during the breakout range, not majority of older bars.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def spot_1m_candle_side(
    candles_1m: list[dict[str, Any]] | None,
    *,
    spot_now: float | None = None,
) -> dict[str, Any]:
    """
    Map Nifty 1m candle color → breakout side.
      green (close > open) → CE
      red   (close < open) → PE
      doji                 → None

    Prefers the forming minute: if last candle is this minute and spot_now
    is set, use spot_now as live close vs that candle's open.
    """
    empty = {"side": None, "color": None, "open": None, "close": None}
    if not candles_1m:
        return empty
    last = candles_1m[-1]
    try:
        o = float(last["open"])
        c = float(last["close"])
    except (KeyError, TypeError, ValueError):
        return empty
    # Live override for in-progress minute
    dt = last.get("date")
    if spot_now is not None and dt is not None:
        try:
            if getattr(dt, "tzinfo", None):
                dt_i = dt.astimezone(IST)
            else:
                dt_i = dt.replace(tzinfo=IST) if hasattr(dt, "replace") else None
            now = datetime.now(IST)
            if dt_i is not None and dt_i.hour == now.hour and dt_i.minute == now.minute:
                c = float(spot_now)
        except Exception:
            pass
    if c > o:
        color, side = "green", "CE"
    elif c < o:
        color, side = "red", "PE"
    else:
        color, side = "doji", None
    return {
        "side": side,
        "color": color,
        "open": round(o, 2),
        "close": round(c, 2),
    }


def _slope(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 3:
        return None
    xs = list(range(n))
    mx = mean(xs)
    my = mean(vals)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    return num / den


def _roc(vals: list[float], n: int) -> float | None:
    if len(vals) <= n or vals[-1 - n] == 0:
        return None
    return (vals[-1] / vals[-1 - n] - 1.0) * 100.0


def _side_from_lead(lead: float, pts: float) -> str | None:
    if lead >= pts:
        return "CE"
    if lead <= -pts:
        return "PE"
    return None


def _slope_agrees(side: str, spr_slope: float | None, slope_min: float) -> bool:
    if spr_slope is None:
        return False
    if side == "CE":
        return spr_slope >= slope_min
    return spr_slope <= -slope_min


def _spot_veto(
    side: str,
    spot_roc: float | None,
    *,
    spot_roc_min: float,
    require_spot: bool,
) -> str | None:
    """Return reason string if blocked, else None."""
    if spot_roc is None:
        return "need spot ROC" if require_spot else None
    if side == "CE" and spot_roc < -abs(spot_roc_min):
        return f"CE lean but spot_roc {spot_roc:.3f}% hard disagree"
    if side == "PE" and spot_roc > abs(spot_roc_min):
        return f"PE lean but spot_roc {spot_roc:.3f}% hard disagree"
    if require_spot:
        if side == "CE" and spot_roc < spot_roc_min:
            return f"CE lean but spot_roc {spot_roc:.3f}% weak"
        if side == "PE" and spot_roc > -spot_roc_min:
            return f"PE lean but spot_roc {spot_roc:.3f}% weak"
    return None


def detect_early_winner_lean(
    *,
    ce_hist: list[float],
    pe_hist: list[float],
    spot_hist: list[float] | None = None,
    atm_ce_hist: list[float] | None = None,
    atm_pe_hist: list[float] | None = None,
    spot_candle: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Returns {ok, side, reason, score, gap, lead, path, persist_n, ...}.
    Uses only history ending at the latest bar (no lookahead).
    """
    cfg = cfg or {}
    empty: dict[str, Any] = {
        "ok": False,
        "side": None,
        "reason": "warming",
        "score": 0.0,
        "gap": None,
        "lead": None,
        "spread_slope": None,
        "spot_roc": None,
        "path": None,
        "persist_n": 0,
        "last_side": None,
        "atm_side": None,
        "candle_side": None,
        "candle_color": None,
    }
    if not cfg.get("early_winner_lean_enabled", True):
        return {**empty, "reason": "lean disabled"}

    look = max(3, int(cfg.get("early_winner_lean_lookback", 5)))
    merge_gap = float(cfg.get("early_winner_lean_merge_gap", 8.0))
    lead_pts = float(cfg.get("early_winner_lean_lead_pts", 3.0))
    clear_pts = float(cfg.get("early_winner_lean_clear_pts", 2.0))
    persist = max(1, int(cfg.get("early_winner_lean_persist", 2)))
    slope_min = float(cfg.get("early_winner_lean_slope_min", 0.08))
    spot_roc_min = float(cfg.get("early_winner_lean_spot_roc_min", 0.015))
    require_spot = bool(cfg.get("early_winner_lean_require_spot", False))
    require_atm = bool(cfg.get("early_winner_lean_require_atm", False))
    candle_confirm = bool(cfg.get("early_winner_lean_candle_confirm", True))
    require_candle = bool(cfg.get("early_winner_lean_require_candle", False))

    n = min(len(ce_hist), len(pe_hist))
    if n < look + 1:
        return empty
    ce = [float(x) for x in ce_hist[-n:]]
    pe = [float(x) for x in pe_hist[-n:]]
    spot = None
    if spot_hist:
        sn = min(n, len(spot_hist))
        spot = [float(x) for x in spot_hist[-sn:]]

    atm_ce = atm_pe = None
    if atm_ce_hist and atm_pe_hist:
        an = min(n, len(atm_ce_hist), len(atm_pe_hist))
        if an >= 1:
            atm_ce = [float(x) for x in atm_ce_hist[-an:]]
            atm_pe = [float(x) for x in atm_pe_hist[-an:]]

    gap = abs(ce[-1] - pe[-1])
    lead = ce[-1] - pe[-1]
    if gap > merge_gap:
        return {
            **empty,
            "reason": f"gap {gap:.1f} > merge {merge_gap:g} (already split)",
            "gap": round(gap, 2),
            "lead": round(lead, 2),
        }

    # Last clear lead on the locked pair (ignore majority / cum area)
    last_side = _side_from_lead(lead, clear_pts)

    # Consecutive persist of same leader with lead_pts (stricter than clear)
    persist_n = 0
    persist_side: str | None = None
    for i in range(-1, -n - 1, -1):
        d = ce[i] - pe[i]
        if abs(ce[i] - pe[i]) > merge_gap:
            break
        s = _side_from_lead(d, lead_pts)
        if s is None:
            break
        if persist_side is None:
            persist_side = s
            persist_n = 1
        elif s == persist_side:
            persist_n += 1
        else:
            break
    if persist_n < persist:
        persist_side = None

    # ATM last lead (optional confirm — survives pair fluctuation)
    atm_side: str | None = None
    if atm_ce is not None and atm_pe is not None:
        atm_side = _side_from_lead(atm_ce[-1] - atm_pe[-1], clear_pts)

    win = min(look, n)
    spreads = [ce[i] - pe[i] for i in range(-win, 0)]
    spr_slope = _slope(spreads)

    spot_roc = None
    if spot and len(spot) > 3:
        spot_roc = _roc(spot, min(3, len(spot) - 1))

    candle_side = (spot_candle or {}).get("side")
    candle_color = (spot_candle or {}).get("color")

    base = {
        "gap": round(gap, 2),
        "lead": round(lead, 2),
        "spread_slope": None if spr_slope is None else round(spr_slope, 3),
        "spot_roc": None if spot_roc is None else round(spot_roc, 4),
        "persist_n": persist_n,
        "last_side": last_side,
        "atm_side": atm_side,
        "candle_side": candle_side,
        "candle_color": candle_color,
        "ce": round(ce[-1], 2),
        "pe": round(pe[-1], 2),
    }

    candidates: list[tuple[str, str]] = []  # (path, side)
    if persist_side is not None:
        candidates.append(("persist", persist_side))
    if last_side is not None:
        if atm_side is not None and atm_side == last_side:
            candidates.append(("last+atm", last_side))
        elif atm_side is None and not require_atm:
            # No ATM feed yet — allow last+slope only if persist already failed
            if persist_side is None:
                candidates.append(("last", last_side))
        elif require_atm and atm_side is None:
            pass
        elif atm_side is not None and atm_side != last_side:
            # ATM disagrees with last — skip last path (persist may still fire)
            pass

    if not candidates:
        why = "no persist / last+atm"
        if last_side and atm_side and last_side != atm_side:
            why = f"last {last_side} vs atm {atm_side}"
        elif last_side is None:
            why = f"lead |{lead:.1f}| < clear {clear_pts:g}"
        return {**empty, **base, "reason": why}

    # Prefer persist, then last+atm, then last
    path_rank = {"persist": 0, "last+atm": 1, "last": 2}
    candidates.sort(key=lambda x: path_rank.get(x[0], 9))

    last_fail = "no path"
    for path, side in candidates:
        if not _slope_agrees(side, spr_slope, slope_min):
            last_fail = (
                f"{path} {side} but slope "
                f"{spr_slope if spr_slope is not None else 'n/a'} weak"
            )
            continue
        veto = _spot_veto(
            side, spot_roc, spot_roc_min=spot_roc_min, require_spot=require_spot
        )
        if veto:
            last_fail = veto
            continue

        # 1m candle: green→CE / red→PE during breakout range
        if require_candle and candle_side is None:
            last_fail = "need spot 1m candle"
            continue
        if candle_confirm and candle_side is not None and candle_side != side:
            last_fail = f"{path} {side} but 1m candle {candle_color}→{candle_side}"
            continue

        score = abs(lead) + abs(spr_slope or 0) * 2 + abs(spot_roc or 0) * 10
        if path == "persist":
            score += persist_n * 2
        if path == "last+atm":
            score += 3
        if candle_side == side:
            score += 4
        return {
            "ok": True,
            "side": side,
            "reason": (
                f"lean {side} via {path} lead={lead:+.1f} gap={gap:.1f} "
                f"persist={persist_n} slope={spr_slope:+.2f}"
                + (f" atm={atm_side}" if atm_side else "")
                + (f" candle={candle_color}" if candle_color else "")
                + (f" spot_roc={spot_roc:+.3f}%" if spot_roc is not None else "")
            ),
            "score": round(score, 2),
            "path": path,
            **base,
        }

    return {**empty, **base, "reason": last_fail}

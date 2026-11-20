"""
Breakout / fallen trendlines for dual exits.

Winner (up-TL): rising support that *touches bottoms* (two swing lows).
  Sell when price breaks below the line.

Fallen (down-TL): falling resistance that *touches tops* (two swing highs).
  Sell when price breaks above the line (retrace).

As price progresses, call correct_up/down_trendline so a newer valid
pivot rotates the line onto the fresh bottom/top (hand-drawn correction).
"""
from __future__ import annotations

from typing import Any


def _local_extrema(
    px: list[float],
    end_i: int,
    *,
    find_low: bool,
    min_sep: int = 3,
) -> list[tuple[int, float]]:
    """Interior swing pivots from 0..end_i, spaced at least min_sep bars."""
    if not px or end_i < 2:
        return []
    hi = min(end_i, len(px) - 1)
    raw: list[tuple[int, float]] = []
    for j in range(1, hi):
        a, b, c = float(px[j - 1]), float(px[j]), float(px[j + 1])
        if find_low:
            if b <= a and b <= c:
                raw.append((j, b))
        else:
            if b >= a and b >= c:
                raw.append((j, b))
    # Always include absolute extreme in window (day base)
    window = [float(px[k]) for k in range(0, hi + 1)]
    if find_low:
        abs_i = min(range(0, hi + 1), key=lambda k: float(px[k]))
        abs_v = float(px[abs_i])
    else:
        abs_i = max(range(0, hi + 1), key=lambda k: float(px[k]))
        abs_v = float(px[abs_i])
    if not any(i == abs_i for i, _ in raw):
        raw.append((abs_i, abs_v))
    raw.sort(key=lambda x: x[0])
    # Thin to min_sep, keep more extreme when close
    out: list[tuple[int, float]] = []
    for i, v in raw:
        if not out:
            out.append((i, v))
            continue
        pi, pv = out[-1]
        if i - pi < min_sep:
            if find_low and v < pv:
                out[-1] = (i, v)
            elif (not find_low) and v > pv:
                out[-1] = (i, v)
        else:
            out.append((i, v))
    return out


def _line_ok_support(
    pivots: list[tuple[int, float]],
    i0: int,
    y0: float,
    i1: int,
    y1: float,
    *,
    tol: float,
) -> bool:
    """All bottoms sit on or above the candidate support line."""
    span = max(1, i1 - i0)
    slope = (y1 - y0) / span
    for i, y in pivots:
        lvl = y0 + slope * (i - i0)
        if y < lvl - tol:
            return False
    return True


def _line_ok_resistance(
    pivots: list[tuple[int, float]],
    i0: int,
    y0: float,
    i1: int,
    y1: float,
    *,
    tol: float,
) -> bool:
    """All tops sit on or below the candidate resistance line."""
    span = max(1, i1 - i0)
    slope = (y1 - y0) / span
    for i, y in pivots:
        lvl = y0 + slope * (i - i0)
        if y > lvl + tol:
            return False
    return True


def _pick_support_pair(
    pivots: list[tuple[int, float]],
    *,
    tol: float = 0.75,
) -> tuple[int, float, int, float] | None:
    """
    Two bottoms defining rising support that touches both and doesn't cut
    through other bottoms. Prefer latest second pivot, then longer span.
    """
    if len(pivots) < 2:
        return None
    best: tuple[int, float, int, float] | None = None
    best_key: tuple[int, int, float] | None = None  # (i1, span, slope)
    for a in range(len(pivots)):
        i0, y0 = pivots[a]
        for b in range(a + 1, len(pivots)):
            i1, y1 = pivots[b]
            if i1 <= i0:
                continue
            # Rising (or flat) support — bottoms must not descend
            if y1 + tol < y0:
                continue
            if not _line_ok_support(pivots, i0, y0, i1, y1, tol=tol):
                continue
            span = i1 - i0
            slope = (y1 - y0) / max(1, span)
            key = (i1, span, slope)
            if best is None or key > best_key:  # type: ignore[operator]
                best = (i0, y0, i1, y1)
                best_key = key
    return best


def _pick_resistance_pair(
    pivots: list[tuple[int, float]],
    *,
    tol: float = 0.75,
) -> tuple[int, float, int, float] | None:
    """Two tops defining falling resistance that touches both."""
    if len(pivots) < 2:
        return None
    best: tuple[int, float, int, float] | None = None
    best_key: tuple[int, int, float] | None = None
    for a in range(len(pivots)):
        i0, y0 = pivots[a]
        for b in range(a + 1, len(pivots)):
            i1, y1 = pivots[b]
            if i1 <= i0:
                continue
            # Falling (or flat) resistance
            if y1 - tol > y0:
                continue
            if not _line_ok_resistance(pivots, i0, y0, i1, y1, tol=tol):
                continue
            span = i1 - i0
            slope = (y1 - y0) / max(1, span)
            # Prefer latest second pivot, longer span, steeper fall (more negative)
            key = (i1, span, -slope)
            if best is None or key > best_key:  # type: ignore[operator]
                best = (i0, y0, i1, y1)
                best_key = key
    return best


def _tl_from_pivots(
    *,
    kind: str,
    i0: int,
    y0: float,
    i1: int,
    y1: float,
    break_i: int,
    from_start: bool,
) -> dict[str, Any]:
    span = max(1, i1 - i0)
    slope = (y1 - y0) / span
    if kind == "up":
        if from_start:
            slope = max(0.0, min(2.0, slope))
        else:
            slope = max(0.0, min(0.35, slope))
        # Flat is allowed if bottoms equal; nudge tiny rise for exit math
        if slope <= 0:
            slope = 0.005 if from_start else 0.02
    else:
        if from_start:
            slope = min(0.0, max(-2.0, slope))
        else:
            slope = min(0.0, max(-0.35, slope))
        if slope >= 0:
            slope = -0.005 if from_start else -0.02
    return {
        "kind": kind,
        "i0": int(i0),
        "y0": round(float(y0), 2),
        "i1": int(i1),
        "y1": round(float(y1), 2),
        "slope": round(float(slope), 5),
        "intercept": round(float(y0) - float(slope) * int(i0), 3),
        "break_i": int(break_i),
        "from_start": bool(from_start),
        # Chart/exit anchors = first bottom/top (line touches it)
        "anchor_fi": int(i0),
        "anchor_y": round(float(y0), 2),
    }


def fit_up_trendline(
    px_hist: list[float],
    *,
    break_i: int,
    lookback: int = 12,
    from_start: bool = True,
    tol: float = 0.75,
) -> dict[str, Any] | None:
    """Rising support through two swing *bottoms* (not through break price)."""
    if break_i < 1 or break_i >= len(px_hist):
        return None
    end = break_i if from_start else min(break_i, max(1, break_i))
    # Search pivots in full day (or lookback window)
    if from_start:
        pivots = _local_extrema(px_hist, end, find_low=True, min_sep=3)
    else:
        lo = max(0, break_i - lookback)
        seg = px_hist[lo : break_i + 1]
        local = _local_extrema(seg, len(seg) - 1, find_low=True, min_sep=2)
        pivots = [(lo + i, y) for i, y in local]
    pair = _pick_support_pair(pivots, tol=tol)
    if pair is None and len(pivots) >= 1:
        # Fallback: earliest bottom → most recent bottom (force rising)
        i0, y0 = pivots[0]
        i1, y1 = pivots[-1]
        if i1 <= i0:
            return None
        if y1 < y0:
            y1 = y0  # flat support through base
        return _tl_from_pivots(
            kind="up",
            i0=i0,
            y0=y0,
            i1=i1,
            y1=y1,
            break_i=break_i,
            from_start=from_start,
        )
    if pair is None:
        return None
    i0, y0, i1, y1 = pair
    return _tl_from_pivots(
        kind="up",
        i0=i0,
        y0=y0,
        i1=i1,
        y1=y1,
        break_i=break_i,
        from_start=from_start,
    )


def fit_premium_channel(
    px_hist: list[float],
    *,
    break_i: int,
    lookback: int = 12,
    from_start: bool = True,
    tol: float = 0.75,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> dict[str, Any]:  # tol forwarded to parallel fit (Index uses ~5 pts)
    """
    Parallel channel (screenshot-style): same slope on both rails.
    Upper touches swing highs, lower is parallel and touches swing lows.
    Prefer real OHLC highs/lows when provided; else use close series.
    Returns {up, down, slope, width} where up=support(lows), down=resistance(highs).
    """
    ch = fit_parallel_channel(
        px_hist,
        break_i=break_i,
        lookback=lookback,
        from_start=from_start,
        tol=tol,
        highs=highs,
        lows=lows,
    )
    if ch:
        return ch
    # Fallback: independent rails if parallel fit fails
    return {
        "up": fit_up_trendline(
            px_hist,
            break_i=break_i,
            lookback=lookback,
            from_start=from_start,
            tol=tol,
        ),
        "down": fit_down_trendline(
            px_hist,
            break_i=break_i,
            lookback=lookback,
            from_start=from_start,
            tol=tol,
        ),
        "slope": None,
        "width": None,
        "parallel": False,
    }


def fit_parallel_channel(
    px_hist: list[float],
    *,
    break_i: int,
    lookback: int = 12,
    from_start: bool = True,
    tol: float = 0.75,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> dict[str, Any] | None:
    """
    Build a true parallel channel like a hand-drawn descending/ascending band.

    1) Fit slope from the clearer rail (two swing highs OR two swing lows)
    2) Offset the opposite rail with the *same* slope so it touches the
       extreme opposite pivots (max channel width from primary rail)
    """
    if break_i < 2 or break_i >= len(px_hist):
        return None
    end = break_i if from_start else min(break_i, max(1, break_i))
    hi_src = list(highs) if highs is not None and len(highs) > end else list(px_hist)
    lo_src = list(lows) if lows is not None and len(lows) > end else list(px_hist)
    if from_start:
        hi_piv = _local_extrema(hi_src, end, find_low=False, min_sep=3)
        lo_piv = _local_extrema(lo_src, end, find_low=True, min_sep=3)
    else:
        lo0 = max(0, break_i - lookback)
        hi_local = _local_extrema(hi_src[lo0 : end + 1], end - lo0, find_low=False, min_sep=2)
        lo_local = _local_extrema(lo_src[lo0 : end + 1], end - lo0, find_low=True, min_sep=2)
        hi_piv = [(lo0 + i, y) for i, y in hi_local]
        lo_piv = [(lo0 + i, y) for i, y in lo_local]

    candidates: list[dict[str, Any]] = []

    def _score(slope: float, a_hi: float, a_lo: float, i0: int, i1: int) -> float:
        """Higher = tighter parallel channel covering the segment."""
        width = a_hi - a_lo
        if width < 0.5:
            return -1e9
        # Penalize rails that cut through many bars
        cut = 0.0
        cover = 0
        for i in range(i0, min(end, i1 + max(5, (i1 - i0))) + 1):
            if i >= len(hi_src) or i >= len(lo_src):
                break
            uh = a_hi + slope * i
            ul = a_lo + slope * i
            h, l = float(hi_src[i]), float(lo_src[i])
            if h > uh + tol:
                cut += h - uh
            if l < ul - tol:
                cut += ul - l
            if l <= uh + tol and h >= ul - tol:
                cover += 1
        span = max(1, i1 - i0)
        return cover * 2.0 + span * 0.15 - cut * 0.35 - abs(width) * 0.02

    # --- Resistance-first (descending / flat channel like the screenshot) ---
    res = _pick_resistance_pair(hi_piv, tol=tol)
    if res is None and len(hi_piv) >= 2:
        (i0, y0), (i1, y1) = hi_piv[0], hi_piv[-1]
        if i1 > i0:
            res = (i0, y0, i1, y1)
    if res is not None:
        i0, y0, i1, y1 = res
        span = max(1, i1 - i0)
        slope = (y1 - y0) / span
        # upper in intercept form: y = a_hi + slope * i  (anchor at i=0 space)
        a_hi = y0 - slope * i0
        # Parallel lower: touch the most extreme low vs upper
        piv = lo_piv if lo_piv else [(i, float(lo_src[i])) for i in range(0, end + 1)]
        width = max((a_hi + slope * i) - y for i, y in piv) if piv else 4.0
        width = max(1.0, float(width))
        a_lo = a_hi - width
        # Build TL dicts in pivot space (i0/i1 for upper; mirror for lower)
        # Find best lower touch index; keep i0 < i1 for stable frame mapping
        touch_i, _touch_y = min(piv, key=lambda p: (a_hi + slope * p[0]) - p[1])
        lo_a, lo_b = sorted((int(touch_i), int(i1)))
        up = {
            "kind": "up",
            "i0": lo_a,
            "y0": round(float(a_lo + slope * lo_a), 2),
            "i1": lo_b,
            "y1": round(float(a_lo + slope * lo_b), 2),
            "slope": round(float(slope), 5),
            "intercept": round(float(a_lo), 3),
            "break_i": int(break_i),
            "from_start": bool(from_start),
            "anchor_fi": lo_a,
            "anchor_y": round(float(a_lo + slope * lo_a), 2),
            "parallel": True,
        }
        hi_a, hi_b = sorted((int(i0), int(i1)))
        down = {
            "kind": "down",
            "i0": hi_a,
            "y0": round(float(a_hi + slope * hi_a), 2),
            "i1": hi_b,
            "y1": round(float(a_hi + slope * hi_b), 2),
            "slope": round(float(slope), 5),
            "intercept": round(float(a_hi), 3),
            "break_i": int(break_i),
            "from_start": bool(from_start),
            "anchor_fi": hi_a,
            "anchor_y": round(float(a_hi + slope * hi_a), 2),
            "parallel": True,
        }
        candidates.append(
            {
                "up": up,
                "down": down,
                "slope": slope,
                "width": width,
                "parallel": True,
                "score": _score(slope, a_hi, a_lo, i0, i1),
            }
        )

    # --- Support-first (rising channel for expanding winner) ---
    sup = _pick_support_pair(lo_piv, tol=tol)
    if sup is None and len(lo_piv) >= 2:
        (i0, y0), (i1, y1) = lo_piv[0], lo_piv[-1]
        if i1 > i0:
            sup = (i0, y0, i1, y1)
    if sup is not None:
        i0, y0, i1, y1 = sup
        span = max(1, i1 - i0)
        slope = (y1 - y0) / span
        a_lo = y0 - slope * i0
        piv = hi_piv if hi_piv else [(i, float(hi_src[i])) for i in range(0, end + 1)]
        width = max(y - (a_lo + slope * i) for i, y in piv) if piv else 4.0
        width = max(1.0, float(width))
        a_hi = a_lo + width
        touch_i, _touch_y = max(piv, key=lambda p: p[1] - (a_lo + slope * p[0]))
        lo_a, lo_b = sorted((int(i0), int(i1)))
        up = {
            "kind": "up",
            "i0": lo_a,
            "y0": round(float(a_lo + slope * lo_a), 2),
            "i1": lo_b,
            "y1": round(float(a_lo + slope * lo_b), 2),
            "slope": round(float(slope), 5),
            "intercept": round(float(a_lo), 3),
            "break_i": int(break_i),
            "from_start": bool(from_start),
            "anchor_fi": lo_a,
            "anchor_y": round(float(a_lo + slope * lo_a), 2),
            "parallel": True,
        }
        hi_a, hi_b = sorted((int(touch_i), int(i1)))
        down = {
            "kind": "down",
            "i0": hi_a,
            "y0": round(float(a_hi + slope * hi_a), 2),
            "i1": hi_b,
            "y1": round(float(a_hi + slope * hi_b), 2),
            "slope": round(float(slope), 5),
            "intercept": round(float(a_hi), 3),
            "break_i": int(break_i),
            "from_start": bool(from_start),
            "anchor_fi": hi_a,
            "anchor_y": round(float(a_hi + slope * hi_a), 2),
            "parallel": True,
        }
        candidates.append(
            {
                "up": up,
                "down": down,
                "slope": slope,
                "width": width,
                "parallel": True,
                "score": _score(slope, a_hi, a_lo, i0, i1),
            }
        )

    if not candidates:
        return None
    best = max(candidates, key=lambda c: c["score"])
    return {
        "up": best["up"],
        "down": best["down"],
        "slope": best["slope"],
        "width": best["width"],
        "parallel": True,
    }


def fit_down_trendline(
    px_hist: list[float],
    *,
    break_i: int,
    lookback: int = 12,
    from_start: bool = True,
    tol: float = 0.75,
) -> dict[str, Any] | None:
    """Falling resistance through two swing *tops*."""
    if break_i < 1 or break_i >= len(px_hist):
        return None
    end = break_i
    if from_start:
        pivots = _local_extrema(px_hist, end, find_low=False, min_sep=3)
    else:
        lo = max(0, break_i - lookback)
        seg = px_hist[lo : break_i + 1]
        local = _local_extrema(seg, len(seg) - 1, find_low=False, min_sep=2)
        pivots = [(lo + i, y) for i, y in local]
    pair = _pick_resistance_pair(pivots, tol=tol)
    if pair is None and len(pivots) >= 1:
        i0, y0 = pivots[0]
        i1, y1 = pivots[-1]
        if i1 <= i0:
            return None
        if y1 > y0:
            y1 = y0
        return _tl_from_pivots(
            kind="down",
            i0=i0,
            y0=y0,
            i1=i1,
            y1=y1,
            break_i=break_i,
            from_start=from_start,
        )
    if pair is None:
        return None
    i0, y0, i1, y1 = pair
    return _tl_from_pivots(
        kind="down",
        i0=i0,
        y0=y0,
        i1=i1,
        y1=y1,
        break_i=break_i,
        from_start=from_start,
    )


def _tl_in_hist_space(tl: dict[str, Any] | None, n: int, px: float) -> bool:
    """False when prior TL is from another index space / projects nonsense."""
    if not tl or n < 2:
        return False
    i0 = int(tl.get("i0") or -1)
    i1 = int(tl.get("i1") if tl.get("i1") is not None else tl.get("break_i") or -1)
    if i0 < 0 or i1 < 0 or i0 >= n or i1 >= n or i1 <= i0:
        return False
    lvl = trendline_value(tl, n - 1)
    if lvl is None:
        return False
    # Premium TLs should stay near traded premium — not hundreds of pts away
    return abs(float(lvl) - float(px)) <= 120.0


def correct_up_trendline(
    tl: dict[str, Any] | None,
    px_hist: list[float],
    *,
    end_i: int,
    from_start: bool = True,
    tol: float = 0.75,
) -> dict[str, Any] | None:
    """
    Re-fit support as price progresses. Accept a new line when it still
    touches bottoms and uses a newer second pivot (or same with better span).
    Keeps prior TL if the refit is weaker / invalid.
    """
    if end_i < 2 or end_i >= len(px_hist):
        return tl
    fresh = fit_up_trendline(
        px_hist, break_i=end_i, from_start=from_start, tol=tol
    )
    px_now = float(px_hist[end_i])
    if not _tl_in_hist_space(tl, end_i + 1, px_now):
        # Drop corrupt/wrong-index prior even if a fresh fit is unavailable
        return fresh
    if not fresh:
        return tl
    assert tl is not None
    # Prefer correction that advances the second bottom or keeps first touch
    old_i1 = int(tl.get("i1") if tl.get("i1") is not None else tl.get("break_i") or 0)
    new_i1 = int(fresh.get("i1") or 0)
    old_i0 = int(tl.get("i0") or 0)
    new_i0 = int(fresh.get("i0") or 0)
    # Always take if second pivot moved forward (new higher-low touch)
    if new_i1 > old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    # Or first pivot refined earlier (deeper day base) with valid second
    if new_i0 < old_i0 and new_i1 >= old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    # Same pivots — refresh slope/anchors from prices (series may densify)
    if new_i0 == old_i0 and new_i1 == old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    return tl


def correct_down_trendline(
    tl: dict[str, Any] | None,
    px_hist: list[float],
    *,
    end_i: int,
    from_start: bool = True,
    tol: float = 0.75,
) -> dict[str, Any] | None:
    """Re-fit falling resistance onto newer swing tops as price progresses."""
    if end_i < 2 or end_i >= len(px_hist):
        return tl
    fresh = fit_down_trendline(
        px_hist, break_i=end_i, from_start=from_start, tol=tol
    )
    px_now = float(px_hist[end_i])
    if not _tl_in_hist_space(tl, end_i + 1, px_now):
        return fresh
    if not fresh:
        return tl
    assert tl is not None
    old_i1 = int(tl.get("i1") if tl.get("i1") is not None else tl.get("break_i") or 0)
    new_i1 = int(fresh.get("i1") or 0)
    old_i0 = int(tl.get("i0") or 0)
    new_i0 = int(fresh.get("i0") or 0)
    if new_i1 > old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    if new_i0 < old_i0 and new_i1 >= old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    if new_i0 == old_i0 and new_i1 == old_i1:
        return {**fresh, "index_space": tl.get("index_space")}
    return tl


def trendline_value(tl: dict[str, Any] | None, i: int) -> float | None:
    """Project TL at index i. Prefers anchor_fi/anchor_y when set (frame space)."""
    if not tl:
        return None
    if tl.get("anchor_fi") is not None and tl.get("anchor_y") is not None:
        return float(tl["anchor_y"]) + float(tl["slope"]) * (
            int(i) - int(tl["anchor_fi"])
        )
    return float(tl["intercept"]) + float(tl["slope"]) * int(i)


def bind_tl_to_frame(
    tl: dict[str, Any] | None,
    *,
    frame_i: int,
    hist_i: int,
) -> dict[str, Any] | None:
    """
    Re-anchor a hist-index TL onto absolute frame index for chart/exits.

    Preserves both pivot touches (i0/y0 and i1/y1) in frame space — slope is
    always from bottom-to-bottom (or top-to-top), never forced through break.
    """
    if not tl:
        return None
    hist_i0 = int(tl.get("i0") or 0)
    hist_i1 = int(tl.get("i1") if tl.get("i1") is not None else hist_i)
    # Map hist indices → frame by aligning hist_i with frame_i
    shift = int(frame_i) - int(hist_i)
    fi0 = hist_i0 + shift
    fi1 = hist_i1 + shift
    y0 = float(tl.get("y0") or 0)
    y1 = float(tl.get("y1") if tl.get("y1") is not None else y0)
    span = max(1, fi1 - fi0)
    slope = (y1 - y0) / span
    if tl.get("kind") == "up":
        slope = max(0.0, slope) or 0.005
    else:
        slope = min(0.0, slope) or -0.005
    return {
        **tl,
        "i0": fi0,
        "y0": round(y0, 2),
        "i1": fi1,
        "y1": round(y1, 2),
        "anchor_fi": fi0,
        "anchor_y": round(y0, 2),
        "slope": round(float(slope), 5),
        "intercept": round(y0 - float(slope) * fi0, 3),
    }


def winner_broke_up_tl(
    px: float,
    tl: dict[str, Any] | None,
    i: int,
    *,
    buffer: float = 1.5,
    armed: bool = True,
) -> bool:
    """True when winner has broken below rising support."""
    if not tl or not armed:
        return False
    lvl = trendline_value(tl, i)
    if lvl is None:
        return False
    return float(px) < float(lvl) - float(buffer)


def fallen_broke_down_tl(
    px: float,
    tl: dict[str, Any] | None,
    i: int,
    *,
    buffer: float = 1.5,
    armed: bool = True,
) -> bool:
    """True when fallen has broken above falling resistance (retrace)."""
    if not tl or not armed:
        return False
    lvl = trendline_value(tl, i)
    if lvl is None:
        return False
    return float(px) > float(lvl) + float(buffer)

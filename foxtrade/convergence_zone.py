"""
Detect the real CE/PE convergence range from history.

Screening band (config premium_low/high) only picks which strikes to watch.
The convergence range is where CE and PE actually overlap / trade tightly
together — not a flat box: as theta decays the merge forms a *pipe* that
tilts down to the right (upper/mid/lower rails share one slope).
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _slope(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = mean(xs), mean(vals)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    return num / den


def _fit_line(vals: list[float]) -> tuple[float, float]:
    """Return (intercept, slope) for vals[i] ≈ a + b*i."""
    n = len(vals)
    if n < 2:
        v = float(vals[0]) if vals else 0.0
        return v, 0.0
    b = _slope(vals) or 0.0
    a = mean(vals) - b * mean(range(n))
    return a, b


def _local_pivots(vals: list[float], *, kind: str, order: int = 2) -> list[tuple[int, float]]:
    """Local highs (kind=high) or lows (kind=low) with `order` bars each side."""
    n = len(vals)
    out: list[tuple[int, float]] = []
    if n < 2 * order + 1:
        # too short — use endpoints + mid
        for i in (0, n // 2, n - 1):
            if 0 <= i < n:
                out.append((i, vals[i]))
        return out
    for i in range(order, n - order):
        w = vals[i - order : i + order + 1]
        if kind == "high" and vals[i] >= max(w) - 1e-9:
            out.append((i, vals[i]))
        elif kind == "low" and vals[i] <= min(w) + 1e-9:
            out.append((i, vals[i]))
    if len(out) < 2:
        out = [(0, vals[0]), (n - 1, vals[-1])]
    return out


def _fit_line_through_pivots(pivots: list[tuple[int, float]], n: int) -> tuple[float, float]:
    if len(pivots) < 2:
        y = pivots[0][1] if pivots else 0.0
        return y, 0.0
    xs = [float(i) for i, _ in pivots]
    ys = [float(v) for _, v in pivots]
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    b = num / den
    a = my - b * mx
    return a, b


def _fit_pipe(
    ce_seg: list[float],
    pe_seg: list[float],
    *,
    pad_frac: float = 0.08,
    min_half: float = 2.0,
    mode: str = "band",
) -> dict[str, Any]:
    """
    Fit convergence rails for chart + breakout.

    band (default): parallel channel (shared slope, often flat) around merge mid —
    matches hand-drawn white top/bottom rails at the lock, not a megaphone.

    pipe: mid ± half_width with shared slope (same idea as band).

    wedge: separate upper/lower trendlines (can fan into a megaphone — avoid for plot).
    """
    n = min(len(ce_seg), len(pe_seg))
    hi = [max(ce_seg[i], pe_seg[i]) for i in range(n)]
    lo = [min(ce_seg[i], pe_seg[i]) for i in range(n)]
    mids = [(ce_seg[i] + pe_seg[i]) / 2.0 for i in range(n)]
    a_mid, b_mid = _fit_line(mids)

    if mode in ("band", "pipe", "channel"):
        # Nearly flat parallel band (user's correct white rails)
        if mode == "band" and abs(b_mid) < 0.08:
            b_mid = 0.0
            a_mid = sum(mids) / float(n)
        fitted = [a_mid + b_mid * i for i in range(n)]
        exc: list[float] = []
        for i in range(n):
            exc.append(abs(ce_seg[i] - fitted[i]))
            exc.append(abs(pe_seg[i] - fitted[i]))
            exc.append(abs(hi[i] - fitted[i]))
            exc.append(abs(lo[i] - fitted[i]))
        exc_s = sorted(exc) if exc else [min_half]
        p90 = exc_s[min(len(exc_s) - 1, int(len(exc_s) * 0.90))]
        # Envelope of merge prints (top≈max CE/PE, bottom≈min) with small pad
        env_half = max(
            min_half,
            (max(hi) - min(lo)) / 2.0,
            p90 * (1.0 + pad_frac),
        )
        half = float(env_half)
        a_hi, b_hi = a_mid + half, b_mid
        a_lo, b_lo = a_mid - half, b_mid
    else:
        # Wedge rails: linreg on local highs of envelope + local lows
        piv_hi = _local_pivots(hi, kind="high", order=max(1, min(3, n // 8 or 1)))
        piv_lo = _local_pivots(lo, kind="low", order=max(1, min(3, n // 8 or 1)))
        a_hi, b_hi = _fit_line_through_pivots(piv_hi, n)
        a_lo, b_lo = _fit_line_through_pivots(piv_lo, n)
        # Soft pad so rails sit just outside typical prints
        pad = max(0.5, min_half * pad_frac)
        a_hi += pad
        a_lo -= pad
        # Ensure high stays above low along the window
        for i in (0, n - 1):
            if (a_hi + b_hi * i) < (a_lo + b_lo * i) + 1.0:
                mid_i = a_mid + b_mid * i
                a_hi = mid_i + min_half - b_hi * i
                a_lo = mid_i - min_half - b_lo * i
                break
        half = max(
            min_half,
            abs((a_hi + b_hi * (n - 1)) - (a_lo + b_lo * (n - 1))) / 2.0,
        )

    high0 = a_hi
    high1 = a_hi + b_hi * (n - 1)
    low0 = a_lo
    low1 = a_lo + b_lo * (n - 1)
    mid0, mid1 = a_mid, a_mid + b_mid * (n - 1)
    # Converging if rail gap shrinks toward the right
    gap0 = high0 - low0
    gap1 = high1 - low1
    converging = gap1 < gap0 * 0.92
    tilt = "down" if b_mid < -0.01 else ("up" if b_mid > 0.01 else "flat")
    return {
        "mode": mode,
        "slope": round(b_mid, 4),
        "intercept": round(a_mid, 3),
        "high_slope": round(b_hi, 4),
        "high_intercept": round(a_hi, 3),
        "low_slope": round(b_lo, 4),
        "low_intercept": round(a_lo, 3),
        "half_width": round(half, 2),
        "mid0": round(mid0, 2),
        "mid1": round(mid1, 2),
        "high0": round(high0, 2),
        "high1": round(high1, 2),
        "low0": round(low0, 2),
        "low1": round(low1, 2),
        "tilt": tilt,
        "converging": converging,
        "n": n,
    }


def _pipe_points_for_window(
    times: list[Any],
    ce_seg: list[float],
    pe_seg: list[float],
    pipe: dict[str, Any],
) -> list[dict[str, Any]]:
    """Dense rail points along the merge window (for Lightweight Charts)."""
    n = min(len(times), len(ce_seg), len(pe_seg), int(pipe.get("n") or 0) or len(ce_seg))
    if n < 2:
        return []
    # Prefer independent high/low rails (wedge); fall back to mid±hw
    if pipe.get("high_intercept") is not None and pipe.get("low_intercept") is not None:
        a_hi = float(pipe["high_intercept"])
        b_hi = float(pipe.get("high_slope") or 0.0)
        a_lo = float(pipe["low_intercept"])
        b_lo = float(pipe.get("low_slope") or 0.0)
        a_mid = float(pipe.get("intercept") or (a_hi + a_lo) / 2)
        b_mid = float(pipe.get("slope") or (b_hi + b_lo) / 2)
        out: list[dict[str, Any]] = []
        for i in range(n):
            hi = a_hi + b_hi * i
            lo = a_lo + b_lo * i
            mid = a_mid + b_mid * i
            out.append(
                {
                    "time": times[i],
                    "high": round(hi, 2),
                    "mid": round(mid, 2),
                    "low": round(lo, 2),
                }
            )
        return out
    a = float(pipe["intercept"])
    b = float(pipe["slope"])
    hw = float(pipe["half_width"])
    out = []
    for i in range(n):
        mid = a + b * i
        out.append(
            {
                "time": times[i],
                "high": round(mid + hw, 2),
                "mid": round(mid, 2),
                "low": round(mid - hw, 2),
            }
        )
    return out


def project_rails_at(
    pipe: dict[str, Any] | None,
    *,
    rel_i: int,
    clamp_rel: int | None = None,
) -> dict[str, float] | None:
    """Project high/mid/low rails at index rel_i from pipe start (for live break).

    clamp_rel: if set, do not project past this relative index (flat-extend
    last merge rail — stops converging wedges from crossing on the chart).
    """
    if not pipe:
        return None
    ri = int(rel_i)
    if clamp_rel is not None:
        ri = min(ri, max(0, int(clamp_rel)))
    if pipe.get("high_intercept") is not None:
        hi = float(pipe["high_intercept"]) + float(pipe.get("high_slope") or 0) * ri
        lo = float(pipe["low_intercept"]) + float(pipe.get("low_slope") or 0) * ri
        mid = float(pipe.get("intercept") or (hi + lo) / 2) + float(
            pipe.get("slope") or 0
        ) * ri
    else:
        mid = float(pipe.get("intercept") or 0) + float(pipe.get("slope") or 0) * ri
        hw = float(pipe.get("half_width") or 4)
        hi, lo = mid + hw, mid - hw
    # Wedge slopes can cross when over-projected — keep a valid channel
    if hi < lo:
        hi, lo = lo, hi
    mid = min(hi, max(lo, mid))
    return {"high": hi, "mid": mid, "low": lo}


def detect_convergence_zone(
    ce_hist: list[float],
    pe_hist: list[float],
    *,
    gap_tol: float = 12.0,
    min_samples: int = 15,
    times: list[Any] | None = None,
    max_soft_breach_pct: float = 0.12,
    max_soft_breach_floor: int = 8,
    max_soft_breach_cap: int = 40,
    max_hard_bridge: int = 2,
    rail_mode: str = "band",
) -> dict[str, Any] | None:
    """
    Find best sustained window where CE≈PE, return parallel band envelope.

    Merge can last 1 minute → ~2 hours. Soft gap wiggles and 1–2 bar
    hard spikes must NOT chop the visual pipe into fragments.

    Returns e.g. {low, high, mid, pipe, pipe_points, samples, active, ...}
    Flat low/high kept for legacy checks; pipe_* is the real drawn range.
    """
    n = min(len(ce_hist), len(pe_hist))
    if n < min_samples:
        return None

    best: dict[str, Any] | None = None
    best_score = -1.0
    hard_gap = gap_tol * 1.85  # hard cut — true split, not a wiggle
    i = 0
    while i < n:
        if abs(ce_hist[i] - pe_hist[i]) > gap_tol:
            i += 1
            continue
        j = i
        soft_breaches = 0
        hard_bridge = 0
        while j < n:
            g = abs(ce_hist[j] - pe_hist[j])
            length_so_far = j - i + 1
            # Soft-breach budget grows with pipe length (supports multi-hour merge)
            max_soft = min(
                max_soft_breach_cap,
                max(
                    max_soft_breach_floor,
                    int(length_so_far * max_soft_breach_pct),
                ),
            )
            if g <= gap_tol:
                hard_bridge = 0
                j += 1
                continue
            if g > hard_gap:
                # Brief hard spike bridge (1–2 bars) if merge resumes after
                if hard_bridge < max_hard_bridge and j + 1 < n:
                    # look ahead: next bar back under hard?
                    nxt = abs(ce_hist[j + 1] - pe_hist[j + 1])
                    if nxt <= hard_gap:
                        hard_bridge += 1
                        soft_breaches += 1  # count toward soft budget too
                        if soft_breaches > max_soft:
                            break
                        j += 1
                        continue
                break
            # soft breach — keep pipe if within growing budget
            hard_bridge = 0
            if soft_breaches >= max_soft:
                break
            soft_breaches += 1
            j += 1
        # trim trailing soft/hard-breach bars so rails end on a tight print
        while j > i and abs(ce_hist[j - 1] - pe_hist[j - 1]) > gap_tol:
            j -= 1
        length = j - i
        if length >= min_samples:
            ce_seg = ce_hist[i:j]
            pe_seg = pe_hist[i:j]
            gaps = [abs(ce_seg[k] - pe_seg[k]) for k in range(length)]
            pipe = _fit_pipe(ce_seg, pe_seg, mode=rail_mode)
            # Prefer the ACTIVE merge ending near "now" (1m–2h), not a later/earlier fragment
            tilt_bonus = 6.0 if (pipe.get("tilt") == "down") else (
                2.0 if pipe.get("tilt") == "flat" else 0.0
            )
            if pipe.get("converging"):
                tilt_bonus += 4.0
            ends_now = j >= n - 2
            active_bonus = 80.0 if ends_now else 0.0
            score = (
                length * 2.5
                - (sum(gaps) / length)
                + active_bonus
                + min(15.0, (j / n) * 8)
                + tilt_bonus
            )
            if score > best_score:
                best_score = score
                t_seg = times[i:j] if times and len(times) >= j else None
                pipe_pts = (
                    _pipe_points_for_window(t_seg, ce_seg, pe_seg, pipe)
                    if t_seg
                    else []
                )
                best = {
                    "low": round(min(pipe["low0"], pipe["low1"]), 1),
                    "high": round(max(pipe["high0"], pipe["high1"]), 1),
                    "mid": round((pipe["mid0"] + pipe["mid1"]) / 2, 1),
                    "samples": length,
                    "gap_avg": round(sum(gaps) / length, 2),
                    "start_i": i,
                    "end_i": j - 1,
                    "gap_tol": gap_tol,
                    "breaches": soft_breaches,
                    "duration_min": length,  # 1 sample ≈ 1m in playback; live may differ
                    "source": "detected",
                    "pipe": pipe,
                    "pipe_points": pipe_pts,
                }
        i = max(j, i + 1)

    if not best:
        # Fallback: use recent samples where gap was relatively tight (best 20%)
        pairs = [(ce_hist[k], pe_hist[k], abs(ce_hist[k] - pe_hist[k])) for k in range(n)]
        pairs.sort(key=lambda x: x[2])
        take = max(min_samples, n // 5)
        tight = pairs[:take]
        if not tight:
            return None
        vals = [p for a, b, _ in tight for p in (a, b)]
        gap_avg = sum(g for _, _, g in tight) / len(tight)
        if gap_avg > gap_tol * 2:
            return None
        # Rebuild chronological segment from last min_samples tight-ish bars
        ce_seg = ce_hist[-min_samples:]
        pe_seg = pe_hist[-min_samples:]
        pipe = _fit_pipe(ce_seg, pe_seg, mode=rail_mode)
        t_seg = times[-min_samples:] if times and len(times) >= min_samples else None
        pipe_pts = (
            _pipe_points_for_window(t_seg, ce_seg, pe_seg, pipe) if t_seg else []
        )
        best = {
            "low": round(min(pipe["low0"], pipe["low1"]), 1),
            "high": round(max(pipe["high0"], pipe["high1"]), 1),
            "mid": round((pipe["mid0"] + pipe["mid1"]) / 2, 1),
            "samples": len(tight),
            "gap_avg": round(gap_avg, 2),
            "start_i": max(0, n - min_samples),
            "end_i": n - 1,
            "gap_tol": gap_tol,
            "source": "detected_tightest",
            "pipe": pipe,
            "pipe_points": pipe_pts,
        }

    last_gap = abs(ce_hist[-1] - pe_hist[-1])
    last_mid = (ce_hist[-1] + pe_hist[-1]) / 2
    pipe = best.get("pipe") or {}
    # Active if still tight and near the *tilted* mid rail (not flat box alone)
    hw = float(pipe.get("half_width") or ((best["high"] - best["low"]) / 2))
    a = float(pipe.get("intercept") or best["mid"])
    b = float(pipe.get("slope") or 0.0)
    end_i = int(best.get("end_i") or (n - 1))
    start_i = int(best.get("start_i") or 0)
    # Project fitted mid at latest bar relative to pipe start
    rel = max(0, (n - 1) - start_i)
    fitted_now = a + b * min(rel, max(0, end_i - start_i))
    near_pipe = abs(last_mid - fitted_now) <= hw + 2.0
    best["active"] = last_gap <= gap_tol and (
        near_pipe or (best["low"] - 2 <= last_mid <= best["high"] + 2)
    )
    best["last_gap"] = round(last_gap, 2)
    return best


def pipe_from_live_series(
    series: list[dict[str, Any]],
    *,
    gap_tol: float = 12.0,
    min_samples: int = 8,
    max_soft_breach_pct: float = 0.12,
    max_soft_breach_floor: int = 8,
    max_hard_bridge: int = 2,
    rail_mode: str = "band",
) -> dict[str, Any] | None:
    """Build chart pipe from dashboard live_series [{time, ce, pe}, ...]."""
    if not series or len(series) < min_samples:
        return None
    ce = [float(p["ce"]) for p in series]
    pe = [float(p["pe"]) for p in series]
    times = [p["time"] for p in series]
    return detect_convergence_zone(
        ce,
        pe,
        gap_tol=gap_tol,
        min_samples=min_samples,
        times=times,
        max_soft_breach_pct=max_soft_breach_pct,
        max_soft_breach_floor=max_soft_breach_floor,
        max_hard_bridge=max_hard_bridge,
        rail_mode=rail_mode,
    )


def detect_entry_above_convergence(
    *,
    ce: float | None,
    pe: float | None,
    conv_mid: float | None,
    pts: float,
) -> dict[str, Any]:
    """
    Entry when the expansion leader prints ≥ last convergence mid + pts
    (default 12). Instant fire — the +pts move is the confirmation.
    """
    if conv_mid is None or pts is None or float(pts) <= 0:
        return {"ok": False, "side": None, "reason": "no conv mid", "threshold": None}
    thr = float(conv_mid) + float(pts)
    ce_hit = ce is not None and float(ce) >= thr
    pe_hit = pe is not None and float(pe) >= thr
    if ce_hit and (pe is None or float(ce) >= float(pe)):
        return {
            "ok": True,
            "side": "CE",
            "px": float(ce),
            "threshold": thr,
            "reason": f"CE {float(ce):.1f}≥conv {float(conv_mid):.0f}+{float(pts):g}",
        }
    if pe_hit and (ce is None or float(pe) >= float(ce)):
        return {
            "ok": True,
            "side": "PE",
            "px": float(pe),
            "threshold": thr,
            "reason": f"PE {float(pe):.1f}≥conv {float(conv_mid):.0f}+{float(pts):g}",
        }
    lead = None
    lead_px = None
    if ce is not None and pe is not None:
        if float(ce) >= float(pe):
            lead, lead_px = "CE", float(ce)
        else:
            lead, lead_px = "PE", float(pe)
    elif ce is not None:
        lead, lead_px = "CE", float(ce)
    elif pe is not None:
        lead, lead_px = "PE", float(pe)
    need = thr - float(lead_px) if lead_px is not None else None
    return {
        "ok": False,
        "side": lead,
        "threshold": thr,
        "reason": (
            f"wait {lead or '?'} ≥{thr:.0f} (conv+{float(pts):g}"
            + (f", need +{need:.1f}" if need is not None else "")
            + ")"
        ),
    }


def detect_rail_breakout(
    *,
    zone: dict[str, Any] | None,
    ce: float | None,
    pe: float | None,
    hist_len: int,
    buffer: float = 1.0,
    confirm_points: int = 0,
    confirm_side: str | None = None,
    confirm_count: int = 0,
    winner_px_hist: list[float] | None = None,
    hard_gap: float | None = None,
    force_side: str | None = None,
) -> dict[str, Any]:
    """
    Breakout = winner punches the *upper* convergence rail (uphill),
    loser slides toward the lower rail (hill slope).

    confirm_points: if >0, require that many successive higher prints on winner
    after first rail/hard touch. HARD gap must also pass this confirm
    (do not instant-buy on hard gap — that skipped +3 on 11-Nov).

    ce/pe may be None if one leg quote is missing (still allow other side).
    force_side: hard-gap leader to arm confirm without rail touch.
    """
    empty = {
        "ok": False,
        "side": None,
        "pending": False,
        "reason": "no zone",
        "confirm_count": 0,
        "rails": None,
    }
    rails = None
    upper = lower = mid = None
    if zone:
        pipe = zone.get("pipe") or {}
        start_i = int(zone.get("start_i") or 0)
        rel = max(0, hist_len - 1 - start_i)
        rails = project_rails_at(pipe, rel_i=rel)
        if rails:
            upper = float(rails["high"]) + float(buffer)
            lower = float(rails["low"]) - float(buffer)
            mid = float(rails["mid"])

    gap = None
    if ce is not None and pe is not None:
        gap = abs(ce - pe)
    # Strong arm only: hard gap OR (above upper rail AND gap already open)
    min_gap_arm = float(hard_gap or 12.0) * 0.75

    def _above(side_x: str) -> bool:
        if upper is None:
            return False
        px_ = ce if side_x == "CE" else pe
        return px_ is not None and px_ >= upper

    side: str | None = None
    arm_reason = ""
    # Continue an in-progress confirm only if still above rail (or no rails)
    if confirm_side in ("CE", "PE") and confirm_count > 0:
        if upper is None or _above(confirm_side):
            side = confirm_side
            arm_reason = "confirming"
        else:
            return {
                "ok": False,
                "side": None,
                "pending": False,
                "reason": f"abort {confirm_side} back inside rail",
                "confirm_count": 0,
                "rails": rails,
                "aborted": True,
            }

    if side is None and force_side in ("CE", "PE"):
        # Hard gap arms — still needs +N up-points
        side = force_side
        arm_reason = f"hard gap arm {force_side}"

    if side is None and upper is not None:
        # Fresh rail arm only if gap is already opening (avoid merge-chop fakes)
        gap_ok = gap is None or gap >= min_gap_arm
        if gap_ok and ce is not None and ce >= upper and (pe is None or ce >= (pe or 0)):
            side = "CE"
            arm_reason = f"rail CE≥{upper:.1f}"
        elif gap_ok and pe is not None and pe >= upper and (ce is None or pe >= (ce or 0)):
            side = "PE"
            arm_reason = f"rail PE≥{upper:.1f}"

    if side is None:
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": (
                f"inside rails u={upper:.1f} l={lower:.1f}"
                if upper is not None
                else "waiting arm"
            ),
            "confirm_count": 0,
            "rails": rails,
        }

    px = ce if side == "CE" else pe
    if px is None:
        return {
            "ok": False,
            "side": side,
            "pending": True,
            "reason": f"armed {side} — waiting quote",
            "confirm_count": confirm_count if confirm_side == side else 0,
            "rails": rails,
        }

    need = int(confirm_points or 0)
    if need <= 0:
        return {
            "ok": True,
            "side": side,
            "pending": False,
            "reason": arm_reason or f"break {side}",
            "confirm_count": 0,
            "rails": rails,
        }

    # Must stay above upper rail during confirm
    if upper is not None and px < upper:
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": f"abort {side} lost rail {px:.1f}<{upper:.1f}",
            "confirm_count": 0,
            "rails": rails,
            "aborted": True,
        }

    # Confirm: successive higher closes on winner + net +N pts from arm
    hist = list(winner_px_hist or [])
    if confirm_side != side:
        count = 1
        hist = []  # never reuse other-side prints for net thrust
    elif not hist:
        count = 1
    elif px > hist[-1] + 0.05:
        count = int(confirm_count) + 1
    elif px < hist[-1] - 0.8:
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": f"abort {side} pullback {px:.1f}<{hist[-1]:.1f}",
            "confirm_count": 0,
            "rails": rails,
            "aborted": True,
        }
    else:
        count = max(int(confirm_count), 1)

    arm_px = float(hist[0]) if hist else float(px)
    net_up = float(px) - arm_px
    need_net = float(need)  # e.g. +3 pts over the confirm window
    # If both legs quoted, gap must stay open during confirm
    if gap is not None and gap < min_gap_arm and count > 1:
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": f"abort {side} gap collapsed {gap:.1f}",
            "confirm_count": 0,
            "rails": rails,
            "aborted": True,
        }

    if count >= need and net_up >= need_net:
        return {
            "ok": True,
            "side": side,
            "pending": False,
            "reason": f"{arm_reason or side} +{count} up / net {net_up:+.1f}",
            "confirm_count": count,
            "rails": rails,
        }
    return {
        "ok": False,
        "side": side,
        "pending": True,
        "reason": (
            f"{side} confirm {count}/{need} net {net_up:+.1f}/{need_net:g} "
            f"({arm_reason})"
        ),
        "confirm_count": count,
        "rails": rails,
    }


def detect_rail_reversal(
    *,
    breakout_side: str,
    zone: dict[str, Any] | None,
    ce: float | None,
    pe: float | None,
    hist_len: int,
    buffer: float = 1.0,
    confirm_points: int = 0,
    confirm_side: str | None = None,
    confirm_count: int = 0,
    winner_px_hist: list[float] | None = None,
    hard_gap: float | None = None,
) -> dict[str, Any]:
    """
    Opposite-rail reversal while long `breakout_side`.

    Mirror of detect_rail_breakout: only the opposite leg may arm (upper rail
    or hard-gap leader) and must complete the same +N confirm. On ok → book
    breakout and flip to opposite.
    """
    idle = {
        "ok": False,
        "side": None,
        "pending": False,
        "reason": "no breakout side",
        "confirm_count": 0,
        "rails": None,
        "aborted": False,
    }
    if breakout_side not in ("CE", "PE"):
        return idle
    opp = "PE" if breakout_side == "CE" else "CE"

    rails = None
    upper = None
    if zone:
        pipe = zone.get("pipe") or {}
        start_i = int(zone.get("start_i") or 0)
        rel = max(0, hist_len - 1 - start_i)
        rails = project_rails_at(pipe, rel_i=rel)
        if rails:
            upper = float(rails["high"]) + float(buffer)

    gap = None
    leader = None
    if ce is not None and pe is not None:
        gap = abs(ce - pe)
        leader = "CE" if ce >= pe else "PE"
    hard = float(hard_gap or 12.0)
    min_gap_arm = hard * 0.75
    opp_px = ce if opp == "CE" else pe

    continuing = confirm_side == opp and int(confirm_count or 0) > 0
    rail_arm = (
        upper is not None
        and opp_px is not None
        and opp_px >= upper
        and (gap is None or gap >= min_gap_arm)
    )
    hard_arm = (
        gap is not None
        and gap >= hard
        and leader == opp
    )
    if not (continuing or rail_arm or hard_arm):
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": f"waiting reverse {opp}",
            "confirm_count": 0,
            "rails": rails,
            "aborted": False,
        }

    rb = detect_rail_breakout(
        zone=zone,
        ce=ce,
        pe=pe,
        hist_len=hist_len,
        buffer=buffer,
        confirm_points=confirm_points,
        confirm_side=opp if continuing else None,
        confirm_count=int(confirm_count or 0) if continuing else 0,
        winner_px_hist=winner_px_hist if continuing else None,
        hard_gap=hard_gap,
        force_side=opp,
    )
    # Only opposite may win; ignore accidental same-side arm
    if rb.get("side") and rb["side"] != opp:
        return {
            "ok": False,
            "side": None,
            "pending": False,
            "reason": f"waiting reverse {opp}",
            "confirm_count": 0,
            "rails": rb.get("rails") or rails,
            "aborted": False,
        }
    reason = str(rb.get("reason") or "")
    if rb.get("ok"):
        reason = f"rail reverse → {opp} ({reason})"
    elif rb.get("aborted"):
        reason = f"abort reverse {reason}"
    elif rb.get("pending"):
        reason = f"reverse {reason}"
    return {
        "ok": bool(rb.get("ok")),
        "side": rb.get("side"),
        "pending": bool(rb.get("pending")),
        "reason": reason,
        "confirm_count": int(rb.get("confirm_count") or 0),
        "rails": rb.get("rails") or rails,
        "aborted": bool(rb.get("aborted")),
    }


def in_convergence_zone(
    ce_ltp: float,
    pe_ltp: float,
    zone: dict[str, Any] | None,
    *,
    entry_tol: float = 3.0,
) -> bool:
    """True when CE/PE are tight and mid sits inside detected (tilted) pipe."""
    if not zone:
        return False
    mid = (ce_ltp + pe_ltp) / 2
    gap_ok = abs(ce_ltp - pe_ltp) <= float(entry_tol)
    pipe = zone.get("pipe") or {}
    if pipe:
        hw = float(pipe.get("half_width") or 0)
        # Prefer latest rail mid if pipe_points exist
        pts = zone.get("pipe_points") or []
        if pts:
            fitted = float(pts[-1]["mid"])
        else:
            fitted = float(pipe.get("mid1") or zone.get("mid") or mid)
        in_pipe = abs(mid - fitted) <= hw + 1.5
        either_in = abs(ce_ltp - fitted) <= hw + 1.5 or abs(pe_ltp - fitted) <= hw + 1.5
        return gap_ok and (in_pipe or either_in)
    in_box = float(zone["low"]) <= mid <= float(zone["high"])
    either_in = (
        float(zone["low"]) <= ce_ltp <= float(zone["high"])
        or float(zone["low"]) <= pe_ltp <= float(zone["high"])
    )
    return gap_ok and (in_box or either_in)


def _minute_closes(token: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    from kite_client import get_authenticated_kite

    kite = get_authenticated_kite()
    rows = kite.historical_data(int(token), start, end, "minute")
    out: list[tuple[datetime, float]] = []
    for row in rows:
        dt = row["date"]
        if getattr(dt, "tzinfo", None):
            dt = dt.astimezone(IST)
        out.append((dt, float(row["close"])))
    return out


def detect_zone_from_day_history(
    ce_token: int,
    pe_token: int,
    *,
    gap_tol: float = 12.0,
    min_samples: int = 15,
    live_ce: list[float] | None = None,
    live_pe: list[float] | None = None,
) -> dict[str, Any] | None:
    """
    Prefer live series; if already broken out, dig 09:15→now minute bars
    so the morning overlap pipe is still recoverable.
    """
    z = detect_convergence_zone(
        live_ce or [], live_pe or [], gap_tol=gap_tol, min_samples=min_samples
    )
    if z and z.get("source") == "detected":
        return z

    now = datetime.now(IST)
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < start:
        return z
    try:
        ce_rows = _minute_closes(ce_token, start, now)
        pe_rows = _minute_closes(pe_token, start, now)
    except Exception:
        return z

    pe_map = {dt.replace(second=0, microsecond=0): px for dt, px in pe_rows}
    ce_h: list[float] = []
    pe_h: list[float] = []
    for dt, px in ce_rows:
        key = dt.replace(second=0, microsecond=0)
        if key in pe_map:
            ce_h.append(px)
            pe_h.append(pe_map[key])

    dug = detect_convergence_zone(ce_h, pe_h, gap_tol=gap_tol, min_samples=min_samples)
    if dug:
        dug["source"] = "detected_day"
        dug["day_samples"] = len(ce_h)
        return dug
    return z or dug

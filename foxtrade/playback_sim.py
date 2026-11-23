"""
Bar-by-bar Convergence playback — no lookahead.

Each minute only sees closes ≤ that minute. Follows live playbook:
  ATM+diag gate → lock 125–145 pair → dual → lean/candle → breakout
  → book 165–175 / fallen reverse-hammer break / stretch 2.5×
  → fallen hold exits

Input: data/option_chain_1m/<day>/viewer.json + NIFTY_SPOT_1m.csv
Output: frame stream + trades for playback dashboard / daily HTML.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foxtrade.atm_diag_merge_lock import try_lock_pair
from foxtrade.charges import estimate_option_charges
from foxtrade.convergence_zone import (
    detect_convergence_zone,
    detect_entry_above_convergence,
    detect_rail_breakout,
    detect_rail_reversal,
)
from foxtrade.early_winner_lean import detect_early_winner_lean, spot_1m_candle_side
from foxtrade.breakout_trendline import (
    correct_down_trendline,
    correct_up_trendline,
    fallen_broke_down_tl,
    fit_down_trendline,
    fit_premium_channel,
    fit_up_trendline,
    trendline_value,
    winner_broke_up_tl,
)
from foxtrade.premium_exit_signals import (
    build_ohlc_bars,
    detect_fallen_reversal_candle,
    fallen_reversal_broken,
    dual_book_winner,
    premium_target_hit,
)

ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "data" / "option_chain_1m"


@dataclass
class Leg:
    side: str
    role: str
    entry_t: str
    entry: float
    lots: int
    symbol: str = ""
    strike: int | None = None
    exit_t: str | None = None
    exit: float | None = None
    reason: str = ""

    def qty(self, lot_size: int) -> int:
        return self.lots * lot_size

    def pnl(self, lot_size: int) -> float:
        if self.exit is None:
            return 0.0
        return (self.exit - self.entry) * self.qty(lot_size)


@dataclass
class SimState:
    phase: str = "SCAN"
    ce_sym: str | None = None
    pe_sym: str | None = None
    ce_strike: int | None = None
    pe_strike: int | None = None
    ce_entry: float | None = None
    pe_entry: float | None = None
    mid_at_entry: float | None = None
    breakout_side: str | None = None
    fallen_side: str | None = None
    breakout_peak: float | None = None
    breakout_t: str | None = None
    fallen_low: float | None = None
    lean_side: str | None = None
    lean_added: bool = False
    fallen_rev_arm: dict[str, Any] | None = None
    message: str = ""
    locked: bool = False


def _load_day(day: str) -> tuple[dict, list[dict], dict[str, dict]]:
    d = CHAIN / day
    viewer = json.loads((d / "viewer.json").read_text(encoding="utf-8"))
    spot: dict[str, dict] = {}
    with (d / "NIFTY_SPOT_1m.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = r["datetime"][11:16]
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            spot[t] = {
                "o": o,
                "h": h,
                "l": l,
                "c": c,
                "color": "green" if c > o else ("red" if c < o else "doji"),
            }
    return viewer.get("meta") or {}, list(viewer.get("frames") or []), spot


def _leg_px(frame: dict, side: str, strike: int) -> float | None:
    row = (frame.get("legs") or {}).get(f"{side}:{strike}")
    if not row:
        return None
    return float(row["c"])


def _leg_ohlc(frame: dict, side: str, strike: int) -> dict[str, Any] | None:
    """Real 1m OHLC for fallen-reversal candles (close-only ticks cannot form hammers)."""
    row = (frame.get("legs") or {}).get(f"{side}:{strike}")
    if not row:
        return None
    try:
        o = float(row.get("o", row.get("c")))
        h = float(row.get("h", o))
        l = float(row.get("l", o))
        c = float(row.get("c", o))
    except (TypeError, ValueError):
        return None
    return {"o": o, "h": h, "l": l, "c": c, "t1": frame.get("t")}


def _close_side(legs: list[Leg], side: str, t: str, px: float, reason: str) -> None:
    for L in legs:
        if L.exit is not None:
            continue
        if L.side != side:
            continue
        L.exit_t, L.exit, L.reason = t, px, reason


def _best_band_chart_pair(
    frames: list[dict],
    *,
    band_lo: float,
    band_hi: float,
    gap_tol: float = 12.0,
) -> tuple[int, int] | None:
    """
    Pick CE/PE strikes (cross-strike allowed) inside premium_low–premium_high
    with the longest contiguous tight-gap run — the real 125–145 merge path.
    """
    from collections import defaultdict

    # Collect all candidate pairs that are in-band at least once
    pair_series: dict[tuple[int, int], list[tuple[float, float] | None]] = defaultdict(list)
    all_pairs: set[tuple[int, int]] = set()
    per_frame_legs: list[dict[tuple[str, int], float]] = []
    for fr in frames:
        legs_px: dict[tuple[str, int], float] = {}
        for key, row in (fr.get("legs") or {}).items():
            try:
                side, ks = key.split(":", 1)
                legs_px[(side, int(ks))] = float(row.get("c", row.get("ltp")))
            except (TypeError, ValueError):
                continue
        per_frame_legs.append(legs_px)
        ces = [k for (s, k), px in legs_px.items() if s == "CE" and band_lo - 5 <= px <= band_hi + 10]
        pes = [k for (s, k), px in legs_px.items() if s == "PE" and band_lo - 5 <= px <= band_hi + 10]
        for ck in ces:
            for pk in pes:
                all_pairs.add((ck, pk))

    if not all_pairs:
        return None

    band_mid = (band_lo + band_hi) / 2.0
    best_pair = None
    best_score: tuple = ()
    for ck, pk in all_pairs:
        tight_n = 0
        mids: list[float] = []
        for legs_px in per_frame_legs:
            ce = legs_px.get(("CE", ck))
            pe = legs_px.get(("PE", pk))
            if ce is None or pe is None:
                continue
            mid = (ce + pe) / 2
            gap = abs(ce - pe)
            # Both legs must themselves sit in the premium band (not ATM ~185)
            legs_in_band = (
                band_lo - 5 <= ce <= band_hi + 10
                and band_lo - 5 <= pe <= band_hi + 10
            )
            if legs_in_band and gap <= gap_tol and band_lo - 15 <= mid <= band_hi + 15:
                tight_n += 1
                mids.append(mid)
        if tight_n < 20:
            continue
        avg_mid = sum(mids) / len(mids)
        # Prefer mid nearest band center (true 125–145 channel), then more cover
        score = (
            -abs(avg_mid - band_mid),
            tight_n,
            1 if ck == pk else 0,
        )
        if best_pair is None or score > best_score:
            best_pair = (ck, pk)
            best_score = score
    return best_pair


def simulate_day(day: str, cfg: dict[str, Any]) -> dict[str, Any]:
    meta, frames, spot = _load_day(day)
    if not frames:
        return {"day": day, "error": "no frames", "frames": [], "trades": [], "summary": {}}

    lot_size = int(cfg.get("lot_size", 65) or 65)
    lots = int(cfg.get("algo_cw_lots", 4))
    lean_lots = int(cfg.get("early_winner_lean_add_lots", 1))
    band_lo = float(cfg.get("premium_low", 125))
    band_hi = float(cfg.get("premium_high", 145))
    merge_gap = float(cfg.get("early_winner_lean_merge_gap", 8.0))
    t_lo = float(cfg.get("breakout_premium_target_low", 168))
    t_hi = float(cfg.get("breakout_premium_target_high", 175))
    book_needs_fallen_tl = bool(cfg.get("breakout_book_needs_fallen_tl", True))
    expand_cap = float(cfg.get("expand_target_mult", 2.5))
    bounce = float(cfg.get("fallen_bounce_points", 10))
    sl_pts = float(cfg.get("fallen_sl_points", 10))
    crush_cut = float(cfg.get("crush_cut", 85))

    st = SimState()
    legs: list[Leg] = []
    events: list[dict] = []
    out_frames: list[dict] = []

    ce_hist: list[float] = []
    pe_hist: list[float] = []
    spot_hist: list[float] = []
    atm_ce_hist: list[float] = []
    atm_pe_hist: list[float] = []
    prem_ticks: list[dict] = []
    # Real 1m OHLC for locked CE/PE (fallen hammer needs wicks, not close-only)
    lock_ce_bars: list[dict[str, Any]] = []
    lock_pe_bars: list[dict[str, Any]] = []
    candles_1m: list[dict] = []

    locked_pair: dict | None = None
    merge_streak = 0
    merge_armed = False
    # Track first real expansion after merge — do NOT flip on pullback re-merge
    peak_gap_seen = 0.0
    expansion_leader: str | None = None
    rail_confirm_side: str | None = None
    rail_confirm_count = 0
    rail_winner_px: list[float] = []
    reverse_confirm_side: str | None = None
    reverse_confirm_count = 0
    reverse_winner_px: list[float] = []
    last_convergence_mid: float | None = None
    day_conv_mid: float | None = None  # first accepted same-day merge mid
    last_ce_px: float | None = None
    last_pe_px: float | None = None
    frozen_pipe: dict[str, Any] | None = None
    range_frozen = False  # lock chart rails at first breakout (don't redraw on re-merge)
    merge_band_high: float | None = None
    merge_band_low: float | None = None
    up_tl: dict[str, Any] | None = None  # winner support (lows)
    down_tl: dict[str, Any] | None = None  # fallen resistance (highs)
    w_dn_tl: dict[str, Any] | None = None  # winner resistance (highs)
    f_up_tl: dict[str, Any] | None = None  # fallen support (lows)
    tl_armed = False
    tl_w_strike: int | None = None
    tl_f_strike: int | None = None
    tl_w_side: str | None = None
    tl_f_side: str | None = None
    tl_buffer = float(cfg.get("algo_cw_breakout_tl_buffer", 1.5))
    tl_lookback = int(cfg.get("algo_cw_breakout_tl_lookback", 12))
    tl_from_start = bool(cfg.get("algo_cw_breakout_tl_from_day_start", True))
    tl_enabled = bool(cfg.get("algo_cw_breakout_tl_enabled", True))

    ce_hi_tl: dict[str, Any] | None = None
    ce_lo_tl: dict[str, Any] | None = None
    pe_hi_tl: dict[str, Any] | None = None
    pe_lo_tl: dict[str, Any] | None = None

    def _frame_tl(raw: dict | None, idxs: list[int]) -> dict | None:
        if not raw or not idxs:
            return None
        hi0 = int(raw["i0"])
        hi1 = int(raw["i1"] if raw.get("i1") is not None else raw.get("break_i") or hi0)
        if hi0 >= len(idxs) or hi1 >= len(idxs):
            return None
        fi0 = int(idxs[hi0])
        fi1 = int(idxs[hi1])
        # Parallel channels: project via hist intercept/slope so both rails stay parallel
        if raw.get("parallel") and raw.get("intercept") is not None and raw.get("slope") is not None:
            a = float(raw["intercept"])
            s_h = float(raw["slope"])
            y0 = a + s_h * hi0
            y1 = a + s_h * hi1
            span = max(1, fi1 - fi0)
            slope = (y1 - y0) / span
            return {
                **raw,
                "i0": fi0,
                "y0": round(y0, 2),
                "i1": fi1,
                "y1": round(y1, 2),
                "break_i": fi1,
                "slope": round(slope, 5),
                "intercept": round(y0 - slope * fi0, 3),
                "anchor_fi": fi0,
                "anchor_y": round(y0, 2),
                "parallel": True,
            }
        y0 = float(raw["y0"])
        y1 = float(raw["y1"] if raw.get("y1") is not None else y0)
        span = max(1, fi1 - fi0)
        slope = (y1 - y0) / span
        if raw.get("kind") == "up":
            slope = max(0.0, slope) or 0.005
        else:
            slope = min(0.0, slope) or -0.005
        return {
            **raw,
            "i0": fi0,
            "y0": round(y0, 2),
            "i1": fi1,
            "y1": round(y1, 2),
            "break_i": fi1,
            "slope": round(slope, 5),
            "intercept": round(y0 - slope * fi0, 3),
            "anchor_fi": fi0,
            "anchor_y": round(y0, 2),
        }

    def _side_ohlc_series(
        side: str, strike: int, fi_now: int
    ) -> tuple[list[int], list[float], list[float], list[float]]:
        idxs: list[int] = []
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        for j in range(0, fi_now + 1):
            bar = _leg_ohlc(frames[j], side, strike)
            px = _leg_px(frames[j], side, strike)
            if px is None and not bar:
                continue
            idxs.append(j)
            if bar:
                closes.append(float(bar.get("c") if bar.get("c") is not None else px or 0))
                highs.append(float(bar.get("h") if bar.get("h") is not None else closes[-1]))
                lows.append(float(bar.get("l") if bar.get("l") is not None else closes[-1]))
            else:
                closes.append(float(px))
                highs.append(float(px))
                lows.append(float(px))
        return idxs, closes, highs, lows

    def _fit_side_channel(
        side: str, strike: int, fi_now: int
    ) -> tuple[dict | None, dict | None]:
        idxs, closes, highs, lows = _side_ohlc_series(side, strike, fi_now)
        if len(closes) < 5:
            return None, None
        bi = max(1, len(closes) - 1)
        # Rolling window channel (like screenshot) — not full-day megaphone
        ch_look = int(cfg.get("algo_cw_channel_lookback_bars", 60) or 60)
        ch = fit_premium_channel(
            closes,
            break_i=bi,
            lookback=max(tl_lookback, ch_look),
            from_start=False,
            highs=highs,
            lows=lows,
        )
        # If still too wide vs price, tighten further
        width = float(ch.get("width") or 0)
        px_now = float(closes[-1] or 0)
        if px_now > 0 and width > max(25.0, px_now * 0.35):
            ch = fit_premium_channel(
                closes,
                break_i=bi,
                lookback=min(30, ch_look),
                from_start=False,
                highs=highs,
                lows=lows,
            )
        return _frame_tl(ch.get("down"), idxs), _frame_tl(ch.get("up"), idxs)

    def _refresh_ce_pe_channels(fi_now: int) -> None:
        """Parallel channels for BOTH Call and Put (screenshot-style)."""
        nonlocal ce_hi_tl, ce_lo_tl, pe_hi_tl, pe_lo_tl
        nonlocal up_tl, down_tl, w_dn_tl, f_up_tl, tl_armed
        nonlocal tl_w_strike, tl_f_strike, tl_w_side, tl_f_side
        ce_k = int(
            (locked_pair or {}).get("ce_strike")
            or st.ce_strike
            or (band_pair[0] if band_pair else 0)
            or 0
        )
        pe_k = int(
            (locked_pair or {}).get("pe_strike")
            or st.pe_strike
            or (band_pair[1] if band_pair else 0)
            or 0
        )
        if not ce_k or not pe_k:
            return
        ce_hi_tl, ce_lo_tl = _fit_side_channel("CE", ce_k, fi_now)
        pe_hi_tl, pe_lo_tl = _fit_side_channel("PE", pe_k, fi_now)
        # Exit aliases: winner support / fallen resistance
        side = st.breakout_side
        if side == "CE":
            up_tl, w_dn_tl = ce_lo_tl, ce_hi_tl
            f_up_tl, down_tl = pe_lo_tl, pe_hi_tl
            tl_w_side, tl_f_side = "CE", "PE"
            tl_w_strike, tl_f_strike = ce_k, pe_k
        elif side == "PE":
            up_tl, w_dn_tl = pe_lo_tl, pe_hi_tl
            f_up_tl, down_tl = ce_lo_tl, ce_hi_tl
            tl_w_side, tl_f_side = "PE", "CE"
            tl_w_strike, tl_f_strike = pe_k, ce_k
        else:
            # Pre-entry: still show both channels; keep aliases on CE/PE
            up_tl, w_dn_tl = ce_lo_tl, ce_hi_tl
            f_up_tl, down_tl = pe_lo_tl, pe_hi_tl
        if ce_hi_tl or pe_hi_tl:
            tl_armed = True

    def _arm_both_channels(side: str, fi_now: int) -> None:
        """Fit parallel CE + PE channels; bind exit rails to breakout side."""
        nonlocal tl_w_side
        st.breakout_side = side
        _refresh_ce_pe_channels(fi_now)
    reject_higher = bool(cfg.get("algo_cw_reject_higher_remerge", True))
    higher_buf = float(cfg.get("algo_cw_higher_remerge_buffer", 2.0))
    band_break_side: str | None = None
    band_break_count = 0
    last_zone: dict[str, Any] | None = None
    # unix-ish times for pipe fit (1m steps)
    series_for_pipe: list[dict[str, Any]] = []
    # Chart the 125–145 band pair from next-week DB (may be cross-strike).
    # ATM mid~185 is outside band — do not plot that as the convergence path.
    atm_series_for_pipe: list[dict[str, Any]] = []
    atm_frozen_pipe: dict[str, Any] | None = None
    atm_last_zone: dict[str, Any] | None = None
    hard_gap = float(cfg.get("algo_cw_breakout_hard_gap", 12.0))
    chart_atm = bool(cfg.get("playback_chart_atm", False))
    atm_pipe_gap = float(cfg.get("convergence_pipe_gap", 12.0))
    band_pair = None
    if cfg.get("playback_chart_ce_strike") is not None and cfg.get("playback_chart_pe_strike") is not None:
        band_pair = (
            int(cfg["playback_chart_ce_strike"]),
            int(cfg["playback_chart_pe_strike"]),
        )
    elif not chart_atm:
        band_pair = _best_band_chart_pair(
            frames, band_lo=band_lo, band_hi=band_hi, gap_tol=atm_pipe_gap
        )
        # Prefer the pair the lock/gate actually uses when it appears in top hits
        # (avoids a random neighboring cross-strike with similar minute-counts).
    # Overlay chart path = band pair (125–145) or ATM if explicitly requested
    chart_overlay = bool(chart_atm or band_pair is not None)

    for fi, fr in enumerate(frames):
        t = fr["t"]
        atm = int(fr["atm"])
        legs_map = fr.get("legs") or {}
        sp = spot.get(t) or {}
        spot_c = float(sp["c"]) if sp else float(fr.get("spot") or 0)
        spot_o = float(sp["o"]) if sp else spot_c
        # Chart = locked in-band trade pair once locked; else auto band pair
        if locked_pair and locked_pair.get("ce_strike") and locked_pair.get("pe_strike"):
            chart_ce_k = int(locked_pair["ce_strike"])
            chart_pe_k = int(locked_pair["pe_strike"])
            band_pair = (chart_ce_k, chart_pe_k)
            chart_k = chart_ce_k
            atm_ce_px = _leg_px(fr, "CE", chart_ce_k)
            atm_pe_px = _leg_px(fr, "PE", chart_pe_k)
        elif band_pair is not None:
            chart_ce_k, chart_pe_k = band_pair
            chart_k = chart_ce_k  # label
            atm_ce_px = _leg_px(fr, "CE", chart_ce_k)
            atm_pe_px = _leg_px(fr, "PE", chart_pe_k)
        else:
            chart_k = atm
            atm_ce_px = _leg_px(fr, "CE", atm)
            atm_pe_px = _leg_px(fr, "PE", atm)

        # Build candle list only up to now (no future)
        candles_1m.append(
            {
                "date": None,
                "open": spot_o,
                "close": spot_c,
                "high": float(sp.get("h") or spot_c),
                "low": float(sp.get("l") or spot_c),
            }
        )
        candle = {
            "side": "CE" if spot_c > spot_o else ("PE" if spot_c < spot_o else None),
            "color": sp.get("color") or ("green" if spot_c > spot_o else "red"),
            "open": spot_o,
            "close": spot_c,
        }

        gate = try_lock_pair(
            atm=atm,
            legs=legs_map,
            band_lo=band_lo,
            band_hi=band_hi,
            atm_tol=float(cfg.get("atm_diag_atm_tol", 3.0)),
            diag_tol=float(cfg.get("atm_diag_diag_tol", 5.0)),
            equal_tol=float(cfg.get("atm_diag_equal_tol", 3.0)),
            require_itm=bool(cfg.get("atm_diag_require_itm", False)),
            require_atm_gate=bool(cfg.get("atm_diag_merge_gate_required", False)),
            allow_cross_atm_x=bool(cfg.get("atm_diag_cross_x_enabled", True)),
            cross_x_tol=float(cfg.get("atm_diag_cross_x_tol", cfg.get("atm_diag_equal_tol", 5.0))),
            cross_x_depths=tuple(
                int(x) for x in (cfg.get("atm_diag_cross_x_depths") or [1, 2, 3, 4])
            ),
        )
        gate_info = gate.get("gate") or {}
        if gate_info.get("ce_atm") is not None:
            atm_ce_hist.append(float(gate_info["ce_atm"]))
            atm_pe_hist.append(float(gate_info["pe_atm"]))

        # Lock once when gate ok — only after algo_cw_merge_after (default 09:20)
        merge_after = str(cfg.get("algo_cw_merge_after") or "09:20")
        if (
            not locked_pair
            and gate.get("ok")
            and gate.get("pair")
            and t >= merge_after
        ):
            locked_pair = dict(gate["pair"])
            # Refuse lock if either premium is outside 125–145
            try:
                _ce = float(locked_pair["ce"])
                _pe = float(locked_pair["pe"])
            except (KeyError, TypeError, ValueError):
                _ce = _pe = -1.0
            if not (
                band_lo <= _ce <= band_hi and band_lo <= _pe <= band_hi
            ):
                locked_pair = None
                st.message = (
                    f"skip lock CE@{_ce}/PE@{_pe} outside {band_lo:g}–{band_hi:g}"
                )
            else:
                st.locked = True
                st.ce_sym = locked_pair.get("ce_sym")
                st.pe_sym = locked_pair.get("pe_sym")
                st.ce_strike = int(locked_pair["ce_strike"])
                st.pe_strike = int(locked_pair["pe_strike"])
                band_pair = (st.ce_strike, st.pe_strike)
                # Rebuild chart pipe on the locked in-band pair (drop wrong overlay)
                atm_series_for_pipe = []
                atm_frozen_pipe = None
                atm_last_zone = None
                series_for_pipe = []
                frozen_pipe = None
                last_zone = None
                range_frozen = False
                events.append({"t": t, "ev": "LOCK", **locked_pair})
                st.message = (
                    f"LOCKED CE{st.ce_strike}/PE{st.pe_strike} "
                    f"@{locked_pair['ce']}/{locked_pair['pe']}"
                )
                # Same bar: refresh chart premiums to the locked in-band pair
                chart_ce_k, chart_pe_k = st.ce_strike, st.pe_strike
                chart_k = chart_ce_k
                atm_ce_px = _leg_px(fr, "CE", chart_ce_k)
                atm_pe_px = _leg_px(fr, "PE", chart_pe_k)
                chart_overlay = True
        elif not locked_pair and t < merge_after:
            st.message = f"wait merge after {merge_after} (now {t})"
            # Channel starts at lock — day-open backfill pulled rails to 09:53
            series_for_pipe = []
            frozen_pipe = None
            last_zone = None
            range_frozen = False
            lock_ce_bars = []
            lock_pe_bars = []
            st.fallen_rev_arm = None

        ce_px = pe_px = None
        # Prefer locked strikes; else gate pair — collect full-day merge for pipe (1m–2h)
        strike_ce = strike_pe = None
        if locked_pair:
            strike_ce = int(locked_pair["ce_strike"])
            strike_pe = int(locked_pair["pe_strike"])
        elif gate.get("pair"):
            gp = gate["pair"]
            strike_ce = int(gp["ce_strike"])
            strike_pe = int(gp["pe_strike"])
        if strike_ce is not None and strike_pe is not None:
            ce_px = _leg_px(fr, "CE", strike_ce)
            pe_px = _leg_px(fr, "PE", strike_pe)
        if ce_px is not None:
            last_ce_px = float(ce_px)
        if pe_px is not None:
            last_pe_px = float(pe_px)

        # ATM chart pipe (visual truth) — independent of 125–145 band lock
        if chart_overlay and atm_ce_px is not None and atm_pe_px is not None:
            t_unix_atm = 1_700_000_000 + fi * 60
            atm_series_for_pipe.append(
                {"time": t_unix_atm, "ce": atm_ce_px, "pe": atm_pe_px}
            )
            z_atm = detect_convergence_zone(
                [p["ce"] for p in atm_series_for_pipe],
                [p["pe"] for p in atm_series_for_pipe],
                gap_tol=atm_pipe_gap,
                min_samples=2,
                times=[p["time"] for p in atm_series_for_pipe],
                max_soft_breach_pct=float(
                    cfg.get("convergence_pipe_soft_breach_pct", 0.12)
                ),
                max_soft_breach_floor=int(
                    cfg.get("convergence_pipe_soft_breach_floor", 8)
                ),
                max_hard_bridge=int(cfg.get("convergence_pipe_hard_bridge", 2)),
                rail_mode=str(cfg.get("convergence_rail_mode", "band")),
            )
            if z_atm:
                atm_last_zone = z_atm
            if z_atm and (z_atm.get("pipe_points") or []):
                new_n = len(z_atm["pipe_points"])
                old_n = len((atm_frozen_pipe or {}).get("pipe_points") or [])
                end_i = int(z_atm.get("end_i") or -1)
                active = end_i >= len(atm_series_for_pipe) - 3
                gap_atm = abs(atm_ce_px - atm_pe_px)
                still_tight = gap_atm <= atm_pipe_gap * 1.25
                # Once we have a real merge channel (≥15m), keep it for the chart —
                # later re-merges / expansion must not wipe or fan the morning rails.
                quality_locked = old_n >= 15 or range_frozen
                take = (not quality_locked) and (
                    atm_frozen_pipe is None
                    or (
                        still_tight
                        and active
                        and (new_n >= old_n or new_n >= max(4, int(old_n * 0.7)))
                    )
                )
                if take:
                    s_i = int(z_atm.get("start_i") or 0)
                    start_fi_atm = 0
                    if 0 <= s_i < len(atm_series_for_pipe):
                        start_fi_atm = int(
                            (int(atm_series_for_pipe[s_i]["time"]) - 1_700_000_000) // 60
                        )
                    e_i = int(z_atm.get("end_i") or s_i)
                    end_fi_atm = start_fi_atm
                    if 0 <= e_i < len(atm_series_for_pipe):
                        end_fi_atm = int(
                            (int(atm_series_for_pipe[e_i]["time"]) - 1_700_000_000) // 60
                        )
                    atm_frozen_pipe = {
                        "pipe": z_atm.get("pipe"),
                        "pipe_points": z_atm["pipe_points"],
                        "start_i": s_i,
                        "end_i": e_i,
                        "start_fi": start_fi_atm,
                        "end_fi": end_fi_atm,
                        "source": "atm",
                    }

        if ce_px is not None and pe_px is not None:
            ce_hist.append(ce_px)
            pe_hist.append(pe_px)
            spot_hist.append(spot_c)
            prem_ticks.append({"t": float(fi), "ce": ce_px, "pe": pe_px})
            gap = abs(ce_px - pe_px)
            mid = (ce_px + pe_px) / 2
            # Parallel CE + PE channels (high/low touches) once we have a pair
            if tl_enabled and (locked_pair or band_pair) and fi >= 4 and fi % 1 == 0:
                _refresh_ce_pe_channels(fi)
            # Last tight merge mid for fall-back SL (update only while not in a trade)
            if locked_pair and st.phase == "SCAN" and gap <= merge_gap:
                last_convergence_mid = mid
            # Convergence channel only on LOCKED pair (pre-lock gate pair polluted rails)
            if locked_pair:
                ce_k = int(locked_pair.get("ce_strike") or st.ce_strike or 0)
                pe_k = int(locked_pair.get("pe_strike") or st.pe_strike or 0)
                if ce_k:
                    bar_ce = _leg_ohlc(fr, "CE", ce_k)
                    if bar_ce:
                        lock_ce_bars.append(bar_ce)
                        lock_ce_bars = lock_ce_bars[-120:]
                if pe_k:
                    bar_pe = _leg_ohlc(fr, "PE", pe_k)
                    if bar_pe:
                        lock_pe_bars.append(bar_pe)
                        lock_pe_bars = lock_pe_bars[-120:]
                t_unix = 1_700_000_000 + fi * 60
                series_for_pipe.append({"time": t_unix, "ce": ce_px, "pe": pe_px})
                pipe_gap = float(cfg.get("convergence_pipe_gap", 15.0))
                pipe_min = 2
                zpipe = detect_convergence_zone(
                    [p["ce"] for p in series_for_pipe],
                    [p["pe"] for p in series_for_pipe],
                    gap_tol=pipe_gap,
                    min_samples=pipe_min,
                    times=[p["time"] for p in series_for_pipe],
                    max_soft_breach_pct=float(
                        cfg.get("convergence_pipe_soft_breach_pct", 0.12)
                    ),
                    max_soft_breach_floor=int(
                        cfg.get("convergence_pipe_soft_breach_floor", 8)
                    ),
                    max_hard_bridge=int(cfg.get("convergence_pipe_hard_bridge", 2)),
                    rail_mode=str(cfg.get("convergence_rail_mode", "band")),
                )
                if zpipe:
                    last_zone = zpipe
                # Grow merge pipe while still tight; freeze once expansion opens
                # (refitting through CE/PE split collapses the drawn channel)
                if zpipe and (zpipe.get("pipe_points") or []) and not range_frozen:
                    new_n = len(zpipe["pipe_points"])
                    old_n = len((frozen_pipe or {}).get("pipe_points") or [])
                    end_i = int(zpipe.get("end_i") or -1)
                    active = end_i >= len(series_for_pipe) - 3
                    still_tight = gap <= pipe_gap * 1.25
                    take = frozen_pipe is None or (
                        still_tight
                        and active
                        and (new_n >= old_n or new_n >= max(4, int(old_n * 0.7)))
                    )
                    if take:
                        s_i = int(zpipe.get("start_i") or 0)
                        start_fi = fi
                        if 0 <= s_i < len(series_for_pipe):
                            start_fi = int(
                                (int(series_for_pipe[s_i]["time"]) - 1_700_000_000)
                                // 60
                            )
                        e_i = int(zpipe.get("end_i") or s_i)
                        end_fi = fi
                        if 0 <= e_i < len(series_for_pipe):
                            end_fi = int(
                                (int(series_for_pipe[e_i]["time"]) - 1_700_000_000)
                                // 60
                            )
                        frozen_pipe = {
                            "pipe": zpipe.get("pipe"),
                            "pipe_points": zpipe.get("pipe_points"),
                            "samples": zpipe.get("samples"),
                            "start_i": s_i,
                            "start_fi": start_fi,
                            "end_i": e_i,
                            "end_fi": end_fi,
                        }

            # --- SCAN: watch merge → buy BREAKOUT side only (default) ---
            breakout_only = cfg.get("algo_cw_breakout_only", True)
            if st.phase == "SCAN" and locked_pair:
                if gap <= merge_gap and band_lo <= mid <= band_hi:
                    # Higher same-day re-merge than first conv = decay — do not arm
                    higher_decay = (
                        reject_higher
                        and day_conv_mid is not None
                        and float(mid) > float(day_conv_mid) + higher_buf
                    )
                    if higher_decay:
                        merge_streak = 0
                        merge_armed = False
                        up_tl = down_tl = None
                        tl_armed = False
                        st.message = (
                            f"skip higher re-merge mid {mid:.0f}>"
                            f"day conv {day_conv_mid:.0f} (decay)"
                        )
                    else:
                        merge_streak += 1
                        if merge_streak >= 2:
                            merge_armed = True
                            if day_conv_mid is None:
                                day_conv_mid = float(mid)
                elif gap <= merge_gap * 1.5 and band_lo - 5 <= mid <= band_hi + 5:
                    # near-merge — keep streak warm, but only arm after 2 real merge bars
                    merge_streak = max(merge_streak, 1)
                else:
                    merge_streak = 0

                # Stay armed through expansion (bugfix: old code dropped watch when gap>12)
                # Must have seen a real merge first — don't chase mid-day without re-merge
                watching = merge_armed
                leader = "CE" if ce_px > pe_px else ("PE" if pe_px > ce_px else None)
                if merge_armed and gap >= peak_gap_seen:
                    peak_gap_seen = gap
                    if leader in ("CE", "PE"):
                        expansion_leader = leader

                side = None
                lean = {"reason": "warming"}
                if watching:
                    # Breakout-only: mid+N first, else rail/hard + confirm
                    if breakout_only:
                        entry_pts = float(cfg.get("algo_cw_entry_above_conv_pts", 12.0) or 0)
                        mid_fired = False
                        if entry_pts > 0:
                            mid_hit = detect_entry_above_convergence(
                                ce=ce_px,
                                pe=pe_px,
                                conv_mid=last_convergence_mid,
                                pts=entry_pts,
                            )
                            if mid_hit.get("ok") and mid_hit.get("side") in ("CE", "PE"):
                                side = str(mid_hit["side"])
                                mid_fired = True
                                lean = {"reason": str(mid_hit.get("reason"))}
                                events.append(
                                    {
                                        "t": t,
                                        "ev": "CONV_MID_PLUS",
                                        "side": side,
                                        "threshold": mid_hit.get("threshold"),
                                        "reason": mid_hit.get("reason"),
                                    }
                                )
                            else:
                                lean = {
                                    "reason": str(
                                        mid_hit.get("reason")
                                        or f"wait ≥conv+{entry_pts:g}"
                                    )
                                }
                        if not mid_fired and not (
                            entry_pts > 0 and last_convergence_mid is not None
                        ):
                            confirm_n = int(cfg.get("algo_cw_break_confirm_points", 3))
                            force = None
                            # One-sided quotes: still allow PE/CE leader
                            if ce_px is not None and pe_px is not None and gap >= hard_gap:
                                force = leader
                            elif ce_px is None and pe_px is not None and rail_confirm_side != "CE":
                                # CE quote gone — PE can still arm if expanding vs recent mid
                                pass
                            zone = None
                            if last_zone or frozen_pipe:
                                zone = last_zone or {
                                    "pipe": (frozen_pipe or {}).get("pipe"),
                                    "start_i": (frozen_pipe or {}).get("start_i") or 0,
                                    "pipe_points": (frozen_pipe or {}).get("pipe_points"),
                                }
                            rb = detect_rail_breakout(
                                zone=zone,
                                ce=ce_px,
                                pe=pe_px,
                                hist_len=max(len(series_for_pipe), 1),
                                buffer=float(cfg.get("algo_cw_break_rail_buffer", 1.0)),
                                confirm_points=confirm_n,
                                confirm_side=rail_confirm_side,
                                confirm_count=rail_confirm_count,
                                winner_px_hist=rail_winner_px,
                                hard_gap=hard_gap,
                                force_side=force,
                            )
                            if rb.get("aborted"):
                                rail_confirm_side = None
                                rail_confirm_count = 0
                                rail_winner_px = []
                                lean = {"reason": str(rb.get("reason"))}
                                events.append(
                                    {"t": t, "ev": "ABORT_CONFIRM", "reason": rb.get("reason")}
                                )
                            elif rb.get("side"):
                                new_side = str(rb["side"])
                                if rail_confirm_side and rail_confirm_side != new_side:
                                    rail_winner_px = []
                                rail_confirm_side = new_side
                                rail_confirm_count = int(rb.get("confirm_count") or 0)
                                wpx = ce_px if new_side == "CE" else pe_px
                                if wpx is not None:
                                    rail_winner_px.append(wpx)
                                    rail_winner_px = rail_winner_px[-12:]
                            if rb.get("ok") and rb.get("side") in ("CE", "PE"):
                                side = str(rb["side"])
                                events.append(
                                    {
                                        "t": t,
                                        "ev": "RAIL_BREAK",
                                        "side": side,
                                        "reason": rb.get("reason"),
                                    }
                                )
                            elif rb.get("pending"):
                                lean = {"reason": str(rb.get("reason") or "confirming")}
                            else:
                                lean = {
                                    "reason": str(
                                        rb.get("reason")
                                        or f"wait rail/hard≥{hard_gap:g}"
                                    )
                                }
                    elif gap >= hard_gap and leader in ("CE", "PE"):
                        side = leader
                        events.append(
                            {
                                "t": t,
                                "ev": "HARD_BREAK",
                                "side": side,
                                "gap": round(gap, 1),
                                "leader": leader,
                            }
                        )
                    else:
                        lean = detect_early_winner_lean(
                            ce_hist=ce_hist,
                            pe_hist=pe_hist,
                            spot_hist=spot_hist,
                            atm_ce_hist=atm_ce_hist or None,
                            atm_pe_hist=atm_pe_hist or None,
                            spot_candle=candle,
                            cfg=cfg,
                        )
                        if lean.get("ok") and lean.get("side") in ("CE", "PE"):
                            side = str(lean["side"])
                            events.append(
                                {
                                    "t": t,
                                    "ev": "LEAN",
                                    "side": side,
                                    "reason": lean.get("reason"),
                                }
                            )
                        elif (
                            cfg.get("early_winner_lean_candle_break", True)
                            and candle.get("side") in ("CE", "PE")
                            and len(ce_hist) >= 3
                        ):
                            cs = str(candle["side"])
                            ce_move = ce_px - ce_hist[-3]
                            pe_move = pe_px - pe_hist[-3]
                            if (cs == "CE" and ce_move >= 3 and ce_move > pe_move) or (
                                cs == "PE" and pe_move >= 3 and pe_move > ce_move
                            ):
                                side = cs
                                events.append(
                                    {
                                        "t": t,
                                        "ev": "CANDLE_BREAK",
                                        "side": side,
                                        "color": candle.get("color"),
                                    }
                                )

                if breakout_only and side in ("CE", "PE"):
                    px = ce_px if side == "CE" else pe_px
                    entry_pts = float(cfg.get("algo_cw_entry_above_conv_pts", 12.0) or 0)
                    band_ceil = band_hi
                    if entry_pts > 0 and last_convergence_mid is not None:
                        band_ceil = max(
                            band_hi, float(last_convergence_mid) + entry_pts + 2.0
                        )
                    # Buy in band (ceil raised for mid+N), and only after a real merge mid
                    if px is None or not (band_lo <= float(px) <= band_ceil):
                        lean = {
                            "reason": (
                                f"skip {side}@{px} outside {band_lo:g}–{band_ceil:g}"
                                if px is not None
                                else "skip no quote"
                            )
                        }
                        side = None
                    elif last_convergence_mid is None and gap > merge_gap:
                        lean = {"reason": f"skip {side} — no convergence yet gap={gap:.1f}"}
                        side = None
                    else:
                        st.phase = "BREAKOUT_WATCH"
                        st.breakout_side = side
                        st.fallen_side = None
                        st.ce_entry, st.pe_entry = ce_px, pe_px
                        if last_convergence_mid is None and gap <= merge_gap:
                            last_convergence_mid = mid
                        st.mid_at_entry = last_convergence_mid
                        st.breakout_peak = px
                        st.breakout_t = t
                        st.lean_side = side
                        legs.append(
                            Leg(
                                side,
                                "breakout",
                                t,
                                px,
                                lots,
                                st.ce_sym if side == "CE" else st.pe_sym or "",
                                st.ce_strike if side == "CE" else st.pe_strike,
                            )
                        )
                        events.append(
                            {
                                "t": t,
                                "ev": "BUY_BREAKOUT",
                                "side": side,
                                "px": px,
                                "conv_mid": last_convergence_mid,
                                "above_pts": entry_pts,
                            }
                        )
                        mid_note = (
                            f" · mid {last_convergence_mid:.0f}+{entry_pts:g}"
                            if last_convergence_mid is not None and entry_pts > 0
                            else ""
                        )
                        st.message = f"BOUGHT {side} @{px:.1f} only{mid_note}"
                        merge_streak = 0
                        reverse_confirm_side = None
                        reverse_confirm_count = 0
                        reverse_winner_px = []
                        rail_confirm_side = None
                        rail_confirm_count = 0
                        rail_winner_px = []
                        range_frozen = True  # keep plotting the merge range that broke
                        if tl_enabled:
                            _arm_both_channels(str(side), fi)
                        # Lock chart to last good merge zone if we never froze one
                        if last_zone and (
                            not frozen_pipe
                            or len(frozen_pipe.get("pipe_points") or []) < 2
                        ):
                            s_i = int(last_zone.get("start_i") or 0)
                            e_i = int(last_zone.get("end_i") or s_i)
                            start_fi = (
                                int((int(series_for_pipe[s_i]["time"]) - 1_700_000_000) // 60)
                                if 0 <= s_i < len(series_for_pipe)
                                else fi
                            )
                            end_fi = (
                                int((int(series_for_pipe[e_i]["time"]) - 1_700_000_000) // 60)
                                if 0 <= e_i < len(series_for_pipe)
                                else fi
                            )
                            frozen_pipe = {
                                "pipe": last_zone.get("pipe"),
                                "pipe_points": last_zone.get("pipe_points"),
                                "samples": last_zone.get("samples"),
                                "start_i": s_i,
                                "start_fi": start_fi,
                                "end_i": e_i,
                                "end_fi": end_fi,
                            }
                        # Ensure rails exist for chart (short 1–2 bar merges still draw)
                        if not frozen_pipe or len(frozen_pipe.get("pipe_points") or []) < 2:
                            hw = max(6.0, abs(ce_px - pe_px) / 2 + 4.0)
                            m0 = float(st.mid_at_entry or mid)
                            pts = []
                            for k, p in enumerate(
                                series_for_pipe[-max(2, min(30, len(series_for_pipe))):]
                            ):
                                pts.append(
                                    {
                                        "time": p["time"],
                                        "high": round(m0 + hw, 2),
                                        "mid": round(m0, 2),
                                        "low": round(m0 - hw, 2),
                                    }
                                )
                            if len(pts) >= 2:
                                frozen_pipe = {
                                    "pipe": {
                                        "half_width": hw,
                                        "tilt": "flat",
                                        "n": len(pts),
                                    },
                                    "pipe_points": pts,
                                    "samples": len(pts),
                                    "start_i": max(0, len(series_for_pipe) - len(pts)),
                                    "start_fi": int(
                                        (int(pts[0]["time"]) - 1_700_000_000) // 60
                                    ),
                                }
                elif (
                    not breakout_only
                    and t >= str(cfg.get("algo_cw_merge_after") or "09:20")
                    and merge_streak >= 2
                    and gate.get("ok")
                    and ce_px is not None
                    and pe_px is not None
                    and band_lo <= float(ce_px) <= band_hi
                    and band_lo <= float(pe_px) <= band_hi
                ):
                    higher_decay = (
                        reject_higher
                        and day_conv_mid is not None
                        and float(mid) > float(day_conv_mid) + higher_buf
                    )
                    if higher_decay:
                        merge_streak = 0
                        merge_armed = False
                        up_tl = down_tl = None
                        tl_armed = False
                        st.message = (
                            f"skip higher re-merge mid {mid:.0f}>"
                            f"day conv {day_conv_mid:.0f} (decay)"
                        )
                        events.append(
                            {
                                "t": t,
                                "ev": "SKIP_HIGHER_REMERGE",
                                "mid": round(float(mid), 1),
                                "day_conv_mid": round(float(day_conv_mid), 1),
                            }
                        )
                    else:
                        st.phase = "DUAL_OPEN"
                        st.ce_entry, st.pe_entry = ce_px, pe_px
                        st.mid_at_entry = mid
                        last_convergence_mid = mid
                        if day_conv_mid is None:
                            day_conv_mid = float(mid)
                        legs.append(
                            Leg(
                                "CE",
                                "dual",
                                t,
                                ce_px,
                                lots,
                                st.ce_sym or "",
                                st.ce_strike,
                            )
                        )
                        legs.append(
                            Leg(
                                "PE",
                                "dual",
                                t,
                                pe_px,
                                lots,
                                st.pe_sym or "",
                                st.pe_strike,
                            )
                        )
                        events.append(
                            {
                                "t": t,
                                "ev": "DUAL",
                                "ce": ce_px,
                                "pe": pe_px,
                                "mid": round(mid, 1),
                            }
                        )
                        st.message = f"dual entry CE@{ce_px} PE@{pe_px}"
                        merge_streak = 0
                    # Freeze flat parallel band at first merge only (don't replace on 2nd dual)
                    range_frozen = True
                    already_flat = (
                        isinstance(atm_frozen_pipe, dict)
                        and atm_frozen_pipe.get("source") == "dual_flat_band"
                    )
                    if not already_flat:
                        hw = max(
                            4.0,
                            abs(float(ce_px) - float(pe_px)) / 2.0 + 2.0,
                            abs(float(ce_px) - float(mid)),
                            abs(float(pe_px) - float(mid)),
                        )
                        m0 = float(mid)
                        flat_pipe = {
                            "mode": "band",
                            "slope": 0.0,
                            "intercept": m0,
                            "high_slope": 0.0,
                            "high_intercept": m0 + hw,
                            "low_slope": 0.0,
                            "low_intercept": m0 - hw,
                            "half_width": hw,
                            "mid0": m0,
                            "mid1": m0,
                            "high0": m0 + hw,
                            "high1": m0 + hw,
                            "low0": m0 - hw,
                            "low1": m0 - hw,
                            "tilt": "flat",
                            "converging": False,
                            "n": 2,
                        }
                        # Paint from lock (or now) → EOD as horizontal channel
                        start_fi_band = fi
                        if locked_pair:
                            for ev in reversed(events):
                                if ev.get("ev") == "LOCK" and ev.get("t"):
                                    for j, frx in enumerate(frames):
                                        if frx.get("t") == ev["t"]:
                                            start_fi_band = j
                                            break
                                    break
                        pts = [
                            {
                                "time": 1_700_000_000 + j * 60,
                                "high": round(m0 + hw, 2),
                                "mid": round(m0, 2),
                                "low": round(m0 - hw, 2),
                            }
                            for j in range(start_fi_band, len(frames))
                        ]
                        band_freeze = {
                            "pipe": flat_pipe,
                            "pipe_points": pts,
                            "start_i": 0,
                            "end_i": max(0, len(pts) - 1),
                            "start_fi": start_fi_band,
                            "end_fi": len(frames) - 1,
                            "source": "dual_flat_band",
                        }
                        atm_frozen_pipe = band_freeze
                        frozen_pipe = band_freeze
                        merge_band_high = m0 + hw
                        merge_band_low = m0 - hw
                else:
                    st.message = (
                        f"WATCH gap={gap:.1f} · {str(lean.get('reason') or '')[:40]}"
                    )

            elif st.phase == "DUAL_OPEN":
                # Wait for band split → arm winner up-TL + fallen down-TL
                ce_move = float(ce_px) - float(st.ce_entry or ce_px)
                pe_move = float(pe_px) - float(st.pe_entry or pe_px)
                b_hi = float(
                    merge_band_high
                    if merge_band_high is not None
                    else (st.mid_at_entry or mid) + 6
                )
                b_lo = float(
                    merge_band_low
                    if merge_band_low is not None
                    else (st.mid_at_entry or mid) - 6
                )
                side = None
                if tl_enabled:
                    cand = None
                    if float(ce_px) >= b_hi and float(ce_px) > float(pe_px):
                        cand = "CE"
                    elif float(pe_px) >= b_hi and float(pe_px) > float(ce_px):
                        cand = "PE"
                    # Require expansion: winner up, loser down, 2 bars or lead ≥5
                    if cand:
                        lead = (
                            float(ce_px) - float(st.ce_entry or ce_px)
                            if cand == "CE"
                            else float(pe_px) - float(st.pe_entry or pe_px)
                        )
                        other = (
                            float(pe_px) - float(st.pe_entry or pe_px)
                            if cand == "CE"
                            else float(ce_px) - float(st.ce_entry or ce_px)
                        )
                        if band_break_side == cand:
                            band_break_count += 1
                        else:
                            band_break_side = cand
                            band_break_count = 1
                        if lead >= 5.0 and other <= -2.0 and band_break_count >= 2:
                            side = cand
                    else:
                        band_break_side = None
                        band_break_count = 0
                if side is None and not tl_enabled:
                    ready = dual_book_winner(
                        float(ce_px),
                        float(pe_px),
                        float(st.ce_entry or ce_px),
                        float(st.pe_entry or pe_px),
                        cfg,
                    )
                    if ready and ready.get("side") in ("CE", "PE"):
                        side = str(ready["side"])
                if side in ("CE", "PE"):
                    f_side = "PE" if side == "CE" else "CE"
                    if last_convergence_mid is None:
                        last_convergence_mid = st.mid_at_entry
                    for L in legs:
                        if L.exit is not None:
                            continue
                        L.role = "breakout" if L.side == side else "fallen"
                    st.breakout_side = side
                    st.fallen_side = f_side
                    st.breakout_peak = float(ce_px if side == "CE" else pe_px)
                    st.breakout_t = t
                    st.fallen_low = float(pe_px if side == "CE" else ce_px)
                    # Fit TLs on locked strikes from day open (fi=0), not post-merge hist only
                    w_strike = int(
                        st.ce_strike if side == "CE" else st.pe_strike or 0
                    )
                    f_strike = int(
                        st.pe_strike if side == "CE" else st.ce_strike or 0
                    )
                    idxs: list[int] = []
                    w_vals: list[float] = []
                    f_vals: list[float] = []
                    for j in range(0, fi + 1):
                        wv = _leg_px(frames[j], side, w_strike)
                        fv = _leg_px(frames[j], f_side, f_strike)
                        if wv is None or fv is None:
                            continue
                        idxs.append(j)
                        w_vals.append(float(wv))
                        f_vals.append(float(fv))
                    bi = max(1, len(w_vals) - 1)
                    fi_bi = max(1, len(f_vals) - 1)
                    raw_up = fit_up_trendline(
                        w_vals,
                        break_i=bi,
                        lookback=tl_lookback,
                        from_start=tl_from_start,
                    )
                    raw_dn = fit_down_trendline(
                        f_vals,
                        break_i=fi_bi,
                        lookback=tl_lookback,
                        from_start=tl_from_start,
                    )

                    def _to_frame_tl(raw: dict | None) -> dict | None:
                        """Map hist pivots → frame indices; slope from bottom↔bottom only."""
                        if not raw or not idxs:
                            return None
                        hi0 = int(raw["i0"])
                        hi1 = int(
                            raw["i1"]
                            if raw.get("i1") is not None
                            else raw.get("break_i")
                            or hi0
                        )
                        if hi0 >= len(idxs) or hi1 >= len(idxs):
                            return None
                        fi0 = int(idxs[hi0])
                        fi1 = int(idxs[hi1])
                        y0 = float(raw["y0"])
                        y1 = float(
                            raw["y1"] if raw.get("y1") is not None else y0
                        )
                        span = max(1, fi1 - fi0)
                        slope = (y1 - y0) / span
                        if raw.get("kind") == "up":
                            slope = max(0.0, slope) or 0.005
                        else:
                            slope = min(0.0, slope) or -0.005
                        return {
                            **raw,
                            "i0": fi0,
                            "y0": round(y0, 2),
                            "i1": fi1,
                            "y1": round(y1, 2),
                            "break_i": fi1,
                            "slope": round(slope, 5),
                            "intercept": round(y0 - slope * fi0, 3),
                            "anchor_fi": fi0,
                            "anchor_y": round(y0, 2),
                        }

                    up_tl = _to_frame_tl(raw_up)
                    down_tl = _to_frame_tl(raw_dn)
                    tl_armed = True
                    # Remember locked strikes for progressive TL correction
                    tl_w_strike = w_strike
                    tl_f_strike = f_strike
                    tl_w_side = side
                    tl_f_side = f_side
                    st.phase = "TL_HOLD"
                    events.append(
                        {
                            "t": t,
                            "ev": "BAND_BREAK",
                            "side": side,
                            "band_hi": round(b_hi, 1),
                            "band_lo": round(b_lo, 1),
                            "up_tl": up_tl,
                            "down_tl": down_tl,
                        }
                    )
                    st.message = (
                        f"band break {side} · up-TL+down-TL armed · hold for TL exits"
                    )
                else:
                    st.message = (
                        f"dual hold ΔCE {ce_move:+.1f} ΔPE {pe_move:+.1f} "
                        f"wait break >{b_hi:.0f}"
                    )

            elif st.phase == "TL_HOLD":
                # Winner: sell below up-TL; Fallen: sell above down-TL
                b = st.breakout_side
                f_side = st.fallen_side
                w_px = ce_px if b == "CE" else pe_px
                f_px = pe_px if b == "CE" else ce_px
                if st.breakout_peak is not None and w_px is not None and w_px > st.breakout_peak:
                    st.breakout_peak = w_px
                if f_px is not None and (
                    st.fallen_low is None or f_px < st.fallen_low
                ):
                    st.fallen_low = f_px
                # Correct TLs onto newer bottoms/tops as the day progresses
                if tl_armed and tl_w_strike and tl_f_strike:
                    w_vals_c: list[float] = []
                    f_vals_c: list[float] = []
                    idxs_c: list[int] = []
                    for j in range(0, fi + 1):
                        wv = _leg_px(frames[j], str(tl_w_side), int(tl_w_strike))
                        fv = _leg_px(frames[j], str(tl_f_side), int(tl_f_strike))
                        if wv is None or fv is None:
                            continue
                        idxs_c.append(j)
                        w_vals_c.append(float(wv))
                        f_vals_c.append(float(fv))
                    if len(w_vals_c) >= 3:
                        bi_c = len(w_vals_c) - 1
                        raw_u = correct_up_trendline(
                            None,
                            w_vals_c,
                            end_i=bi_c,
                            from_start=tl_from_start,
                        )
                        raw_d = correct_down_trendline(
                            None,
                            f_vals_c,
                            end_i=bi_c,
                            from_start=tl_from_start,
                        )
                        # Map hist pivots → frame; keep prior if map fails
                        def _map_corr(raw: dict | None) -> dict | None:
                            if not raw or not idxs_c:
                                return None
                            h0 = int(raw["i0"])
                            h1 = int(raw.get("i1") if raw.get("i1") is not None else h0)
                            if h0 >= len(idxs_c) or h1 >= len(idxs_c):
                                return None
                            f0, f1 = int(idxs_c[h0]), int(idxs_c[h1])
                            y0, y1 = float(raw["y0"]), float(
                                raw["y1"] if raw.get("y1") is not None else raw["y0"]
                            )
                            span = max(1, f1 - f0)
                            slope = (y1 - y0) / span
                            if raw.get("kind") == "up":
                                slope = max(0.0, slope) or 0.005
                            else:
                                slope = min(0.0, slope) or -0.005
                            return {
                                **raw,
                                "i0": f0,
                                "y0": round(y0, 2),
                                "i1": f1,
                                "y1": round(y1, 2),
                                "break_i": f1,
                                "slope": round(slope, 5),
                                "intercept": round(y0 - slope * f0, 3),
                                "anchor_fi": f0,
                                "anchor_y": round(y0, 2),
                            }

                        nu = _map_corr(raw_u)
                        nd = _map_corr(raw_d)
                        if nu:
                            # Accept correction if second bottom advanced or pivots refreshed
                            old_i1 = int((up_tl or {}).get("i1") or -1)
                            if up_tl is None or int(nu.get("i1") or 0) >= old_i1:
                                up_tl = nu
                        if nd:
                            old_i1 = int((down_tl or {}).get("i1") or -1)
                            if down_tl is None or int(nd.get("i1") or 0) >= old_i1:
                                down_tl = nd
                winner_open = any(
                    L.exit is None and L.side == b and L.role == "breakout" for L in legs
                )
                fallen_open = any(
                    L.exit is None and L.side == f_side and L.role == "fallen" for L in legs
                )
                # Grace: need a few bars above TL before a break can book
                tl_age = (
                    fi - int(up_tl.get("anchor_fi") or fi)
                    if up_tl
                    else 0
                )
                if (
                    winner_open
                    and w_px is not None
                    and tl_age >= 5
                    and winner_broke_up_tl(
                        float(w_px), up_tl, fi, buffer=tl_buffer, armed=tl_armed
                    )
                ):
                    reason = (
                        f"winner broke up-TL {trendline_value(up_tl, fi):.1f}"
                    )
                    _close_side(legs, str(b), t, float(w_px), reason)
                    events.append(
                        {
                            "t": t,
                            "ev": "BOOK_WINNER",
                            "side": b,
                            "px": w_px,
                            "reason": reason,
                        }
                    )
                    winner_open = False
                    up_tl = None  # stop plotting up-TL after winner exit
                    st.message = f"booked {b}: {reason}"
                if (
                    fallen_open
                    and f_px is not None
                    and fallen_broke_down_tl(
                        float(f_px), down_tl, fi, buffer=tl_buffer, armed=tl_armed
                    )
                ):
                    reason = (
                        f"fallen broke down-TL {trendline_value(down_tl, fi):.1f}"
                    )
                    _close_side(legs, str(f_side), t, float(f_px), reason)
                    events.append(
                        {
                            "t": t,
                            "ev": "EXIT_FALLEN",
                            "side": f_side,
                            "px": f_px,
                            "reason": reason,
                        }
                    )
                    fallen_open = False
                    down_tl = None
                    st.message = f"closed fallen: {reason}"
                if not winner_open and not fallen_open:
                    keep_ce, keep_pe = st.ce_sym, st.pe_sym
                    keep_cs, keep_ps = st.ce_strike, st.pe_strike
                    st = SimState(message="TL cycle done")
                    st.ce_sym, st.pe_sym = keep_ce, keep_pe
                    st.ce_strike, st.pe_strike = keep_cs, keep_ps
                    st.locked = bool(locked_pair)
                    merge_streak = 0
                    merge_armed = False
                    up_tl = down_tl = None
                    tl_armed = False
                    tl_w_strike = tl_f_strike = None
                    tl_w_side = tl_f_side = None
                    band_break_side = None
                    band_break_count = 0
                elif not winner_open and fallen_open:
                    st.phase = "FALLEN_HOLD"
                    dn = trendline_value(down_tl, fi)
                    st.message = (
                        f"fallen {f_side} hold · down-TL {dn:.1f}"
                        if dn is not None
                        else f"fallen {f_side} hold"
                    )
                else:
                    up_v = trendline_value(up_tl, fi)
                    dn_v = trendline_value(down_tl, fi)
                    st.message = (
                        f"TL hold W {b}@{w_px} tl={up_v:.1f} · "
                        f"F {f_side}@{f_px} tl={dn_v:.1f}"
                        if up_v is not None and dn_v is not None and w_px and f_px
                        else "TL hold"
                    )

            elif st.phase == "BREAKOUT_WATCH":
                b = st.breakout_side
                px = ce_px if b == "CE" else pe_px
                # Opposite premium still watched for reverse-hammer exit (not bought)
                f_side = st.fallen_side or ("PE" if b == "CE" else "CE")
                f_px = pe_px if b == "CE" else ce_px
                entry = st.ce_entry if b == "CE" else st.pe_entry
                # entry was set to both; for breakout-only winner entry is buy px
                for L in legs:
                    if L.exit is None and L.side == b and L.role == "breakout":
                        entry = L.entry
                        break
                if st.breakout_peak is None or px > st.breakout_peak:
                    st.breakout_peak = px
                mult = px / entry if entry else 1.0
                book_reason = None
                # SL only after leave, then fall-back to last convergence mid
                conv_mid = last_convergence_mid if last_convergence_mid is not None else st.mid_at_entry
                leave = float(cfg.get("algo_cw_sl_leave_pts", 3.0))
                sl_below = float(cfg.get("algo_cw_sl_below_convergence", 0.0))
                if (
                    conv_mid is not None
                    and st.breakout_peak is not None
                    and float(st.breakout_peak) >= float(conv_mid) + leave
                    and px is not None
                    and float(px) <= float(conv_mid) - sl_below
                ):
                    stop_px = float(conv_mid) - sl_below
                    book_reason = (
                        f"SL fell back to convergence {px:.1f}≤{stop_px:.1f} "
                        f"(last mid {float(conv_mid):.1f})"
                    )
                # Refresh both-side channels while holding
                if tl_enabled and st.breakout_side in ("CE", "PE"):
                    if not tl_armed:
                        _arm_both_channels(str(st.breakout_side), fi)
                    elif tl_w_side and tl_f_side and tl_w_strike and tl_f_strike:
                        # Progressive correction on both channels
                        idxs_c: list[int] = []
                        w_vals_c: list[float] = []
                        f_vals_c: list[float] = []
                        for j in range(0, fi + 1):
                            wv = _leg_px(frames[j], str(tl_w_side), int(tl_w_strike))
                            fv = _leg_px(frames[j], str(tl_f_side), int(tl_f_strike))
                            if wv is None or fv is None:
                                continue
                            idxs_c.append(j)
                            w_vals_c.append(float(wv))
                            f_vals_c.append(float(fv))
                        if len(w_vals_c) >= 3:
                            end_i = len(w_vals_c) - 1
                            raw_u = correct_up_trendline(
                                None, w_vals_c, end_i=end_i, from_start=tl_from_start
                            )
                            raw_wd = correct_down_trendline(
                                None, w_vals_c, end_i=end_i, from_start=tl_from_start
                            )
                            raw_fu = correct_up_trendline(
                                None, f_vals_c, end_i=end_i, from_start=tl_from_start
                            )
                            raw_d = correct_down_trendline(
                                None, f_vals_c, end_i=end_i, from_start=tl_from_start
                            )
                            # Prefer advancing corrections mapped to frame space
                            nu = _frame_tl(raw_u, idxs_c)
                            nwd = _frame_tl(raw_wd, idxs_c)
                            nfu = _frame_tl(raw_fu, idxs_c)
                            nd = _frame_tl(raw_d, idxs_c)
                            if nu and (
                                up_tl is None
                                or int(nu.get("i1") or 0) >= int((up_tl or {}).get("i1") or -1)
                            ):
                                up_tl = nu
                            if nwd and (
                                w_dn_tl is None
                                or int(nwd.get("i1") or 0)
                                >= int((w_dn_tl or {}).get("i1") or -1)
                            ):
                                w_dn_tl = nwd
                            if nfu and (
                                f_up_tl is None
                                or int(nfu.get("i1") or 0)
                                >= int((f_up_tl or {}).get("i1") or -1)
                            ):
                                f_up_tl = nfu
                            if nd and (
                                down_tl is None
                                or int(nd.get("i1") or 0)
                                >= int((down_tl or {}).get("i1") or -1)
                            ):
                                down_tl = nd

                if book_reason is None and cfg.get("breakout_premium_target_enabled", True):
                    fallen_broke = bool(
                        f_px is not None
                        and down_tl
                        and fallen_broke_down_tl(
                            float(f_px),
                            down_tl,
                            fi,
                            buffer=tl_buffer,
                            armed=tl_armed,
                        )
                    )
                    book_reason = premium_target_hit(
                        float(px) if px is not None else 0.0,
                        target_low=t_lo,
                        target_high=t_hi,
                        fallen_tl_broke=fallen_broke,
                        require_fallen_tl=book_needs_fallen_tl,
                    )

                reverse_flip = False
                reverse_side: str | None = None

                if book_reason is None and cfg.get("breakout_fallen_rev_exit", True):
                    # Prefer real 1m OHLC wicks from chain; close-only prem_ticks
                    # collapse to o=h=l=c and never form hammers.
                    bars = lock_pe_bars if f_side == "PE" else lock_ce_bars
                    if len(bars) >= 2:
                        closed = bars[:-1]
                        sig = detect_fallen_reversal_candle(
                            closed, fallen_side=str(f_side)
                        )
                        if sig:
                            st.fallen_rev_arm = sig
                        # Break level on current bar close (or high for up-break)
                        break_px = f_px
                        cur = bars[-1]
                        if st.fallen_rev_arm and st.fallen_rev_arm.get("break_dir") == "up":
                            break_px = float(cur.get("h") or f_px or 0)
                        elif st.fallen_rev_arm and st.fallen_rev_arm.get("break_dir") == "down":
                            break_px = float(cur.get("l") or f_px or 0)
                        if (
                            st.fallen_rev_arm
                            and break_px is not None
                            and fallen_reversal_broken(
                                st.fallen_rev_arm, fallen_px=float(break_px)
                            )
                        ):
                            a = st.fallen_rev_arm
                            book_reason = (
                                f"fallen {a.get('kind')} candle break "
                                f"{a.get('break_dir')} {a.get('level')}"
                            )
                            if (
                                cfg.get("algo_cw_fallen_rev_flip", True)
                                and f_side in ("CE", "PE")
                                and cfg.get("algo_cw_breakout_only", True)
                            ):
                                reverse_flip = True
                                reverse_side = str(f_side)

                if book_reason is None and cfg.get("breakout_expand_stretch_enabled", True):
                    if mult >= expand_cap and px >= t_hi:
                        book_reason = f"expand stretch {mult:.1f}x"

                if book_reason is None and cfg.get("algo_cw_rail_reverse_enabled", True):
                    zone = None
                    if last_zone or frozen_pipe:
                        zone = last_zone or {
                            "pipe": (frozen_pipe or {}).get("pipe"),
                            "start_i": (frozen_pipe or {}).get("start_i") or 0,
                            "pipe_points": (frozen_pipe or {}).get("pipe_points"),
                        }
                    rr = detect_rail_reversal(
                        breakout_side=str(b or ""),
                        zone=zone,
                        ce=ce_px,
                        pe=pe_px,
                        hist_len=max(len(series_for_pipe), 1),
                        buffer=float(cfg.get("algo_cw_break_rail_buffer", 1.0)),
                        confirm_points=int(cfg.get("algo_cw_break_confirm_points", 3)),
                        confirm_side=reverse_confirm_side,
                        confirm_count=reverse_confirm_count,
                        winner_px_hist=reverse_winner_px,
                        hard_gap=hard_gap,
                    )
                    if rr.get("aborted"):
                        reverse_confirm_side = None
                        reverse_confirm_count = 0
                        reverse_winner_px = []
                        events.append(
                            {"t": t, "ev": "ABORT_REVERSE", "reason": rr.get("reason")}
                        )
                    elif rr.get("side"):
                        new_side = str(rr["side"])
                        if reverse_confirm_side and reverse_confirm_side != new_side:
                            reverse_winner_px = []
                        reverse_confirm_side = new_side
                        reverse_confirm_count = int(rr.get("confirm_count") or 0)
                        rpx = ce_px if new_side == "CE" else pe_px
                        if rpx is not None:
                            reverse_winner_px.append(float(rpx))
                            reverse_winner_px = reverse_winner_px[-12:]
                    if rr.get("ok") and rr.get("side") in ("CE", "PE"):
                        reverse_side = str(rr["side"])
                        book_reason = str(
                            rr.get("reason") or f"rail reverse → {reverse_side}"
                        )
                        reverse_flip = bool(cfg.get("algo_cw_rail_reverse_flip", True))

                if book_reason:
                    _close_side(legs, b, t, px, book_reason)
                    flip_px = (
                        (ce_px if reverse_side == "CE" else pe_px)
                        if reverse_side in ("CE", "PE")
                        else None
                    )
                    # Flip-buy only in breakout-only mode; dual already holds fallen
                    can_flip = (
                        bool(cfg.get("algo_cw_breakout_only", True))
                        and reverse_flip
                        and reverse_side in ("CE", "PE")
                        and flip_px is not None
                        and band_lo <= float(flip_px) <= band_hi
                    )
                    if can_flip:
                        events.append(
                            {
                                "t": t,
                                "ev": "REVERSAL",
                                "side": b,
                                "flip": reverse_side,
                                "px": px,
                                "reason": book_reason,
                                "via": (
                                    "fallen_candle"
                                    if "fallen" in str(book_reason)
                                    else "rail"
                                ),
                            }
                        )
                        st.breakout_side = reverse_side
                        st.fallen_side = None
                        st.ce_entry, st.pe_entry = ce_px, pe_px
                        # Keep last convergence mid — SL waits for leave then fall-back
                        st.mid_at_entry = last_convergence_mid
                        st.breakout_peak = flip_px
                        st.breakout_t = t
                        st.lean_side = reverse_side
                        st.fallen_rev_arm = None
                        st.phase = "BREAKOUT_WATCH"
                        st.message = (
                            f"REVERSAL {b}→{reverse_side} @{flip_px:.1f}"
                        )
                        legs.append(
                            Leg(
                                reverse_side,
                                "breakout",
                                t,
                                float(flip_px),
                                lots,
                                st.ce_sym if reverse_side == "CE" else st.pe_sym or "",
                                st.ce_strike if reverse_side == "CE" else st.pe_strike,
                            )
                        )
                        events.append(
                            {
                                "t": t,
                                "ev": "BUY_BREAKOUT",
                                "side": reverse_side,
                                "px": flip_px,
                                "reason": "reverse flip in-band",
                            }
                        )
                        reverse_confirm_side = None
                        reverse_confirm_count = 0
                        reverse_winner_px = []
                        rail_confirm_side = None
                        rail_confirm_count = 0
                        rail_winner_px = []
                        if tl_enabled:
                            _arm_both_channels(str(reverse_side), fi)
                    else:
                        if reverse_flip and reverse_side in ("CE", "PE"):
                            events.append(
                                {
                                    "t": t,
                                    "ev": "SKIP_FLIP",
                                    "side": b,
                                    "flip": reverse_side,
                                    "px": flip_px,
                                    "reason": (
                                        f"flip {reverse_side}@{flip_px} outside "
                                        f"{band_lo:g}–{band_hi:g}"
                                    ),
                                }
                            )
                        events.append(
                            {
                                "t": t,
                                "ev": "BOOK_BREAKOUT",
                                "side": b,
                                "px": px,
                                "reason": book_reason,
                            }
                        )
                        st.fallen_rev_arm = None
                        reverse_confirm_side = None
                        reverse_confirm_count = 0
                        reverse_winner_px = []
                        if cfg.get("algo_cw_breakout_only", True):
                            # Preserve lock symbols for next cycle; reset phase
                            keep_ce, keep_pe = st.ce_sym, st.pe_sym
                            keep_cs, keep_ps = st.ce_strike, st.pe_strike
                            st = SimState(message=f"booked {b}: {book_reason}")
                            st.ce_sym, st.pe_sym = keep_ce, keep_pe
                            st.ce_strike, st.pe_strike = keep_cs, keep_ps
                            st.locked = bool(locked_pair)
                            merge_streak = 0
                            merge_armed = False
                            peak_gap_seen = 0.0
                            expansion_leader = None
                            rail_confirm_side = None
                            rail_confirm_count = 0
                            rail_winner_px = []
                            up_tl = down_tl = w_dn_tl = f_up_tl = None
                            ce_hi_tl = ce_lo_tl = pe_hi_tl = pe_lo_tl = None
                            tl_armed = False
                            tl_w_strike = tl_f_strike = None
                            tl_w_side = tl_f_side = None
                            # keep lock for possible next cycle same day
                        else:
                            st.phase = "FALLEN_HOLD"
                            st.message = f"booked {b}: {book_reason}"
                else:
                    arm = st.fallen_rev_arm
                    arm_txt = f" | arm {arm.get('kind')}@{arm.get('level')}" if arm else ""
                    rev_txt = ""
                    if reverse_confirm_side:
                        rev_txt = f" | rev {reverse_confirm_side}{reverse_confirm_count}"
                    st.message = (
                        f"breakout {b} {px:.1f} peak {st.breakout_peak:.1f} "
                        f"tgt {t_lo:g}–{t_hi:g}{arm_txt}{rev_txt}"
                    )

            elif st.phase == "FALLEN_HOLD" and not cfg.get("algo_cw_breakout_only", True):
                f_side = st.fallen_side
                f_px = pe_px if f_side == "PE" else ce_px
                f_entry = st.pe_entry if f_side == "PE" else st.ce_entry
                if f_px is None:
                    st.message = f"fallen {f_side} — waiting quote"
                else:
                    if st.fallen_low is None or f_px < st.fallen_low:
                        st.fallen_low = f_px
                    reason = None
                    stop_line = (st.fallen_low or f_px) - sl_pts
                    mid_ref = float(
                        last_convergence_mid
                        if last_convergence_mid is not None
                        else (st.mid_at_entry or 135)
                    )
                    low = float(st.fallen_low or f_px)
                    up_from_low = float(f_px) - low
                    # Primary: break above falling down-TL (retrace)
                    if (
                        tl_enabled
                        and down_tl
                        and fallen_broke_down_tl(
                            float(f_px),
                            down_tl,
                            fi,
                            buffer=tl_buffer,
                            armed=True,
                        )
                    ):
                        reason = (
                            f"fallen broke down-TL "
                            f"{trendline_value(down_tl, fi):.1f}"
                        )
                    elif (
                        cfg.get("algo_cw_fallen_ride_enabled", True)
                        and f_side in ("CE", "PE")
                        and up_from_low >= bounce
                        and float(f_px) >= mid_ref - 2.0
                        and float(f_px) >= band_lo
                    ):
                        st.breakout_side = f_side
                        st.fallen_side = None
                        st.breakout_peak = f_px
                        st.breakout_t = t
                        st.mid_at_entry = last_convergence_mid or st.mid_at_entry
                        st.fallen_low = None
                        st.fallen_rev_arm = None
                        st.phase = "BREAKOUT_WATCH"
                        for L in legs:
                            if L.exit is None and L.side == f_side:
                                L.role = "breakout"
                        events.append(
                            {
                                "t": t,
                                "ev": "FALLEN_RIDE",
                                "side": f_side,
                                "px": f_px,
                                "reason": "fallen reverse → ride",
                            }
                        )
                        st.message = f"FALLEN RIDE {f_side} @{f_px:.1f}"
                    elif f_px <= 50:
                        reason = "fallen deep crush ≤50"
                    elif f_px <= stop_line:
                        reason = "fallen SL"
                    elif not cfg.get("algo_cw_fallen_ride_enabled", True):
                        if up_from_low >= bounce:
                            reason = f"fallen reverse +{up_from_low:.0f} from low"
                        elif f_px >= mid_ref - 2.0:
                            reason = "fallen back to convergence"
                        elif f_entry is not None and f_px >= f_entry + bounce:
                            reason = f"fallen bounce +{bounce:g}"
                    elif f_px <= crush_cut:
                        reason = f"crush cut @{f_px:.1f}"

                    if reason:
                        _close_side(legs, f_side, t, f_px, reason)
                        events.append(
                            {
                                "t": t,
                                "ev": "EXIT_FALLEN",
                                "side": f_side,
                                "px": f_px,
                                "reason": reason,
                            }
                        )
                        keep_ce, keep_pe = st.ce_sym, st.pe_sym
                        keep_cs, keep_ps = st.ce_strike, st.pe_strike
                        st = SimState(message=reason)
                        st.ce_sym, st.pe_sym = keep_ce, keep_pe
                        st.ce_strike, st.pe_strike = keep_cs, keep_ps
                        st.locked = bool(locked_pair)
                        merge_streak = 0
                        merge_armed = False
                        down_tl = None
                        up_tl = None
                        tl_armed = False
                    elif st.phase == "FALLEN_HOLD":
                        dn = trendline_value(down_tl, fi) if down_tl else None
                        st.message = (
                            f"fallen {f_side} {f_px:.1f} low {st.fallen_low:.1f} "
                            f"down-TL {dn:.1f}"
                            if dn is not None
                            else (
                                f"fallen {f_side} {f_px:.1f} low {st.fallen_low:.1f} "
                                f"mid~{mid_ref:.0f}"
                            )
                        )

        # One-sided dual target book only when TL mode is off
        if (
            st.phase == "DUAL_OPEN"
            and not tl_enabled
            and not cfg.get("algo_cw_breakout_only", True)
            and ((ce_px is None) ^ (pe_px is None))
            and last_ce_px is not None
            and last_pe_px is not None
            and st.ce_entry is not None
            and st.pe_entry is not None
        ):
            use_ce = float(ce_px if ce_px is not None else last_ce_px)
            use_pe = float(pe_px if pe_px is not None else last_pe_px)
            ready = dual_book_winner(
                use_ce, use_pe, float(st.ce_entry), float(st.pe_entry), cfg
            )
            if ready and ready.get("side") in ("CE", "PE"):
                side = str(ready["side"])
                live_px = ce_px if side == "CE" else pe_px
                if live_px is not None:
                    b_px = float(live_px)
                    f_side = "PE" if side == "CE" else "CE"
                    book_reason = str(ready.get("reason") or "dual book")
                    for L in legs:
                        if L.exit is not None:
                            continue
                        L.role = "breakout" if L.side == side else "fallen"
                    _close_side(legs, side, t, b_px, book_reason)
                    events.append(
                        {
                            "t": t,
                            "ev": "BOOK_WINNER",
                            "side": side,
                            "px": b_px,
                            "reason": book_reason,
                            "one_sided": True,
                        }
                    )
                    st.breakout_side = side
                    st.fallen_side = f_side
                    st.breakout_peak = b_px
                    st.breakout_t = t
                    other = last_pe_px if side == "CE" else last_ce_px
                    st.fallen_low = float(other) if other is not None else None
                    st.phase = "FALLEN_HOLD"
                    st.message = (
                        f"booked {side}: {book_reason}; holding fallen (one-sided)"
                    )

        # One-sided fallen hold (only fallen leg still quoting)
        if (
            st.phase == "FALLEN_HOLD"
            and not cfg.get("algo_cw_breakout_only", True)
            and st.fallen_side in ("CE", "PE")
            and ((ce_px is None) ^ (pe_px is None))
        ):
            f_side = st.fallen_side
            f_px = pe_px if f_side == "PE" else ce_px
            if f_px is not None:
                if st.fallen_low is None or f_px < st.fallen_low:
                    st.fallen_low = f_px
                mid_ref = float(
                    last_convergence_mid
                    if last_convergence_mid is not None
                    else (st.mid_at_entry or 135)
                )
                low = float(st.fallen_low or f_px)
                up_from_low = float(f_px) - low
                stop_line = low - sl_pts
                reason = None
                if f_px <= 50:
                    reason = "fallen deep crush ≤50"
                elif f_px <= stop_line:
                    reason = "fallen SL"
                elif up_from_low >= bounce:
                    reason = f"fallen reverse +{up_from_low:.0f} from low"
                elif f_px >= mid_ref - 2.0:
                    reason = "fallen back to convergence"
                if reason:
                    _close_side(legs, f_side, t, f_px, reason)
                    events.append(
                        {
                            "t": t,
                            "ev": "EXIT_FALLEN",
                            "side": f_side,
                            "px": f_px,
                            "reason": reason,
                        }
                    )
                    keep_ce, keep_pe = st.ce_sym, st.pe_sym
                    keep_cs, keep_ps = st.ce_strike, st.pe_strike
                    st = SimState(message=reason)
                    st.ce_sym, st.pe_sym = keep_ce, keep_pe
                    st.ce_strike, st.pe_strike = keep_cs, keep_ps
                    st.locked = bool(locked_pair)
                    merge_streak = 0
                    merge_armed = False

        # One-sided quotes (Nov-11: CE quote dies ~12:07, PE runs) — keep confirm alive
        if (
            locked_pair
            and st.phase == "SCAN"
            and merge_armed
            and cfg.get("algo_cw_breakout_only", True)
            and ((ce_px is None) ^ (pe_px is None))
        ):
            confirm_n = int(cfg.get("algo_cw_break_confirm_points", 3))
            zone = last_zone or (
                {
                    "pipe": (frozen_pipe or {}).get("pipe"),
                    "start_i": (frozen_pipe or {}).get("start_i") or 0,
                }
                if frozen_pipe
                else None
            )
            # One-sided: align hist_len so rel_i = fi - start_fi (not series length)
            start_fi_z = (frozen_pipe or {}).get("start_fi")
            start_i_z = int((zone or {}).get("start_i") or (frozen_pipe or {}).get("start_i") or 0)
            if start_fi_z is not None:
                hist_len_1s = fi - int(start_fi_z) + start_i_z + 1
            else:
                hist_len_1s = max(len(series_for_pipe), fi + 1)
            rb = detect_rail_breakout(
                zone=zone,
                ce=ce_px,
                pe=pe_px,
                hist_len=hist_len_1s,
                buffer=float(cfg.get("algo_cw_break_rail_buffer", 1.0)),
                confirm_points=confirm_n,
                confirm_side=rail_confirm_side,
                confirm_count=rail_confirm_count,
                winner_px_hist=rail_winner_px,
                hard_gap=hard_gap,
            )
            if rb.get("aborted"):
                rail_confirm_side = None
                rail_confirm_count = 0
                rail_winner_px = []
                events.append({"t": t, "ev": "ABORT_CONFIRM", "reason": rb.get("reason")})
            elif rb.get("side"):
                new_side = str(rb["side"])
                if rail_confirm_side and rail_confirm_side != new_side:
                    rail_winner_px = []
                rail_confirm_side = new_side
                rail_confirm_count = int(rb.get("confirm_count") or 0)
                wpx = ce_px if new_side == "CE" else pe_px
                if wpx is not None:
                    rail_winner_px.append(wpx)
                    rail_winner_px = rail_winner_px[-12:]
            if rb.get("ok") and rb.get("side") in ("CE", "PE"):
                side = str(rb["side"])
                px = ce_px if side == "CE" else pe_px
                if px is not None and band_lo <= float(px) <= band_hi:
                    st.phase = "BREAKOUT_WATCH"
                    st.breakout_side = side
                    st.fallen_side = None
                    st.breakout_peak = px
                    st.breakout_t = t
                    st.lean_side = side
                    if side == "CE":
                        st.ce_entry = px
                    else:
                        st.pe_entry = px
                    # Convergence mid for SL (use last dual mid / pipe mid if available)
                    if st.mid_at_entry is None:
                        if last_convergence_mid is not None:
                            st.mid_at_entry = last_convergence_mid
                        elif ce_px is not None and pe_px is not None:
                            st.mid_at_entry = (ce_px + pe_px) / 2
                        elif frozen_pipe and (frozen_pipe.get("pipe") or {}).get("intercept") is not None:
                            st.mid_at_entry = float((frozen_pipe.get("pipe") or {}).get("intercept"))
                    legs.append(
                        Leg(
                            side,
                            "breakout",
                            t,
                            px,
                            lots,
                            st.ce_sym if side == "CE" else st.pe_sym or "",
                            st.ce_strike if side == "CE" else st.pe_strike,
                        )
                    )
                    events.append(
                        {"t": t, "ev": "RAIL_BREAK", "side": side, "reason": rb.get("reason")}
                    )
                    events.append({"t": t, "ev": "BUY_BREAKOUT", "side": side, "px": px})
                    st.message = f"BOUGHT {side} @{px:.1f} only"
                    range_frozen = True
                elif px is not None:
                    st.message = f"skip {side}@{px:.1f} outside {band_lo:g}–{band_hi:g}"
            elif rb.get("pending"):
                st.message = str(rb.get("reason") or "confirming one-sided")

        # Mark open MTM on frame
        open_legs = []
        for L in legs:
            if L.exit is not None:
                continue
            px = ce_px if L.side == "CE" else pe_px
            if px is None:
                continue
            open_legs.append(
                {
                    "side": L.side,
                    "role": L.role,
                    "entry": L.entry,
                    "px": px,
                    "mtm": round((px - L.entry) * L.qty(lot_size), 2),
                }
            )

        # Map / project wedge rails onto this minute (frame-anchored, not series-index)
        pipe_high = pipe_mid = pipe_low = None
        t_unix = 1_700_000_000 + fi * 60
        # Prefer ATM chart pipe so the real mid~185 channel shows on expiry / rich days
        if chart_overlay and atm_frozen_pipe:
            chart_src = atm_frozen_pipe
        elif chart_overlay and atm_last_zone and (
            atm_last_zone.get("pipe") or atm_last_zone.get("pipe_points")
        ):
            s_i = int(atm_last_zone.get("start_i") or 0)
            start_fi_live = fi
            if 0 <= s_i < len(atm_series_for_pipe):
                start_fi_live = int(
                    (int(atm_series_for_pipe[s_i]["time"]) - 1_700_000_000) // 60
                )
            chart_src = {
                "pipe": atm_last_zone.get("pipe"),
                "pipe_points": atm_last_zone.get("pipe_points"),
                "start_i": s_i,
                "start_fi": start_fi_live,
            }
        else:
            chart_src = frozen_pipe
            # While still scanning, prefer live last_zone so channel tracks the merge
            if not range_frozen and last_zone and (
                last_zone.get("pipe") or last_zone.get("pipe_points")
            ):
                s_i = int(last_zone.get("start_i") or 0)
                start_fi_live = fi
                if 0 <= s_i < len(series_for_pipe):
                    start_fi_live = int(
                        (int(series_for_pipe[s_i]["time"]) - 1_700_000_000) // 60
                    )
                chart_src = {
                    "pipe": last_zone.get("pipe"),
                    "pipe_points": last_zone.get("pipe_points"),
                    "start_i": s_i,
                    "start_fi": start_fi_live,
                }
        if chart_src and chart_src.get("pipe"):
            from foxtrade.convergence_zone import project_rails_at

            start_fi = chart_src.get("start_fi")
            if start_fi is None:
                # fallback: derive from first pipe_point time
                pps0 = chart_src.get("pipe_points") or []
                if pps0:
                    start_fi = int((int(pps0[0]["time"]) - 1_700_000_000) // 60)
                else:
                    start_fi = int(chart_src.get("start_i") or 0)
            start_fi = int(start_fi)
            pps = chart_src.get("pipe_points") or []
            hit = next((p for p in pps if int(p["time"]) == t_unix), None)
            if hit is not None:
                pipe_high, pipe_mid, pipe_low = hit["high"], hit["mid"], hit["low"]
            elif fi >= start_fi:
                end_fi = chart_src.get("end_fi")
                clamp = None
                if end_fi is not None:
                    clamp = max(0, int(end_fi) - start_fi)
                rails = project_rails_at(
                    chart_src.get("pipe") or {},
                    rel_i=fi - start_fi,
                    clamp_rel=clamp,
                )
                if rails:
                    pipe_high, pipe_mid, pipe_low = (
                        rails["high"],
                        rails["mid"],
                        rails["low"],
                    )
        elif chart_src and chart_src.get("pipe_points"):
            pps = chart_src["pipe_points"]
            hit = next((p for p in pps if int(p["time"]) == t_unix), None)
            if hit:
                pipe_high, pipe_mid, pipe_low = hit["high"], hit["mid"], hit["low"]

        # Chart lines: ATM premiums when enabled (algo lock may be a different OTM band pair)
        plot_ce = atm_ce_px if chart_overlay and atm_ce_px is not None else ce_px
        plot_pe = atm_pe_px if chart_overlay and atm_pe_px is not None else pe_px
        out_frames.append(
            {
                "i": fi,
                "t": t,
                "spot": spot_c,
                "atm": atm,
                "chart_strike": chart_k,
                "chart_ce_strike": band_pair[0] if band_pair else chart_k,
                "chart_pe_strike": band_pair[1] if band_pair else chart_k,
                "candle": candle.get("color"),
                "gate_ok": bool(gate.get("ok")),
                "phase": st.phase,
                "message": st.message,
                "locked": bool(locked_pair),
                "ce_strike": st.ce_strike or (locked_pair or {}).get("ce_strike"),
                "pe_strike": st.pe_strike or (locked_pair or {}).get("pe_strike"),
                "ce": plot_ce,
                "pe": plot_pe,
                "lock_ce": ce_px,
                "lock_pe": pe_px,
                "atm_ce": atm_ce_px,
                "atm_pe": atm_pe_px,
                "gap": None
                if plot_ce is None or plot_pe is None
                else round(abs(plot_ce - plot_pe), 2),
                "breakout_side": st.breakout_side,
                "lean_side": st.lean_side,
                "open_legs": open_legs,
                "conv_mid": (
                    round(float(last_convergence_mid), 2)
                    if last_convergence_mid is not None
                    else (
                        round(float(st.mid_at_entry), 2)
                        if st.mid_at_entry is not None
                        else None
                    )
                ),
                "band_lo": band_lo,
                "band_hi": band_hi,
                "tgt_lo": t_lo,
                "tgt_hi": t_hi,
                "pipe_high": pipe_high,
                "pipe_mid": pipe_mid,
                "pipe_low": pipe_low,
                "tl_up": (
                    round(trendline_value(up_tl, fi), 2)
                    if up_tl and tl_armed
                    else None
                ),
                "tl_down": (
                    round(trendline_value(down_tl, fi), 2)
                    if down_tl and tl_armed
                    else None
                ),
                "tl_w_dn": (
                    round(trendline_value(w_dn_tl, fi), 2)
                    if w_dn_tl and tl_armed
                    else None
                ),
                "tl_f_up": (
                    round(trendline_value(f_up_tl, fi), 2)
                    if f_up_tl and tl_armed
                    else None
                ),
                "ce_ch_hi": None,
                "ce_ch_lo": None,
                "pe_ch_hi": None,
                "pe_ch_lo": None,
                "event": events[-1] if events and events[-1].get("t") == t else None,
            }
        )
        # Fill CE/PE parallel channel levels (hi >= lo)
        _fr = out_frames[-1]
        _ce_h = round(trendline_value(ce_hi_tl, fi), 2) if ce_hi_tl else None
        _ce_l = round(trendline_value(ce_lo_tl, fi), 2) if ce_lo_tl else None
        _pe_h = round(trendline_value(pe_hi_tl, fi), 2) if pe_hi_tl else None
        _pe_l = round(trendline_value(pe_lo_tl, fi), 2) if pe_lo_tl else None
        if _ce_h is not None and _ce_l is not None:
            _fr["ce_ch_hi"], _fr["ce_ch_lo"] = max(_ce_h, _ce_l), min(_ce_h, _ce_l)
        else:
            _fr["ce_ch_hi"], _fr["ce_ch_lo"] = _ce_h, _ce_l
        if _pe_h is not None and _pe_l is not None:
            _fr["pe_ch_hi"], _fr["pe_ch_lo"] = max(_pe_h, _pe_l), min(_pe_h, _pe_l)
        else:
            _fr["pe_ch_hi"], _fr["pe_ch_lo"] = _pe_h, _pe_l

    # Backfill / extend channel from frozen merge only (do not invent pre-lock rails)
    backfill_pipe = atm_frozen_pipe if (chart_overlay and atm_frozen_pipe) else frozen_pipe
    if backfill_pipe and (backfill_pipe.get("pipe") or backfill_pipe.get("pipe_points")):
        from foxtrade.convergence_zone import project_rails_at

        by_t = {int(p["time"]): p for p in (backfill_pipe.get("pipe_points") or [])}
        start_fi = backfill_pipe.get("start_fi")
        if start_fi is None and by_t:
            start_fi = int((min(by_t) - 1_700_000_000) // 60)
        if start_fi is None:
            start_fi = int(backfill_pipe.get("start_i") or 0)
        start_fi = int(start_fi)
        end_fi = backfill_pipe.get("end_fi")
        if end_fi is None and by_t:
            end_fi = int((max(by_t) - 1_700_000_000) // 60)
        pipe = backfill_pipe.get("pipe") or {}
        # Clear accidental pre-start paints, then paint merge→EOD from frozen pipe
        for fr in out_frames:
            fi2 = int(fr["i"])
            if fi2 < start_fi:
                fr["pipe_high"] = fr["pipe_mid"] = fr["pipe_low"] = None
                continue
            tu = 1_700_000_000 + fi2 * 60
            hit = by_t.get(tu)
            if hit:
                fr["pipe_high"] = hit["high"]
                fr["pipe_mid"] = hit["mid"]
                fr["pipe_low"] = hit["low"]
            elif pipe:
                clamp = None
                if end_fi is not None:
                    clamp = max(0, int(end_fi) - start_fi)
                rails = project_rails_at(
                    pipe, rel_i=fi2 - start_fi, clamp_rel=clamp
                )
                if rails:
                    fr["pipe_high"] = round(rails["high"], 2)
                    fr["pipe_mid"] = round(rails["mid"], 2)
                    fr["pipe_low"] = round(rails["low"], 2)

    # EOD flat any open (use last known premium if final bar missing that strike)
    if frames:
        last = frames[-1]
        t = last["t"]
        for L in legs:
            if L.exit is not None:
                continue
            px = _leg_px(last, L.side, int(L.strike or 0))
            exit_t = t
            if px is None:
                for fr in reversed(frames):
                    px = _leg_px(fr, L.side, int(L.strike or 0))
                    if px is not None:
                        exit_t = fr["t"]
                        break
            if px is None:
                continue
            L.exit_t, L.exit, L.reason = exit_t, px, "EOD flat"
            events.append({"t": exit_t, "ev": "EOD", "side": L.side, "px": px})

    trade_rows = []
    gross = 0.0
    charges = 0.0
    for L in legs:
        pnl = L.pnl(lot_size)
        gross += pnl
        ch = 0.0
        if L.exit is not None:
            try:
                ch = float(
                    estimate_option_charges(
                        L.entry, L.exit, L.qty(lot_size), cfg=cfg
                    ).get("charges")
                    or 0
                )
            except Exception:
                ch = 0.0
        charges += ch
        trade_rows.append(
            {
                **{k: getattr(L, k) for k in (
                    "side", "role", "entry_t", "entry", "lots", "symbol", "strike",
                    "exit_t", "exit", "reason",
                )},
                "pnl": round(pnl, 2),
                "charges": round(ch, 2),
                "net": round(pnl - ch, 2),
            }
        )

    mode = "breakout_only" if cfg.get("algo_cw_breakout_only", True) else "dual"
    summary = {
        "day": day,
        "expiry": meta.get("expiry"),
        "mode": mode,
        "frames": len(out_frames),
        "trades": len(trade_rows),
        "gross": round(gross, 2),
        "charges": round(charges, 2),
        "net": round(gross - charges, 2),
        "events": len(events),
        "lot_size": lot_size,
        "lots": lots,
    }
    return {
        "day": day,
        "meta": meta,
        "summary": summary,
        "events": events,
        "trades": trade_rows,
        "frames": out_frames,
        "cheat": False,
        "mode": mode,
        "note": "1m bar-by-bar; breakout side only; no future bars",
    }

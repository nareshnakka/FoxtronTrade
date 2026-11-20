from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from foxtrade import broker
from foxtrade import trade_store
from foxtrade.convergence_zone import (
    detect_convergence_zone,
    detect_entry_above_convergence,
    detect_rail_breakout,
    detect_rail_reversal,
    in_convergence_zone,
    pipe_from_live_series,
)
from foxtrade.early_winner_lean import detect_early_winner_lean, spot_1m_candle_side
from foxtrade.expert_filters import size_dual_pair_lots
from foxtrade.learner import build_entry_features
from foxtrade.breakout_trendline import (
    _local_extrema,
    bind_tl_to_frame,
    correct_down_trendline,
    correct_up_trendline,
    fallen_broke_down_tl,
    fit_down_trendline,
    fit_parallel_channel,
    fit_premium_channel,
    fit_up_trendline,
    trendline_value,
    winner_broke_up_tl,
)
from foxtrade.premium_exit_signals import (
    build_ohlc_bars,
    detect_fallen_reversal_candle,
    dual_book_winner,
    fallen_reversal_broken,
    premium_target_hit,
)
from foxtrade.scanner import expiry_allowed_for_entry, find_next_week_option, is_converged
from foxtrade.session_rhythm import compute_rhythm, rhythm_payload, rhythm_tag
from foxtrade.session_trend import compute_session_trend

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Algo2State:
    phase: str = "SCAN"
    pair: dict[str, Any] | None = None
    ce_trade_id: str | None = None
    pe_trade_id: str | None = None
    ce_symbol: str | None = None
    pe_symbol: str | None = None
    ce_entry: float | None = None
    pe_entry: float | None = None
    breakout_side: str | None = None
    fallen_side: str | None = None
    breakout_trade_id: str | None = None
    fallen_trade_id: str | None = None
    breakout_peak: float | None = None
    breakout_at: datetime | None = None
    fallen_low: float | None = None
    # After pair-exit: hold fallen (<45) for +5–10 bounce instead of selling with winner
    pair_fallen_hold: bool = False
    mid_at_entry: float | None = None
    # Last tight CE≈PE merge mid — SL only when breakout falls back here
    last_convergence_mid: float | None = None
    # First accepted same-day convergence mid — higher re-merges = decay (skip)
    day_conv_date: str | None = None
    day_conv_mid: float | None = None
    setup_id: str | None = None
    last_message: str = ""
    notification: dict[str, Any] | None = None
    ce_hist: list[float] = field(default_factory=list)
    pe_hist: list[float] = field(default_factory=list)
    conv_zone: dict[str, Any] | None = None
    fallen_rolled: bool = False
    fallen_roll_from: str | None = None
    fallen_roll_to: str | None = None
    fallen_strike: float | None = None
    fallen_expiry: str | None = None
    rhythm: dict[str, Any] | None = None
    rhythm_date: str | None = None
    live_series: list[dict[str, Any]] = field(default_factory=list)
    live_series_ts: float = 0.0
    tl_replot_ts: float = 0.0  # last CE/PE channel re-fit (wall clock)
    lean_side: str | None = None
    lean_added: bool = False
    lean_signal: dict[str, Any] | None = None
    spot_hist: list[float] = field(default_factory=list)
    atm_ce_hist: list[float] = field(default_factory=list)
    atm_pe_hist: list[float] = field(default_factory=list)
    # Timed premium ticks for 30s/1m fallen-reversal exits
    prem_ticks: list[dict[str, Any]] = field(default_factory=list)
    fallen_rev_arm: dict[str, Any] | None = None
    # Frozen tilted merge pipe for chart (keep drawing after breakout)
    pipe_chart: dict[str, Any] | None = None
    merge_band_high: float | None = None
    merge_band_low: float | None = None
    # live_series index of the dual convergence / entry — TL/channels start here
    conv_series_i: int | None = None
    up_tl: dict[str, Any] | None = None  # winner support (lows)
    down_tl: dict[str, Any] | None = None  # fallen resistance (highs)
    # Full channel on both premiums (highs + lows)
    w_dn_tl: dict[str, Any] | None = None  # winner resistance (highs)
    f_up_tl: dict[str, Any] | None = None  # fallen support (lows)
    # Explicit Call / Put parallel channels (same as backtest chart)
    ce_hi_tl: dict[str, Any] | None = None
    ce_lo_tl: dict[str, Any] | None = None
    pe_hi_tl: dict[str, Any] | None = None
    pe_lo_tl: dict[str, Any] | None = None
    # Index (Nifty) parallel channel + horizontal S/R (dotted on Nifty chart)
    ix_hi_tl: dict[str, Any] | None = None
    ix_lo_tl: dict[str, Any] | None = None
    ix_candles: list[dict[str, Any]] = field(default_factory=list)
    index_overlay: dict[str, Any] | None = None
    # Defer 1.45× book while Index channel break favors the winner
    index_trend_hold: bool = False
    index_hold_side: str | None = None
    index_hold_peak_mult: float = 0.0
    # First exit: arm at pair-exit ×, wait 5–30% peak retrace (or fallen reverse)
    pair_retrace_armed: bool = False
    pair_retrace_side: str | None = None
    pair_retrace_peak_mult: float = 0.0
    pair_retrace_peak_ltp: float = 0.0
    pair_retrace_fallen_low: float | None = None
    # After winner booked: watch sold-winner LTP for reverse → dump fallen
    sold_winner_side: str | None = None
    sold_winner_exit: float | None = None
    sold_winner_peak: float | None = None
    tl_armed: bool = False
    tl_tick: int = 0
    tl_arm_tick: int = 0  # tick when TLs armed — grace before winner exit
    # After first merge sighting — stay armed so hard-gap breakout can fire after split
    merge_armed: bool = False
    peak_gap_seen: float = 0.0
    expansion_leader: str | None = None
    # Rail-break confirm (N successive up-points after upper-rail punch)
    rail_confirm_side: str | None = None
    rail_confirm_count: int = 0
    rail_winner_px: list[float] = field(default_factory=list)
    # Opposite-rail reverse confirm while holding breakout
    reverse_confirm_side: str | None = None
    reverse_confirm_count: int = 0
    reverse_winner_px: list[float] = field(default_factory=list)


class Algo2Dual:
    """Watch CE/PE merge → buy both (or breakout) → book at pair-exit mult."""

    def __init__(
        self,
        algo_id: str = "algo2",
        *,
        display_name: str | None = None,
        week_mode: str = "current",
    ) -> None:
        self.algo_id = algo_id
        self.display_name = display_name or (
            "Convergence Next-Week" if algo_id == "algo2nw" else "Convergence Algo"
        )
        self.week_mode = week_mode if week_mode in ("current", "next") else "current"
        self.state = Algo2State()

    @staticmethod
    def _as_map(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _pair_leg(pair: Any, side: str) -> dict[str, Any]:
        """Normalize CE/PE leg — scanners usually return dicts; tolerate bare symbol strings."""
        if not isinstance(pair, dict):
            return {}
        key = "ce" if str(side).upper() == "CE" else "pe"
        leg = pair.get(key)
        if isinstance(leg, dict):
            return leg
        if isinstance(leg, str) and leg.strip():
            return {"tradingsymbol": leg.strip()}
        return {}

    @classmethod
    def _normalize_pair(cls, pair: Any) -> dict[str, Any] | None:
        if not isinstance(pair, dict):
            return None
        ce = cls._pair_leg(pair, "CE")
        pe = cls._pair_leg(pair, "PE")
        if not (ce.get("tradingsymbol") or pe.get("tradingsymbol")):
            # Keep non-leg metadata only when at least one structured leg exists
            if not (pair.get("ce") or pair.get("pe")):
                return None
        out = dict(pair)
        if ce:
            out["ce"] = ce
        if pe:
            out["pe"] = pe
        return out

    def resolve_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Per-book capital + next-week profile (band 46–54 · 1.70× · max 1 setup)."""
        out = dict(cfg)
        base_cap = float(cfg.get("capital_inr") or 100_000)
        base_dep = float(cfg.get("deploy_pct_of_balance") or 1.0)
        if self.algo_id == "algo2nw":
            out["premium_low"] = float(cfg.get("algo2nw_premium_low", 46) or 46)
            out["premium_high"] = float(cfg.get("algo2nw_premium_high", 54) or 54)
            out["algo2_pair_exit_mult"] = float(cfg.get("algo2nw_pair_exit_mult", 1.70) or 1.70)
            out["algo2_max_setups_per_day"] = int(cfg.get("algo2nw_max_setups_per_day", 1) or 1)
            out["algo2_enabled"] = bool(cfg.get("algo2nw_enabled", True))
            out["algo2_second_entry_after"] = "23:59"
            out["algo2_last_entry_before"] = "00:00"
            out["algo2_skip_second_if_day_pnl_ge"] = 0.0
            out["algo2_index_channel_hold"] = bool(cfg.get("algo2nw_index_channel_hold", False))
            out["algo2_pair_exit_wait_retrace"] = False
            if cfg.get("algo2nw_lots") is not None:
                out["algo2_lots"] = int(cfg.get("algo2nw_lots") or out.get("algo2_lots") or 1)
            if cfg.get("algo2nw_dual_size_from_capital") is not None:
                out["algo2nw_dual_size_from_capital"] = bool(
                    cfg.get("algo2nw_dual_size_from_capital")
                )
                out["algo2_dual_size_from_capital"] = out["algo2nw_dual_size_from_capital"]
            nw_cap = cfg.get("algo2nw_capital_inr")
            if nw_cap is None:
                nw_cap = cfg.get("algo2_capital_inr")
            out["capital_inr"] = float(nw_cap if nw_cap is not None else base_cap)
            nw_dep = cfg.get("algo2nw_deploy_pct")
            if nw_dep is None:
                nw_dep = cfg.get("algo2_deploy_pct")
            out["deploy_pct_of_balance"] = float(nw_dep if nw_dep is not None else base_dep)
        else:
            a2_cap = cfg.get("algo2_capital_inr")
            out["capital_inr"] = float(a2_cap if a2_cap is not None else base_cap)
            a2_dep = cfg.get("algo2_deploy_pct")
            out["deploy_pct_of_balance"] = float(a2_dep if a2_dep is not None else base_dep)
        return out

    def restore_from_store(self, runtime: dict[str, Any] | None = None) -> bool:
        """Re-attach newest OPEN algo2 setup after process restart."""
        opens = [t for t in trade_store.open_trades() if t.get("algo") == self.algo_id]
        if not opens:
            return False
        saved = (runtime or {}).get(self.algo_id) or {}
        # Prefer calendar-rolled fallen PE if present
        rolled = next(
            (
                t
                for t in opens
                if t.get("rolled_from")
                or "calendar roll" in str(t.get("note") or "").lower()
            ),
            None,
        )
        if rolled:
            best_sid = str(rolled.get("setup_id") or rolled["id"])
            legs = [t for t in opens if str(t.get("setup_id") or t["id"]) == best_sid]
            if rolled not in legs:
                legs = [rolled]
        else:
            by_setup: dict[str, list[dict[str, Any]]] = {}
            for t in opens:
                sid = str(t.get("setup_id") or t["id"])
                by_setup.setdefault(sid, []).append(t)
            best_sid = max(
                by_setup.keys(),
                key=lambda s: max(str(x.get("opened_at") or "") for x in by_setup[s]),
            )
            legs = by_setup[best_sid]
        ce = next((t for t in legs if t.get("side") == "CE" or str(t.get("symbol", "")).endswith("CE")), None)
        pe = next((t for t in legs if t.get("side") == "PE" or str(t.get("symbol", "")).endswith("PE")), None)
        self.state.setup_id = best_sid
        if ce:
            self.state.ce_trade_id = ce["id"]
            self.state.ce_symbol = ce.get("symbol")
            self.state.ce_entry = float(ce.get("entry_price") or 0)
        if pe:
            self.state.pe_trade_id = pe["id"]
            self.state.pe_symbol = pe.get("symbol")
            self.state.pe_entry = float(pe.get("entry_price") or 0)
        roles = {t.get("leg_role") for t in legs}
        if "fallen" in roles and "breakout" not in roles and len(legs) == 1:
            self.state.phase = "FALLEN_HOLD"
            fallen = legs[0]
            self.state.fallen_side = fallen.get("side") or (
                "CE" if str(fallen.get("symbol")).endswith("CE") else "PE"
            )
            self.state.fallen_trade_id = fallen["id"]
            self.state.breakout_side = "PE" if self.state.fallen_side == "CE" else "CE"
            self.state.fallen_low = float(
                saved.get("fallen_low") or fallen.get("live_price") or fallen.get("entry_price") or 0
            )
        elif "breakout" in roles:
            # Single breakout leg (breakout-only) or breakout+fallen (legacy dual)
            self.state.phase = "BREAKOUT_WATCH"
            for t in legs:
                if t.get("leg_role") == "breakout":
                    self.state.breakout_side = t.get("side")
                    self.state.breakout_trade_id = t["id"]
                    self.state.breakout_peak = float(
                        saved.get("breakout_peak") or t.get("live_price") or t.get("entry_price") or 0
                    )
                if t.get("leg_role") == "fallen":
                    self.state.fallen_side = t.get("side")
                    self.state.fallen_trade_id = t["id"]
            if "fallen" not in roles:
                self.state.fallen_side = None
                self.state.fallen_trade_id = None
                # Keep opposite symbol from runtime for quotes/pipe after restart
                if not self.state.ce_symbol and saved.get("ce_symbol"):
                    self.state.ce_symbol = saved.get("ce_symbol")
                if not self.state.pe_symbol and saved.get("pe_symbol"):
                    self.state.pe_symbol = saved.get("pe_symbol")
                if self.state.ce_entry is None and saved.get("ce_entry") is not None:
                    try:
                        self.state.ce_entry = float(saved["ce_entry"])
                    except (TypeError, ValueError):
                        pass
                if self.state.pe_entry is None and saved.get("pe_entry") is not None:
                    try:
                        self.state.pe_entry = float(saved["pe_entry"])
                    except (TypeError, ValueError):
                        pass
        else:
            self.state.phase = "DUAL_OPEN"
        # Restore TL / band chart state — never closes or replaces open trade ids
        if saved.get("up_tl"):
            self.state.up_tl = saved.get("up_tl")
        if saved.get("down_tl"):
            self.state.down_tl = saved.get("down_tl")
        if saved.get("w_dn_tl"):
            self.state.w_dn_tl = saved.get("w_dn_tl")
        if saved.get("f_up_tl"):
            self.state.f_up_tl = saved.get("f_up_tl")
        self.state.tl_armed = bool(
            saved.get("tl_armed")
            or self.state.up_tl
            or self.state.down_tl
            or self.state.w_dn_tl
            or self.state.f_up_tl
        )
        try:
            self.state.tl_tick = int(saved.get("tl_tick") or 0)
        except (TypeError, ValueError):
            self.state.tl_tick = 0
        if saved.get("merge_band_high") is not None:
            try:
                self.state.merge_band_high = float(saved["merge_band_high"])
                self.state.merge_band_low = float(saved["merge_band_low"])
            except (TypeError, ValueError, KeyError):
                pass
        if saved.get("conv_series_i") is not None:
            try:
                self.state.conv_series_i = int(saved["conv_series_i"])
            except (TypeError, ValueError):
                pass
        if (
            self.state.tl_armed
            and self.state.breakout_trade_id
            and self.state.phase in ("BREAKOUT_WATCH", "TL_MAP", "DUAL_OPEN")
        ):
            # Winner exits below up-TL — always manage as TL_HOLD when breakout open
            self.state.phase = "TL_HOLD"
        try:
            self.state.tl_arm_tick = int(saved.get("tl_arm_tick") or self.state.tl_tick or 0)
        except (TypeError, ValueError):
            self.state.tl_arm_tick = int(self.state.tl_tick or 0)
        # Clamp arm tick into current index space so grace can't stick forever
        if self.state.tl_arm_tick > self.state.tl_tick:
            self.state.tl_arm_tick = max(
                0, int(self.state.tl_tick) - int(5)
            )
        if self.state.ce_entry and self.state.pe_entry:
            self.state.mid_at_entry = (self.state.ce_entry + self.state.pe_entry) / 2
        elif saved.get("mid_at_entry"):
            self.state.mid_at_entry = float(saved["mid_at_entry"])
        # Rebuild chart pair so day-series sync works without scanner lock
        if self.state.ce_symbol and self.state.pe_symbol:
            try:
                self._active_pair_for_chart({})
            except Exception:
                pass
        self.state.fallen_rolled = bool(saved.get("fallen_rolled"))
        self.state.pair_fallen_hold = bool(saved.get("pair_fallen_hold"))
        self.state.pair_retrace_armed = bool(saved.get("pair_retrace_armed"))
        self.state.pair_retrace_side = saved.get("pair_retrace_side")
        try:
            self.state.pair_retrace_peak_mult = float(saved.get("pair_retrace_peak_mult") or 0)
        except (TypeError, ValueError):
            self.state.pair_retrace_peak_mult = 0.0
        try:
            self.state.pair_retrace_peak_ltp = float(saved.get("pair_retrace_peak_ltp") or 0)
        except (TypeError, ValueError):
            self.state.pair_retrace_peak_ltp = 0.0
        if saved.get("pair_retrace_fallen_low") is not None:
            try:
                self.state.pair_retrace_fallen_low = float(saved["pair_retrace_fallen_low"])
            except (TypeError, ValueError):
                pass
        self.state.sold_winner_side = saved.get("sold_winner_side")
        if saved.get("sold_winner_exit") is not None:
            try:
                self.state.sold_winner_exit = float(saved["sold_winner_exit"])
            except (TypeError, ValueError):
                pass
        if saved.get("sold_winner_peak") is not None:
            try:
                self.state.sold_winner_peak = float(saved["sold_winner_peak"])
            except (TypeError, ValueError):
                pass
        self.state.fallen_roll_from = saved.get("fallen_roll_from")
        self.state.fallen_roll_to = saved.get("fallen_roll_to")
        if saved.get("fallen_strike") is not None:
            try:
                self.state.fallen_strike = float(saved["fallen_strike"])
            except (TypeError, ValueError):
                pass
        if saved.get("fallen_expiry"):
            self.state.fallen_expiry = str(saved["fallen_expiry"])
        # Infer roll from trade metadata if runtime missed it
        if not self.state.fallen_rolled:
            for t in legs:
                if t.get("rolled_from") or "calendar roll" in str(t.get("note") or "").lower():
                    self.state.fallen_rolled = True
                    self.state.fallen_roll_from = t.get("rolled_from") or self.state.fallen_roll_from
                    self.state.fallen_roll_to = t.get("symbol")
                    if t.get("strike") is not None:
                        self.state.fallen_strike = float(t["strike"])
                    if t.get("expiry"):
                        self.state.fallen_expiry = str(t["expiry"])
                    break
        if self.state.fallen_strike is None:
            for t in legs:
                if t.get("leg_role") == "fallen" or self.state.phase == "FALLEN_HOLD":
                    if t.get("strike") is not None:
                        self.state.fallen_strike = float(t["strike"])
                    if t.get("expiry"):
                        self.state.fallen_expiry = str(t["expiry"])
                    break
        if saved.get("day_conv_date"):
            self.state.day_conv_date = str(saved["day_conv_date"])
        if saved.get("day_conv_mid") is not None:
            try:
                self.state.day_conv_mid = float(saved["day_conv_mid"])
            except (TypeError, ValueError):
                pass
        # Seed day conv from this trade's mid so a later higher re-merge stays blocked
        today = datetime.now(IST).date().isoformat()
        if self.state.day_conv_date != today:
            self.state.day_conv_date = today
            self.state.day_conv_mid = None
        if self.state.day_conv_mid is None:
            seed = self.state.mid_at_entry or self.state.last_convergence_mid or saved.get(
                "mid_at_entry"
            )
            if seed is not None:
                try:
                    self.state.day_conv_mid = float(seed)
                except (TypeError, ValueError):
                    pass
        self.state.last_message = f"restored algo2 {self.state.phase} setup {best_sid}"
        return True

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.state.phase,
            "setup_id": self.state.setup_id,
            "ce_trade_id": self.state.ce_trade_id,
            "pe_trade_id": self.state.pe_trade_id,
            "ce_symbol": self.state.ce_symbol,
            "pe_symbol": self.state.pe_symbol,
            "ce_entry": self.state.ce_entry,
            "pe_entry": self.state.pe_entry,
            "breakout_side": self.state.breakout_side,
            "fallen_side": self.state.fallen_side,
            "breakout_trade_id": self.state.breakout_trade_id,
            "fallen_trade_id": self.state.fallen_trade_id,
            "breakout_peak": self.state.breakout_peak,
            "fallen_low": self.state.fallen_low,
            "pair_fallen_hold": self.state.pair_fallen_hold,
            "pair_retrace_armed": self.state.pair_retrace_armed,
            "pair_retrace_side": self.state.pair_retrace_side,
            "pair_retrace_peak_mult": self.state.pair_retrace_peak_mult,
            "pair_retrace_peak_ltp": self.state.pair_retrace_peak_ltp,
            "pair_retrace_fallen_low": self.state.pair_retrace_fallen_low,
            "sold_winner_side": self.state.sold_winner_side,
            "sold_winner_exit": self.state.sold_winner_exit,
            "sold_winner_peak": self.state.sold_winner_peak,
            "mid_at_entry": self.state.mid_at_entry,
            "day_conv_date": self.state.day_conv_date,
            "day_conv_mid": self.state.day_conv_mid,
            "merge_band_high": self.state.merge_band_high,
            "merge_band_low": self.state.merge_band_low,
            "conv_series_i": self.state.conv_series_i,
            "up_tl": self.state.up_tl,
            "down_tl": self.state.down_tl,
            "w_dn_tl": self.state.w_dn_tl,
            "f_up_tl": self.state.f_up_tl,
            "tl_armed": self.state.tl_armed,
            "tl_tick": self.state.tl_tick,
            "tl_arm_tick": self.state.tl_arm_tick,
            "fallen_rolled": self.state.fallen_rolled,
            "fallen_roll_from": self.state.fallen_roll_from,
            "fallen_roll_to": self.state.fallen_roll_to,
            "fallen_strike": self.state.fallen_strike,
            "fallen_expiry": self.state.fallen_expiry,
            "rhythm": self.state.rhythm,
        }

    def _day_aligned_series(
        self, ctx: dict[str, Any], ce_sym: str, pe_sym: str
    ) -> list[dict[str, Any]]:
        """09:15→now minute CE/PE from market hub (seeded on late dig / watch)."""
        market = ctx.get("market")
        if market is None or not ce_sym or not pe_sym:
            return []
        try:
            ce_rows = list(market.series_payload(ce_sym) or [])
            pe_rows = list(market.series_payload(pe_sym) or [])
        except Exception:
            return []
        if not ce_rows or not pe_rows:
            return []
        pe_by_t = {int(p["time"]): float(p["value"]) for p in pe_rows if p.get("time") is not None}
        out: list[dict[str, Any]] = []
        for p in ce_rows:
            t = p.get("time")
            if t is None:
                continue
            ti = int(t)
            if ti not in pe_by_t:
                # tolerate ±60s skew on minute bars
                alt = pe_by_t.get(ti - 60) or pe_by_t.get(ti + 60)
                if alt is None:
                    continue
                pe_v = alt
            else:
                pe_v = pe_by_t[ti]
            out.append(
                {
                    "time": ti,
                    "ce": round(float(p["value"]), 2),
                    "pe": round(float(pe_v), 2),
                }
            )
        return out

    def _active_pair_for_chart(self, ctx: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Scanner pair, or rebuild from open dual symbols/tokens after restart."""
        ctx = ctx or {}
        pair = self._normalize_pair(self.state.pair or ctx.get("pair"))
        if pair and (self._pair_leg(pair, "CE") or self._pair_leg(pair, "PE")):
            self.state.pair = pair
            return pair
        ce_sym = self.state.ce_symbol
        pe_sym = self.state.pe_symbol
        if not ce_sym or not pe_sym:
            return None
        ce_tok = pe_tok = 0
        try:
            for t in trade_store.list_trades():
                if t.get("status") != "OPEN" or t.get("algo") != self.algo_id:
                    continue
                if t.get("id") not in (self.state.ce_trade_id, self.state.pe_trade_id) and (
                    self.state.setup_id and t.get("setup_id") != self.state.setup_id
                ):
                    continue
                tok = int(t.get("instrument_token") or 0)
                if t.get("side") == "CE" and t.get("symbol") == ce_sym:
                    ce_tok = tok or ce_tok
                if t.get("side") == "PE" and t.get("symbol") == pe_sym:
                    pe_tok = tok or pe_tok
        except Exception:
            pass
        rebuilt = {
            "ce": {"tradingsymbol": ce_sym, "instrument_token": ce_tok},
            "pe": {"tradingsymbol": pe_sym, "instrument_token": pe_tok},
        }
        self.state.pair = rebuilt
        return rebuilt

    def _seed_pair_day_history(self, cfg: dict[str, Any], ctx: dict[str, Any]) -> None:
        """Pull 09:15→now minute bars for the active CE/PE into market hub."""
        market = ctx.get("market")
        pair = self._active_pair_for_chart(ctx)
        if market is None or not pair:
            return
        legs = []
        for key in ("ce", "pe"):
            leg = self._pair_leg(pair, key.upper())
            sym = leg.get("tradingsymbol") or (
                self.state.ce_symbol if key == "ce" else self.state.pe_symbol
            )
            tok = int(leg.get("instrument_token") or 0)
            if sym and tok > 0:
                legs.append({"tradingsymbol": sym, "instrument_token": tok})
        if not legs:
            return
        try:
            from foxtrade.late_start import seed_series_from_history

            seed_series_from_history(market, legs, cfg)
        except Exception:
            pass

    def _sync_day_live_series(self, cfg: dict[str, Any], ctx: dict[str, Any]) -> None:
        """Ensure live_series spans session open so TLs can paint from day start."""
        pair = self._active_pair_for_chart(ctx)
        if not pair:
            return
        ce_sym = self._pair_leg(pair, "CE").get("tradingsymbol") or self.state.ce_symbol
        pe_sym = self._pair_leg(pair, "PE").get("tradingsymbol") or self.state.pe_symbol
        if not ce_sym or not pe_sym:
            return
        day = self._day_aligned_series(ctx, str(ce_sym), str(pe_sym))
        if len(day) < 2:
            # Late / missing seed — dig pair minutes from 09:15 then retry
            self._seed_pair_day_history(cfg, ctx)
            day = self._day_aligned_series(ctx, str(ce_sym), str(pe_sym))
        if len(day) < 2:
            return
        existing = list(self.state.live_series or [])
        day_last_t = int(day[-1]["time"])
        # Keep denser live ticks after the last minute bar
        tail = [p for p in existing if int(p.get("time") or 0) > day_last_t]
        merged = day + tail
        cap = int(cfg.get("convergence_chart_points", 2000))
        self.state.live_series = merged[-cap:]
        if self.state.live_series:
            self.state.live_series_ts = float(self.state.live_series[-1]["time"])

    def _entry_unix_ts(self) -> float | None:
        """Best-effort entry/merge unix time from open dual legs."""
        try:
            rows = [
                t
                for t in trade_store.list_trades()
                if t.get("algo") == self.algo_id
                and t.get("status") == "OPEN"
                and (
                    t.get("id") in (self.state.ce_trade_id, self.state.pe_trade_id)
                    or t.get("setup_id") == self.state.setup_id
                )
            ]
            best: float | None = None
            for t in rows:
                raw = t.get("opened_at")
                if not raw:
                    continue
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                u = float(ts.timestamp())
                if best is None or u < best:
                    best = u
            return best
        except Exception:
            return None

    def _conv_bar_ok(
        self, p: dict[str, Any], cfg: dict[str, Any], *, mid_ref: float | None
    ) -> bool:
        """True when a series bar looks like the dual merge (both legs near band)."""
        try:
            ce = float(p["ce"])
            pe = float(p["pe"])
        except (KeyError, TypeError, ValueError):
            return False
        lo = float(cfg.get("premium_low", 70))
        hi = float(cfg.get("premium_high", 77))
        gap = abs(ce - pe)
        mid = (ce + pe) / 2.0
        # Soft band pad — merge can sit just outside exact 70–77 prints
        pad = 8.0
        near_band = (lo - pad) <= mid <= (hi + pad)
        both_near = (lo - pad) <= ce <= (hi + pad) and (lo - pad) <= pe <= (hi + pad)
        tight = gap <= float(cfg.get("convergence_tol", 8) or 8) * 2.5
        if mid_ref is not None and abs(mid - float(mid_ref)) > 12:
            return False
        return bool(near_band and both_near and tight)

    def _locate_convergence_index(
        self, series: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> int:
        """live_series index of the dual convergence — channels/TLs start here."""
        n = len(series)
        if n <= 0:
            return 0
        mid_ref = (
            self.state.mid_at_entry
            or self.state.last_convergence_mid
            or self.state.day_conv_mid
        )
        entry_ts = self._entry_unix_ts()
        # Refuse to lock on a series that starts after entry (day seed missing)
        if entry_ts is not None:
            try:
                t0 = float(series[0].get("time") or 0)
            except (TypeError, ValueError):
                t0 = 0.0
            if t0 > float(entry_ts) + 90:
                # Keep prior good lock if still in range; else wait for day bars
                if (
                    self.state.conv_series_i is not None
                    and 0 <= int(self.state.conv_series_i) < n
                    and self._conv_bar_ok(
                        series[int(self.state.conv_series_i)], cfg, mid_ref=mid_ref
                    )
                ):
                    return int(self.state.conv_series_i)
                return max(0, n - 1)

        # Keep a previously locked merge only if it still looks like convergence
        # and sits at/near entry (not a later re-merge)
        if (
            self.state.conv_series_i is not None
            and 0 <= int(self.state.conv_series_i) < n
            and self._conv_bar_ok(series[int(self.state.conv_series_i)], cfg, mid_ref=mid_ref)
        ):
            if entry_ts is None:
                return int(self.state.conv_series_i)
            try:
                ct = float(series[int(self.state.conv_series_i)].get("time") or 0)
                if abs(ct - float(entry_ts)) <= 10 * 60:
                    return int(self.state.conv_series_i)
            except (TypeError, ValueError):
                return int(self.state.conv_series_i)

        lo = float(cfg.get("premium_low", 70))
        hi = float(cfg.get("premium_high", 77))
        # Nearest bar to entry time as hard fallback
        entry_i = max(0, n - 1)
        if entry_ts is not None:
            best_dt = 1e18
            for i, p in enumerate(series):
                try:
                    dt = abs(float(p.get("time") or 0) - float(entry_ts))
                except (TypeError, ValueError):
                    continue
                if dt < best_dt:
                    best_dt = dt
                    entry_i = i

        search_from = 0
        search_to = n
        if entry_ts is not None:
            for i, p in enumerate(series):
                try:
                    if float(p.get("time") or 0) >= float(entry_ts) - 45 * 60:
                        search_from = i
                        break
                except (TypeError, ValueError):
                    continue
            # Do not take a later re-merge after entry as the origin
            for i in range(n - 1, -1, -1):
                try:
                    if float(series[i].get("time") or 0) <= float(entry_ts) + 90:
                        search_to = i + 1
                        break
                except (TypeError, ValueError):
                    continue

        best_i: int | None = None
        best_score = -1e18
        for i in range(search_from, max(search_from + 1, search_to)):
            p = series[i]
            try:
                ce = float(p["ce"])
                pe = float(p["pe"])
            except (KeyError, TypeError, ValueError):
                continue
            mid = (ce + pe) / 2.0
            gap = abs(ce - pe)
            both_in = lo <= ce <= hi and lo <= pe <= hi
            mid_in = lo <= mid <= hi
            if not (both_in or mid_in or self._conv_bar_ok(p, cfg, mid_ref=mid_ref)):
                continue
            score = (2000.0 if both_in else 800.0 if mid_in else 200.0) - gap * 25.0
            if mid_ref is not None:
                score -= abs(mid - float(mid_ref)) * 3.0
            if entry_ts is not None:
                try:
                    score -= abs(float(p.get("time") or 0) - float(entry_ts)) / 30.0
                except (TypeError, ValueError):
                    pass
            if score > best_score:
                best_score = score
                best_i = i

        if best_i is None:
            best_i = int(entry_i)
        self.state.conv_series_i = int(best_i)
        return int(best_i)

    @staticmethod
    def _shift_tl_from_slice(
        tl: dict[str, Any] | None, start_i: int
    ) -> dict[str, Any] | None:
        """Map a channel TL fitted on series[start_i:] back to full-series indices."""
        if not tl:
            return None
        out = dict(tl)
        slope = float(out.get("slope") or 0.0)
        # Fit used local indices; intercept is value at local i=0 (= convergence)
        a_local = out.get("intercept")
        if a_local is None and out.get("y0") is not None and out.get("i0") is not None:
            a_local = float(out["y0"]) - slope * float(out["i0"])
        if a_local is None:
            a_local = float(out.get("y0") or out.get("anchor_y") or 0.0)
        a_local = float(a_local)
        for k in ("i0", "i1", "break_i"):
            if out.get(k) is not None:
                out[k] = int(out[k]) + int(start_i)
        # Always paint from the convergence bar
        out["anchor_fi"] = int(start_i)
        out["anchor_y"] = round(a_local, 2)
        out["intercept"] = round(a_local - slope * float(start_i), 3)
        out["parallel"] = True
        out["index_space"] = "series"
        out["from_convergence"] = True
        return out

    def _fit_channels_from_convergence(
        self, cfg: dict[str, Any], series: list[dict[str, Any]]
    ) -> bool:
        """Fit CE+PE parallel channels using only bars from convergence → now."""
        if len(series) < 3:
            return False
        start_i = self._locate_convergence_index(series, cfg)
        # Need a few post-merge bars to form a channel
        if len(series) - start_i < 3:
            start_i = max(0, len(series) - 3)
            self.state.conv_series_i = start_i
        ce_full = [float(p["ce"]) for p in series]
        pe_full = [float(p["pe"]) for p in series]
        ce_seg = ce_full[start_i:]
        pe_seg = pe_full[start_i:]
        ce_bi = max(1, len(ce_seg) - 1)
        pe_bi = max(1, len(pe_seg) - 1)
        look = max(
            int(cfg.get("algo2_breakout_tl_lookback", 12) or 12),
            int(cfg.get("algo2_channel_lookback_bars", 60) or 60),
            ce_bi,
            pe_bi,
        )
        ce_ch = fit_premium_channel(
            ce_seg, break_i=ce_bi, lookback=look, from_start=True
        )
        pe_ch = fit_premium_channel(
            pe_seg, break_i=pe_bi, lookback=look, from_start=True
        )
        self.state.ce_hi_tl = self._shift_tl_from_slice(ce_ch.get("down"), start_i)
        self.state.ce_lo_tl = self._shift_tl_from_slice(ce_ch.get("up"), start_i)
        self.state.pe_hi_tl = self._shift_tl_from_slice(pe_ch.get("down"), start_i)
        self.state.pe_lo_tl = self._shift_tl_from_slice(pe_ch.get("up"), start_i)
        end_i = len(series) - 1
        self.state.tl_tick = end_i
        return bool(
            self.state.ce_hi_tl
            or self.state.ce_lo_tl
            or self.state.pe_hi_tl
            or self.state.pe_lo_tl
        )

    def _arm_day_trendlines(
        self,
        cfg: dict[str, Any],
        ctx: dict[str, Any],
        *,
        side: str,
    ) -> None:
        """Fit CE+PE channels from the convergence point forward."""
        self._sync_day_live_series(cfg, ctx)
        series = list(self.state.live_series or [])
        index_space = "series"
        if len(series) < 3:
            # Fall back to tick hist (pre-series) — rare
            w_hist = list(self.state.ce_hist if side == "CE" else self.state.pe_hist)
            if len(w_hist) < 2:
                return
            bi = max(1, len(w_hist) - 1)
            look = int(cfg.get("algo2_breakout_tl_lookback", 12))
            ch_look = int(cfg.get("algo2_channel_lookback_bars", 60) or 60)
            ce_hist = list(self.state.ce_hist)
            pe_hist = list(self.state.pe_hist)
            ce_bi = max(1, len(ce_hist) - 1)
            pe_bi = max(1, len(pe_hist) - 1)
            ce_ch = fit_premium_channel(
                ce_hist, break_i=ce_bi, lookback=max(look, ch_look), from_start=True
            )
            pe_ch = fit_premium_channel(
                pe_hist, break_i=pe_bi, lookback=max(look, ch_look), from_start=True
            )
            self.state.ce_hi_tl = bind_tl_to_frame(ce_ch.get("down"), frame_i=ce_bi, hist_i=ce_bi)
            self.state.ce_lo_tl = bind_tl_to_frame(ce_ch.get("up"), frame_i=ce_bi, hist_i=ce_bi)
            self.state.pe_hi_tl = bind_tl_to_frame(pe_ch.get("down"), frame_i=pe_bi, hist_i=pe_bi)
            self.state.pe_lo_tl = bind_tl_to_frame(pe_ch.get("up"), frame_i=pe_bi, hist_i=pe_bi)
            self.state.tl_tick = bi
            index_space = "hist"
        else:
            if not self._fit_channels_from_convergence(cfg, series):
                return
        # Winner/fallen aliases for exits
        if side == "CE":
            self.state.up_tl = self.state.ce_lo_tl
            self.state.w_dn_tl = self.state.ce_hi_tl
            self.state.f_up_tl = self.state.pe_lo_tl
            self.state.down_tl = self.state.pe_hi_tl
        else:
            self.state.up_tl = self.state.pe_lo_tl
            self.state.w_dn_tl = self.state.pe_hi_tl
            self.state.f_up_tl = self.state.ce_lo_tl
            self.state.down_tl = self.state.ce_hi_tl
        for tl in (
            self.state.up_tl,
            self.state.w_dn_tl,
            self.state.f_up_tl,
            self.state.down_tl,
            self.state.ce_hi_tl,
            self.state.ce_lo_tl,
            self.state.pe_hi_tl,
            self.state.pe_lo_tl,
        ):
            if tl:
                tl["index_space"] = index_space
                tl["parallel"] = True
        self.state.tl_armed = True
        self.state.tl_arm_tick = int(
            self.state.conv_series_i
            if self.state.conv_series_i is not None
            else self.state.tl_tick
            or 0
        )
        if self.state.conv_zone is None:
            self.state.conv_zone = {}
        self._attach_chart_trendlines()

    def _book_winner_below_up_tl(
        self,
        cfg: dict[str, Any],
        *,
        w_px: float,
        tick: int,
        lot_size: int,
        ctx: dict[str, Any],
        release: Callable[[str], None],
    ) -> bool:
        """Exit breakout leg when premium falls below rising up-TL (chart support break)."""
        if not self.state.breakout_trade_id or not self.state.up_tl:
            return False
        grace = int(cfg.get("algo2_breakout_tl_grace_bars", 5))
        arm = int(self.state.tl_arm_tick or 0)
        # Restored TLs may carry an old hist-space arm tick — don't block forever
        if arm > int(tick):
            arm = max(0, int(tick) - grace)
            self.state.tl_arm_tick = arm
        if int(tick) - arm < grace:
            return False
        buf = float(cfg.get("algo2_breakout_tl_buffer", 1.5))
        if not winner_broke_up_tl(
            float(w_px),
            self.state.up_tl,
            tick,
            buffer=buf,
            armed=self.state.tl_armed,
        ):
            return False
        lvl = trendline_value(self.state.up_tl, tick)
        reason = (
            f"winner broke up-TL {lvl:.1f}"
            if lvl is not None
            else "winner broke up-TL"
        )
        b_side = self.state.breakout_side
        broker.close_long(self.state.breakout_trade_id, w_px, reason, lot_size)
        trade_store.update_trade(
            self.state.breakout_trade_id, {"leg_role": "breakout_closed"}
        )
        self.state.breakout_trade_id = None
        if b_side == "CE":
            self.state.ce_trade_id = None
        else:
            self.state.pe_trade_id = None
        self.state.up_tl = None  # stop plotting winner TL after exit
        self._attach_chart_trendlines()
        self._notify(ctx, f"BOOKED {b_side} @{w_px:.1f} · {reason}")
        if self.state.fallen_trade_id:
            self.state.phase = "FALLEN_HOLD"
            self.state.last_message = f"booked {b_side}: {reason} · hold fallen"
        else:
            self._clear(release)
            self.state.last_message = f"booked {b_side}: {reason}"
        return True

    def _correct_day_trendlines(
        self, cfg: dict[str, Any], ctx: dict[str, Any]
    ) -> None:
        """Re-fit CE+PE channels from the locked convergence index as series grows."""
        if not self.state.tl_armed:
            return
        side = self.state.breakout_side if self.state.breakout_side in ("CE", "PE") else "CE"
        self._sync_day_live_series(cfg, ctx)
        series = list(self.state.live_series or [])
        if len(series) < 3:
            return
        if not self._fit_channels_from_convergence(cfg, series):
            self._arm_day_trendlines(cfg, ctx, side=side)
            return
        if side == "CE":
            self.state.up_tl, self.state.w_dn_tl = self.state.ce_lo_tl, self.state.ce_hi_tl
            self.state.f_up_tl, self.state.down_tl = self.state.pe_lo_tl, self.state.pe_hi_tl
        else:
            self.state.up_tl, self.state.w_dn_tl = self.state.pe_lo_tl, self.state.pe_hi_tl
            self.state.f_up_tl, self.state.down_tl = self.state.ce_lo_tl, self.state.ce_hi_tl
        self._attach_chart_trendlines()

    def _channel_replot_due(self, cfg: dict[str, Any], *, now: float | None = None) -> bool:
        """True when CE/PE channels should be re-fit (default every 30s)."""
        every = float(cfg.get("algo2_channel_replot_seconds", 30) or 30)
        if every <= 0:
            return True
        t = float(now if now is not None else time.time())
        return (t - float(self.state.tl_replot_ts or 0.0)) >= every

    def _ensure_dual_channels(
        self,
        cfg: dict[str, Any],
        ctx: dict[str, Any] | None,
        *,
        force: bool = False,
    ) -> None:
        """Fit/refresh both CE+PE parallel channels while dual is open (chart)."""
        if not cfg.get("algo2_breakout_tl_enabled", True):
            return
        ctx = ctx or {}
        side = self.state.breakout_side if self.state.breakout_side in ("CE", "PE") else "CE"
        now = time.time()
        try:
            if not self.state.tl_armed:
                self._arm_day_trendlines(cfg, ctx, side=side)
                self.state.tl_replot_ts = now
                self._fit_index_channel_and_sr(cfg, ctx)
                return
            if force or self._channel_replot_due(cfg, now=now):
                self._correct_day_trendlines(cfg, ctx)
                self.state.tl_replot_ts = now
                self._fit_index_channel_and_sr(cfg, ctx)
        except Exception:
            pass

    def _today_index_candles(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize today's Nifty 5m OHLC for channel / S/R fitting."""
        market = ctx.get("market")
        raw = list(getattr(market, "nifty_candles_5m", None) or [])
        today = datetime.now(IST).date()
        out: list[dict[str, Any]] = []
        for row in raw:
            dt = row.get("date")
            if dt is None:
                continue
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            else:
                dt = dt.replace(tzinfo=IST)
            if dt.date() != today:
                continue
            # Cash session only
            hm = dt.hour * 60 + dt.minute
            if hm < 9 * 60 + 15 or hm > 15 * 60 + 30:
                continue
            try:
                out.append(
                    {
                        "time": int(dt.timestamp()),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
                )
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _index_swing_start(
        self,
        lows: list[float],
        highs: list[float],
        closes: list[float],
        cfg: dict,
    ) -> int:
        """
        Anchor Index channel at the trough/peak that started the *current* swing
        (hand-drawn ascending rails), not the session open.
        """
        n = len(lows)
        if n < 4:
            return 0
        # ~4h of 5m bars so the impulse trough is included
        win = max(16, int(cfg.get("algo2_index_channel_bars", 48) or 48))
        lo0 = max(0, n - win)
        last = n - 1
        # Rising: bar whose low → now gives the largest climb
        best_up = max(
            range(lo0, max(lo0 + 1, last - 2)),
            key=lambda i: float(closes[last]) - float(lows[i]),
        )
        rise = float(closes[last]) - float(lows[best_up])
        # Falling: bar whose high → now gives the largest drop
        best_dn = max(
            range(lo0, max(lo0 + 1, last - 2)),
            key=lambda i: float(highs[i]) - float(closes[last]),
        )
        drop = float(highs[best_dn]) - float(closes[last])
        if rise >= max(40.0, drop + 10.0):
            return int(best_up)
        if drop >= 40.0:
            return int(best_dn)
        return int(best_up)

    def _shift_index_tl(
        self, tl: dict[str, Any] | None, start_i: int
    ) -> dict[str, Any] | None:
        """Map a TL fitted on candles[start_i:] back into full-day index space."""
        if not tl:
            return None
        out = dict(tl)
        for k in ("i0", "i1", "break_i", "anchor_fi"):
            if out.get(k) is not None:
                out[k] = int(out[k]) + int(start_i)
        # intercept form y = intercept + slope*i is in slice space → rewrite via anchor
        if out.get("anchor_fi") is not None and out.get("anchor_y") is not None:
            out["intercept"] = float(out["anchor_y"]) - float(out["slope"]) * int(
                out["anchor_fi"]
            )
        return out

    def _session_eod_ts(self, cfg: dict[str, Any] | None = None) -> int:
        """Unix ts for today's cash close (default 15:30 IST)."""
        end_hm = str((cfg or {}).get("cash_session_end") or "15:30").strip() or "15:30"
        try:
            hh, mm = [int(x) for x in end_hm.split(":")[:2]]
        except ValueError:
            hh, mm = 15, 30
        now = datetime.now(IST)
        return int(
            now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
        )

    def _extend_tl_points_to_eod(
        self,
        pts: list[dict[str, Any]],
        tl: dict[str, Any] | None,
        *,
        last_i: int,
        bar_seconds: int,
        cfg: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Keep rails painting through the full cash day: append synthetic points
        from last sample → 15:30 using the fitted slope.
        """
        if not pts or not tl or bar_seconds <= 0:
            return pts
        eod = self._session_eod_ts(cfg)
        last_t = int(pts[-1].get("time") or 0)
        if last_t <= 0 or last_t >= eod:
            return pts
        out = list(pts)
        n_bars = max(1, int(round((eod - last_t) / float(bar_seconds))))
        # Dense premium ticks (≤60s): one tip at 15:30 is enough (straight line).
        # Index 5m: step through remaining bars so the rail stays continuous on zoom.
        if int(bar_seconds) >= 60:
            t = last_t + int(bar_seconds)
            i = int(last_i) + 1
            max_steps = max(2, n_bars + 2)
            steps = 0
            while t < eod and steps < max_steps:
                lvl = trendline_value(tl, i)
                if lvl is not None:
                    out.append({"time": int(t), "value": round(float(lvl), 2)})
                t += int(bar_seconds)
                i += 1
                steps += 1
        lvl_eod = trendline_value(tl, int(last_i) + n_bars)
        if lvl_eod is not None:
            if out and int(out[-1]["time"]) == eod:
                out[-1] = {"time": eod, "value": round(float(lvl_eod), 2)}
            else:
                out.append({"time": eod, "value": round(float(lvl_eod), 2)})
        return out

    def _fit_rising_index_channel(
        self,
        seg_c: list[float],
        seg_h: list[float],
        seg_l: list[float],
        bi: int,
        tol: float,
    ) -> dict[str, Any]:
        """
        Ascending parallel channel: slope from swing lows, upper rail parallel
        touching the extreme swing high — matches hand-drawn yellow rails.
        """
        lo_piv = _local_extrema(seg_l, bi, find_low=True, min_sep=2)
        hi_piv = _local_extrema(seg_h, bi, find_low=False, min_sep=2)
        # Always include segment start low + last low as anchors
        anchors = [(0, float(seg_l[0]))]
        if lo_piv:
            anchors.extend(lo_piv)
        anchors.append((bi, float(seg_l[bi])))
        # Dedup by index
        by_i = {i: y for i, y in anchors}
        anchors = sorted(by_i.items())
        # Pick pair of lows that maximize rise slope while keeping later lows above the line
        best: tuple[float, int, float, int, float] | None = None  # score,i0,y0,i1,y1
        for a in range(len(anchors)):
            for b in range(a + 1, len(anchors)):
                i0, y0 = anchors[a]
                i1, y1 = anchors[b]
                if i1 - i0 < 3:
                    continue
                sl = (y1 - y0) / (i1 - i0)
                if sl < -0.2:
                    continue
                # Penalize cutting below later lows
                cut = 0.0
                for i, y in anchors:
                    if i < i0:
                        continue
                    line = y0 + sl * (i - i0)
                    if y < line - tol:
                        cut += line - y
                # Prefer steeper valid rises that span more of the move
                score = sl * (i1 - i0) * 2.0 + (i1 - i0) * 0.5 - cut * 3.0
                if best is None or score > best[0]:
                    best = (score, i0, y0, i1, y1)
        if best is None:
            # Fallback: start low → last low
            i0, y0, i1, y1 = 0, float(seg_l[0]), bi, float(seg_l[bi])
            sl = (y1 - y0) / max(1, i1 - i0)
        else:
            _, i0, y0, i1, y1 = best
            sl = (y1 - y0) / max(1, i1 - i0)
        a_lo = y0 - sl * i0
        # Width = max distance of highs above lower rail (contains the candles)
        hi_pts = hi_piv if hi_piv else [(i, float(seg_h[i])) for i in range(0, bi + 1)]
        width = max((float(h) - (a_lo + sl * i)) for i, h in hi_pts)
        # Also respect raw highs in segment
        width = max(
            width,
            max(float(seg_h[i]) - (a_lo + sl * i) for i in range(0, bi + 1)),
        )
        width = max(25.0, float(width))
        a_hi = a_lo + width
        return {
            "up": {
                "kind": "up",
                "i0": int(i0),
                "y0": round(a_lo + sl * i0, 2),
                "i1": int(i1),
                "y1": round(a_lo + sl * i1, 2),
                "slope": round(sl, 5),
                "intercept": round(a_lo, 3),
                "anchor_fi": int(i0),
                "anchor_y": round(a_lo + sl * i0, 2),
                "parallel": True,
            },
            "down": {
                "kind": "down",
                "i0": int(i0),
                "y0": round(a_hi + sl * i0, 2),
                "i1": int(i1),
                "y1": round(a_hi + sl * i1, 2),
                "slope": round(sl, 5),
                "intercept": round(a_hi, 3),
                "anchor_fi": int(i0),
                "anchor_y": round(a_hi + sl * i0, 2),
                "parallel": True,
            },
            "slope": sl,
            "width": width,
            "parallel": True,
        }

    def _fit_index_channel_and_sr(
        self, cfg: dict[str, Any], ctx: dict[str, Any]
    ) -> None:
        """Fit Index parallel channel + swing S/R; project dotted rails for Nifty chart."""
        if not cfg.get("algo2_index_channel_enabled", True):
            self.state.index_overlay = None
            return
        candles = self._today_index_candles(ctx)
        if len(candles) < 4:
            return
        self.state.ix_candles = candles
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        # Always plot Index channel from cash open 09:15 (first today candle)
        start_i = 0
        seg_c = closes
        seg_h = highs
        seg_l = lows
        bi = max(1, len(seg_c) - 1)
        look = max(bi, int(cfg.get("algo2_index_channel_bars", 48) or 48))
        tol = float(cfg.get("algo2_index_channel_tol", 5.0) or 5.0)
        seg_rise = float(seg_c[-1]) - float(seg_l[0])
        seg_drop = float(seg_h[0]) - float(seg_c[-1])
        # Rising day: ascending parallel from 09:15 swing lows (yellow-rail style)
        # Falling day: generic parallel; range: flatter premium fit — all from 09:15
        if seg_rise >= 40 and seg_rise >= seg_drop:
            ch = self._fit_rising_index_channel(seg_c, seg_h, seg_l, bi, tol)
        else:
            ch = fit_premium_channel(
                seg_c,
                break_i=bi,
                lookback=look,
                from_start=True,
                highs=seg_h,
                lows=seg_l,
                tol=tol,
            )
        self.state.ix_hi_tl = self._shift_index_tl(ch.get("down"), start_i)
        self.state.ix_lo_tl = self._shift_index_tl(ch.get("up"), start_i)

        def _proj(tl: dict[str, Any] | None) -> list[dict[str, Any]]:
            if not tl:
                return []
            pts: list[dict[str, Any]] = []
            # Paint from 09:15 → now, then extend to 15:30
            for i in range(start_i, len(candles)):
                lvl = trendline_value(tl, i)
                if lvl is None:
                    continue
                pts.append({"time": candles[i]["time"], "value": round(float(lvl), 2)})
            return self._extend_tl_points_to_eod(
                pts, tl, last_i=len(candles) - 1, bar_seconds=300, cfg=cfg
            )

        spot = (ctx.get("index") or {}).get("spot")
        try:
            spot_f = float(spot) if spot is not None else float(closes[-1])
        except (TypeError, ValueError):
            spot_f = float(closes[-1])

        # Horizontal S/R from recent swing highs / lows (R above spot, S below)
        end_i = len(closes) - 1
        hi_piv = _local_extrema(highs, end_i, find_low=False, min_sep=3)
        lo_piv = _local_extrema(lows, end_i, find_low=True, min_sep=3)
        n_sr = max(1, int(cfg.get("algo2_index_sr_count", 4) or 4))
        all_piv = [round(float(v), 1) for _, v in (hi_piv + lo_piv)]

        def _uniq(levels: list[float]) -> list[float]:
            out: list[float] = []
            for lv in sorted(set(levels)):
                if not out or abs(lv - out[-1]) >= 8:
                    out.append(lv)
            return out

        piv = _uniq(all_piv)
        resistance = [lv for lv in piv if lv >= spot_f][-n_sr:]
        support = [lv for lv in piv if lv < spot_f][:n_sr]
        # Prefer nearest levels
        resistance = sorted(resistance)[:n_sr]
        support = sorted(support, reverse=True)[:n_sr]
        t0 = int(candles[0]["time"])
        t1 = self._session_eod_ts(cfg)  # S/R span full cash day

        def _h_line(level: float) -> list[dict[str, Any]]:
            return [
                {"time": t0, "value": level},
                {"time": t1, "value": level},
            ]
        end_full = len(candles) - 1
        hi_now = trendline_value(self.state.ix_hi_tl, end_full)
        lo_now = trendline_value(self.state.ix_lo_tl, end_full)
        buf = float(cfg.get("algo2_index_channel_break_buf", 8) or 8)
        bias = "inside"
        if hi_now is not None and spot_f > float(hi_now) + buf:
            bias = "bull_break"  # favors CE hold
        elif lo_now is not None and spot_f < float(lo_now) - buf:
            bias = "bear_break"  # favors PE hold

        self.state.index_overlay = {
            "ch_hi_points": _proj(self.state.ix_hi_tl),
            "ch_lo_points": _proj(self.state.ix_lo_tl),
            "support": [{"level": lv, "points": _h_line(lv)} for lv in support],
            "resistance": [{"level": lv, "points": _h_line(lv)} for lv in resistance],
            "bias": bias,
            "spot": round(spot_f, 2),
            "ch_hi": round(float(hi_now), 2) if hi_now is not None else None,
            "ch_lo": round(float(lo_now), 2) if lo_now is not None else None,
            "swing_start_i": start_i,
        }

    def _index_channel_pos(self, cfg: dict[str, Any]) -> tuple[str, float | None]:
        """Return (bias, frac 0=lo…1=hi) for spot inside/through Index channel."""
        ov = self.state.index_overlay or {}
        bias = str(ov.get("bias") or "inside")
        spot = ov.get("spot")
        hi = ov.get("ch_hi")
        lo = ov.get("ch_lo")
        if spot is None or hi is None or lo is None:
            n = max(0, len(self.state.ix_candles) - 1)
            if spot is None:
                return bias, None
            hi = trendline_value(self.state.ix_hi_tl, n) if hi is None else hi
            lo = trendline_value(self.state.ix_lo_tl, n) if lo is None else lo
        try:
            s, h, l = float(spot), float(hi), float(lo)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return bias, None
        width = h - l
        if width <= 1e-6:
            return bias, 0.5
        frac = (s - l) / width
        return bias, frac

    def _index_supports_extension(self, cfg: dict[str, Any], win_side: str) -> bool:
        """
        True when Nifty channel still favors riding winner past 1.45× toward 2×/2.5×.
        bull_break→CE, bear_break→PE; or inside channel in the favorable half.
        """
        bias, frac = self._index_channel_pos(cfg)
        inside_frac = float(cfg.get("algo2_index_hold_inside_frac", 0.55) or 0.55)
        if win_side == "CE":
            if bias == "bull_break":
                return True
            if bias == "bear_break":
                return False
            return frac is not None and frac >= inside_frac
        if win_side == "PE":
            if bias == "bear_break":
                return True
            if bias == "bull_break":
                return False
            return frac is not None and frac <= (1.0 - inside_frac)
        return False

    def _index_break_favors(self, cfg: dict[str, Any], win_side: str) -> bool:
        """Alias — Index channel supports stretching the winner."""
        return self._index_supports_extension(cfg, win_side)

    def _index_break_against(self, cfg: dict[str, Any], win_side: str) -> bool:
        """True when Index channel no longer supports holding the winner."""
        return not self._index_supports_extension(cfg, win_side)

    def _stretch_ladder(self, cfg: dict[str, Any], pair_exit: float) -> list[float]:
        """Sorted stretch targets above pair-exit (default 2.0, 2.5)."""
        raw = cfg.get("algo2_index_hold_stretch_mults") or [2.0, 2.5]
        out: list[float] = []
        try:
            for x in raw:
                v = float(x)
                if v > pair_exit + 1e-6:
                    out.append(v)
        except (TypeError, ValueError):
            out = [2.0, 2.5]
        max_m = float(cfg.get("algo2_index_hold_max_mult", 2.5) or 2.5)
        if max_m > pair_exit and max_m not in out:
            out.append(max_m)
        return sorted(set(round(v, 4) for v in out if v <= max_m + 1e-9))

    def _sample_series(self, cfg: dict[str, Any], ctx: dict[str, Any]) -> None:
        """Throttled CE/PE premium samples for the dashboard convergence chart."""
        now = time.time()
        # Re-fit CE/PE + Index channels every ~30s so the trend stays clear
        if self._channel_replot_due(cfg, now=now):
            if self.state.tl_armed:
                self._sync_day_live_series(cfg, ctx)
                self._ensure_dual_channels(cfg, ctx, force=True)
            else:
                try:
                    before = self.state.index_overlay
                    self._fit_index_channel_and_sr(cfg, ctx)
                    # Only advance clock once Index rails painted (candles may lag)
                    if self.state.index_overlay or before is not None:
                        self.state.tl_replot_ts = now
                except Exception:
                    pass
        elif cfg.get("algo2_index_channel_enabled", True) and self.state.index_overlay is None:
            # Candles often arrive after first ticks — keep trying until first overlay
            try:
                self._fit_index_channel_and_sr(cfg, ctx)
                if self.state.index_overlay:
                    self.state.tl_replot_ts = now
            except Exception:
                pass
        # Faster samples → better tilted pipe fit on the live chart
        interval = float(cfg.get("convergence_chart_seconds", 5))
        if now - self.state.live_series_ts < interval:
            return
        pair = self._active_pair_for_chart(ctx)
        if not pair:
            return
        ce_sym = self._pair_leg(pair, "CE").get("tradingsymbol") or self.state.ce_symbol
        pe_sym = self._pair_leg(pair, "PE").get("tradingsymbol") or self.state.pe_symbol
        quotes = ctx.get("quotes") or {}
        ce = quotes.get(ce_sym) if ce_sym else None
        pe = quotes.get(pe_sym) if pe_sym else None
        if ce is None or pe is None:
            return
        # First samples of the session: pull 09:15 history before appending live
        if not self.state.live_series or len(self.state.live_series) < 30:
            self._sync_day_live_series(cfg, ctx)
        self.state.live_series.append(
            {"time": int(now), "ce": round(float(ce), 2), "pe": round(float(pe), 2)}
        )
        cap = int(cfg.get("convergence_chart_points", 2000))
        self.state.live_series = self.state.live_series[-cap:]
        self.state.live_series_ts = now
        if self.state.tl_armed:
            self.state.tl_tick = max(0, len(self.state.live_series) - 1)
            # Project existing rails onto new samples between 30s re-fits
            self._attach_chart_trendlines()

    def _rhythm(self, cfg: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
        """Prior-session quiet/big rhythm, computed once per session."""
        if not cfg.get("rhythm_enabled", True):
            return None
        today = datetime.now(IST).date().isoformat()
        if self.state.rhythm_date == today and self.state.rhythm:
            return self.state.rhythm
        market = ctx.get("market")
        candles = list(getattr(market, "nifty_candles_5m", None) or [])
        if not candles:
            return self.state.rhythm
        self.state.rhythm = compute_rhythm(candles, cfg, today=today)
        self.state.rhythm_date = today
        return self.state.rhythm

    def _append_lean_hist(
        self,
        cfg: dict[str, Any],
        ctx: dict[str, Any],
        ce_ltp: float,
        pe_ltp: float,
    ) -> None:
        """Tick CE/PE/spot/ATM histories used by flip-resistant lean."""
        self.state.ce_hist.append(ce_ltp)
        self.state.pe_hist.append(pe_ltp)
        spot_now = self._as_map(ctx.get("index")).get("spot")
        if spot_now is not None:
            try:
                self.state.spot_hist.append(float(spot_now))
            except (TypeError, ValueError):
                pass
        gate = self._as_map(self._as_map(ctx.get("scan")).get("merge_gate"))
        atm_ce_px = gate.get("ce_atm")
        atm_pe_px = gate.get("pe_atm")
        if atm_ce_px is not None and atm_pe_px is not None:
            try:
                self.state.atm_ce_hist.append(float(atm_ce_px))
                self.state.atm_pe_hist.append(float(atm_pe_px))
            except (TypeError, ValueError):
                pass
        n = int(cfg.get("history_seconds", 500))
        self.state.ce_hist = self.state.ce_hist[-n:]
        self.state.pe_hist = self.state.pe_hist[-n:]
        self.state.spot_hist = self.state.spot_hist[-n:]
        self.state.atm_ce_hist = self.state.atm_ce_hist[-n:]
        self.state.atm_pe_hist = self.state.atm_pe_hist[-n:]

    def _refresh_chart_pipe(self, cfg: dict[str, Any]) -> None:
        """Overlay tilted merge pipe rails onto conv_zone for the dashboard chart.

        Once a good pipe is found during merge, freeze it so it still plots
        after the breakout split (otherwise rails vanish when gap opens).
        """
        chart_gap = float(
            cfg.get(
                "convergence_pipe_gap",
                float(cfg.get("convergence_zone_gap", 10.0)) * 1.25,
            )
        )
        min_s = max(4, int(cfg.get("convergence_pipe_min_samples", 4)))
        chart_pipe = pipe_from_live_series(
            self.state.live_series,
            gap_tol=chart_gap,
            min_samples=min_s,
            max_soft_breach_pct=float(cfg.get("convergence_pipe_soft_breach_pct", 0.12)),
            max_soft_breach_floor=int(cfg.get("convergence_pipe_soft_breach_floor", 8)),
            max_hard_bridge=int(cfg.get("convergence_pipe_hard_bridge", 2)),
            rail_mode=str(cfg.get("convergence_rail_mode", "band")),
        )
        pts = (chart_pipe or {}).get("pipe_points") or []
        # Keep extending the merge pipe (1m → ~2h). Prefer longer coverage;
        # freeze only after breakout so rails still show the full range.
        if (
            chart_pipe
            and len(pts) >= 3
            and (chart_pipe.get("pipe") or {}).get("tilt") in ("down", "flat", "up")
        ):
            # After breakout, keep the merge-range rails that broke — don't redraw on re-merge
            if self.state.phase in (
                "BREAKOUT_WATCH",
                "FALLEN_HOLD",
                "DUAL_OPEN",
                "TL_HOLD",
                "TL_MAP",
            ):
                if self.state.pipe_chart and (self.state.pipe_chart.get("pipe_points") or []):
                    pass  # frozen
                else:
                    self.state.pipe_chart = {
                        "pipe": chart_pipe.get("pipe"),
                        "pipe_points": pts,
                        "samples": int(chart_pipe.get("samples") or len(pts)),
                        "source": chart_pipe.get("source"),
                    }
            else:
                old_pts = (self.state.pipe_chart or {}).get("pipe_points") or []
                old_n = len(old_pts)
                new_n = len(pts)
                new_span = int(chart_pipe.get("samples") or new_n)
                end_i = int(chart_pipe.get("end_i") or -1)
                n_ser = len(self.state.live_series)
                active = end_i >= n_ser - 3
                take = (
                    self.state.pipe_chart is None
                    or (active and new_n >= max(3, int(old_n * 0.7)))
                    or new_span > int((self.state.pipe_chart or {}).get("samples") or old_n)
                )
                if take:
                    self.state.pipe_chart = {
                        "pipe": chart_pipe.get("pipe"),
                        "pipe_points": pts,
                        "samples": new_span,
                        "end_i": end_i,
                        "source": chart_pipe.get("source"),
                    }
        frozen = self.state.pipe_chart
        use = chart_pipe if (chart_pipe and pts) else frozen
        if not use:
            return
        pipe = use.get("pipe") or (frozen or {}).get("pipe")
        pipe_points = use.get("pipe_points") or (frozen or {}).get("pipe_points") or []
        if self.state.conv_zone is not None:
            self.state.conv_zone = {
                **self.state.conv_zone,
                "pipe": pipe or self.state.conv_zone.get("pipe"),
                "pipe_points": pipe_points,
                "chart_source": use.get("source") or "frozen",
                "pipe_frozen": frozen is not None,
            }
        else:
            self.state.conv_zone = {
                **(use if isinstance(use, dict) else {}),
                "pipe": pipe,
                "pipe_points": pipe_points,
                "pipe_frozen": frozen is not None,
            }
        # Chart-only: project dotted TLs onto live series times (never touches orders)
        self._attach_chart_trendlines()

    def _attach_chart_trendlines(self) -> None:
        """Map up/down TLs onto live_series from day-start swing — no broker calls."""
        series = self.state.live_series or []
        if not series:
            return
        if self.state.conv_zone is None:
            self.state.conv_zone = {}

        conv_i = (
            int(self.state.conv_series_i)
            if self.state.conv_series_i is not None
            else None
        )

        # Infer premium sample step (fallback 5s) so EOD extension stays on-grid
        bar_sec = 5
        if len(series) >= 3:
            try:
                dt = int(series[-1]["time"]) - int(series[-3]["time"])
                if dt > 0:
                    bar_sec = max(1, int(round(dt / 2.0)))
            except (TypeError, ValueError, KeyError):
                pass

        def _project(tl: dict[str, Any] | None) -> list[dict[str, Any]]:
            if not tl:
                return []
            # Always start at convergence (or TL anchor) — never paint pre-merge rails
            anchor = int(
                conv_i
                if conv_i is not None
                else (
                    tl.get("anchor_fi")
                    if tl.get("anchor_fi") is not None
                    else tl.get("i0")
                    or 0
                )
            )
            out: list[dict[str, Any]] = []
            last_i = -1
            for i, p in enumerate(series):
                if i < anchor:
                    continue
                lvl = trendline_value(tl, i)
                if lvl is None:
                    continue
                out.append({"time": p.get("time"), "value": round(float(lvl), 2)})
                last_i = i
            if last_i < 0:
                return out
            # Extend CE/PE / winner-fallen rails through 15:30
            return self._extend_tl_points_to_eod(
                out, tl, last_i=last_i, bar_seconds=bar_sec, cfg=None
            )

        self.state.conv_zone = {
            **self.state.conv_zone,
            "tl_up_points": _project(self.state.up_tl),
            "tl_down_points": _project(self.state.down_tl),
            "tl_w_dn_points": _project(self.state.w_dn_tl),
            "tl_f_up_points": _project(self.state.f_up_tl),
            "ce_ch_hi_points": _project(self.state.ce_hi_tl),
            "ce_ch_lo_points": _project(self.state.ce_lo_tl),
            "pe_ch_hi_points": _project(self.state.pe_hi_tl),
            "pe_ch_lo_points": _project(self.state.pe_lo_tl),
            "merge_band_high": self.state.merge_band_high,
            "merge_band_low": self.state.merge_band_low,
            "conv_mid": self.state.last_convergence_mid or self.state.mid_at_entry,
            "conv_series_i": conv_i,
        }

    def on_tick(self, ctx: dict[str, Any]) -> None:
        cfg = self.resolve_cfg(ctx["config"])
        quotes = ctx.get("quotes") or {}
        index = ctx.get("index") or {}
        release: Callable[[str], None] = ctx["release"]
        pair_regime = ctx.get("pair_regime")
        self._sample_series(cfg, ctx)
        self._rhythm(cfg, ctx)

        # Drop ghost DUAL_OPEN after legs closed outside _clear (orphan/manual)
        if self._sync_flat_if_no_opens(release):
            return

        # Manage restored / open positions even without scan pair
        if self.state.phase in (
            "DUAL_OPEN",
            "BREAKOUT_WATCH",
            "FALLEN_HOLD",
            "TL_HOLD",
            "TL_MAP",
        ):
            ce_sym = self.state.ce_symbol
            pe_sym = self.state.pe_symbol
            ce_ltp = quotes.get(ce_sym) if ce_sym else None
            pe_ltp = quotes.get(pe_sym) if pe_sym else None
            # FALLEN_HOLD may only need one leg
            if self.state.phase == "FALLEN_HOLD":
                f_sym = self.state.ce_symbol if self.state.fallen_side == "CE" else self.state.pe_symbol
                f_px = quotes.get(f_sym or "") if f_sym else None
                if f_px is None and self.state.fallen_trade_id:
                    ft = next(
                        (t for t in trade_store.open_trades() if t.get("id") == self.state.fallen_trade_id),
                        None,
                    )
                    if ft and ft.get("live_price") is not None:
                        f_px = float(ft["live_price"])
                    elif ft and ft.get("entry_price") is not None:
                        f_px = float(ft["entry_price"])
                    # Partial roll: CW already closed — still drive manage so buy can finish
                    if f_px is None:
                        closed = next(
                            (t for t in trade_store.list_trades() if t.get("id") == self.state.fallen_trade_id),
                            None,
                        )
                        if closed and "roll→NW" in str(closed.get("exit_reason") or ""):
                            f_px = float(closed.get("exit_price") or closed.get("live_price") or self.state.pe_entry or 0)
                if f_px is not None:
                    ce_ltp = ce_ltp if ce_ltp is not None else (
                        f_px if self.state.fallen_side == "CE" else (self.state.ce_entry or f_px)
                    )
                    pe_ltp = pe_ltp if pe_ltp is not None else (
                        f_px if self.state.fallen_side == "PE" else (self.state.pe_entry or f_px)
                    )
                    self._manage(cfg, float(ce_ltp), float(pe_ltp), index, release, pair_regime, ctx)
                else:
                    self.state.last_message = f"restored fallen — waiting quote {f_sym}"
                return
            # Breakout-only: manage with winner quote; fabricate opposite from last entry if needed
            if self.state.phase == "BREAKOUT_WATCH" and self.state.fallen_trade_id is None:
                b = self.state.breakout_side
                b_px = ce_ltp if b == "CE" else pe_ltp
                if b_px is not None:
                    if ce_ltp is None:
                        ce_ltp = float(self.state.ce_entry or b_px)
                    if pe_ltp is None:
                        pe_ltp = float(self.state.pe_entry or b_px)
                    self._append_lean_hist(cfg, ctx, float(ce_ltp), float(pe_ltp))
                    self._refresh_chart_pipe(cfg)
                    self._manage(
                        cfg, float(ce_ltp), float(pe_ltp), index, release, pair_regime, ctx
                    )
                    return
            # Fall back to trade live_price when quote map hasn't caught the symbols yet
            if ce_ltp is None and self.state.ce_trade_id:
                ct = next(
                    (t for t in trade_store.open_trades() if t.get("id") == self.state.ce_trade_id),
                    None,
                )
                if ct and ct.get("live_price") is not None:
                    ce_ltp = float(ct["live_price"])
            if pe_ltp is None and self.state.pe_trade_id:
                pt = next(
                    (t for t in trade_store.open_trades() if t.get("id") == self.state.pe_trade_id),
                    None,
                )
                if pt and pt.get("live_price") is not None:
                    pe_ltp = float(pt["live_price"])
            if ce_ltp is None and self.state.ce_entry is not None:
                ce_ltp = float(self.state.ce_entry)
            if pe_ltp is None and self.state.pe_entry is not None:
                pe_ltp = float(self.state.pe_entry)
            if ce_ltp is not None and pe_ltp is not None:
                self._append_lean_hist(cfg, ctx, float(ce_ltp), float(pe_ltp))
                self._refresh_chart_pipe(cfg)
                self._manage(cfg, float(ce_ltp), float(pe_ltp), index, release, pair_regime, ctx)
                return
            self.state.last_message = "restored — waiting CE/PE quotes"
            return

        if not cfg.get("algo2_enabled", True):
            self.state.last_message = "disabled"
            return

        pair = self._normalize_pair(ctx.get("pair")) or self._normalize_pair(self.state.pair)
        in_session = ctx.get("in_session", False)
        mode = "live" if cfg.get("live_trading") else "paper"
        try_acquire: Callable[[str], bool] = ctx["try_acquire"]
        decay_block = bool(ctx.get("decay_block"))

        if not pair:
            self.state.last_message = "no pair"
            return

        ce_sym = self._pair_leg(pair, "CE").get("tradingsymbol")
        pe_sym = self._pair_leg(pair, "PE").get("tradingsymbol")
        if not ce_sym or not pe_sym:
            self.state.last_message = "no pair"
            return
        ce_ltp = quotes.get(ce_sym)
        pe_ltp = quotes.get(pe_sym)
        if ce_ltp is None or pe_ltp is None:
            return

        self.state.pair = pair
        self._append_lean_hist(cfg, ctx, float(ce_ltp), float(pe_ltp))
        self.state.conv_zone = detect_convergence_zone(
            self.state.ce_hist,
            self.state.pe_hist,
            gap_tol=float(cfg.get("convergence_zone_gap", 10.0)),
            min_samples=int(cfg.get("convergence_zone_min_samples", 25)),
        )
        self._refresh_chart_pipe(cfg)

        if not in_session:
            start = cfg.get("session_start", "09:15")
            end = cfg.get("session_end", "13:30")
            self.state.last_message = f"outside {start}–{end} window"
            return

        if decay_block:
            self.state.last_message = f"decay guard: {ctx.get('decay_msg', 'blocked')}"
            return

        # Pair-exit multi-cycle: short cool-down; otherwise global entry cool-down
        pair_exit_on = float(cfg.get("algo2_pair_exit_mult") or 0) > 0
        cd_min = int(
            cfg.get("algo2_reentry_cooldown_minutes", 1)
            if pair_exit_on
            else cfg.get("entry_cooldown_minutes", 60)
        )
        if cd_min > 0:
            since = trade_store.seconds_since_last_close(self.algo_id, mode)
            if since is not None and since < cd_min * 60:
                left = int(cd_min * 60 - since)
                self.state.last_message = f"cool-down {left // 60}m {left % 60}s left"
                return

        if trade_store.count_day_setups(None, mode) >= int(cfg.get("max_trades_per_day", 15)):
            self.state.last_message = "daily setup cap"
            return
        # Dual multi-cycle: 3rd+ setups were systematically EOD losers in backtest
        a2_cap = int(cfg.get("algo2_max_setups_per_day", 2) or 2)
        a2_setups = trade_store.count_day_setups(self.algo_id, mode)
        if a2_cap > 0 and a2_setups >= a2_cap:
            self.state.last_message = f"{self.algo_id} daily setup cap ({a2_cap})"
            return

        now_hm = datetime.now(IST).strftime("%H:%M")
        # Live rule: 1st after merge_after; optional 2nd in [11:00, 13:00) unless first paid well.
        if self.state.phase == "SCAN":
            blocked = self._second_entry_blocked(cfg, mode, now_hm, a2_setups)
            if blocked:
                self.state.merge_armed = False
                self.state.last_message = blocked
                return

        tol = float(cfg.get("convergence_tol", 3.0))
        low = float(cfg.get("premium_low", 125))
        high = float(cfg.get("premium_high", 145))
        if self.state.conv_zone:
            converged = in_convergence_zone(ce_ltp, pe_ltp, self.state.conv_zone, entry_tol=tol)
        else:
            converged = is_converged(ce_ltp, pe_ltp, tol, low, high)

        breakout_only = cfg.get("algo2_breakout_only", True)
        merge_after = str(cfg.get("algo2_merge_after") or "09:20")
        # No arm / buy before merge_after (default 09:20). Open-leg management later.
        if self.state.phase == "SCAN" and now_hm < merge_after:
            self.state.merge_armed = False
            self.state.last_message = f"wait merge after {merge_after} (now {now_hm})"
            return
        # Arm on first merge; stay armed so hard-gap buy can fire after split.
        # Same-day re-merge ABOVE the day's first conv mid = decay trap — skip.
        mid_now = (float(ce_ltp) + float(pe_ltp)) / 2.0
        if converged:
            ok_conv, conv_why = self._accept_day_convergence(cfg, mid_now)
            if not ok_conv:
                self.state.merge_armed = False
                # Higher re-merge TLs are invalid (upside → decay)
                if self.state.phase == "SCAN":
                    self.state.up_tl = None
                    self.state.down_tl = None
                    self.state.tl_armed = False
                    if self.state.conv_zone is not None:
                        self.state.conv_zone = {
                            **self.state.conv_zone,
                            "tl_up_points": [],
                            "tl_down_points": [],
                        }
                self.state.last_message = conv_why
                return
            self.state.merge_armed = True
            # Track last tight merge mid for fall-back SL (while still SCAN)
            if self.state.phase == "SCAN":
                self.state.last_convergence_mid = mid_now
        if not converged and not (breakout_only and self.state.merge_armed):
            z = self.state.conv_zone
            zmsg = f" zone {z['low']:.0f}-{z['high']:.0f}" if z else ""
            self.state.last_message = (
                f"waiting converge gap={abs(ce_ltp - pe_ltp):.1f}{zmsg}{rhythm_tag(self.state.rhythm)}"
            )
            return

        # Expiry day: never enter current-week / 0DTE (field + both symbols)
        pair_exp = (
            pair.get("expiry")
            or self._pair_leg(pair, "CE").get("expiry")
            or self._pair_leg(pair, "PE").get("expiry")
        )
        if not expiry_allowed_for_entry(pair_exp, symbol=ce_sym) or not expiry_allowed_for_entry(
            pair_exp, symbol=pe_sym
        ):
            self.state.last_message = (
                f"skip same-day expiry {ce_sym}/{pe_sym} ({pair_exp}) — use next week"
            )
            return
        scan = ctx.get("scan") or {}
        if self.week_mode == "next":
            trade_week = str(scan.get("next_expiry") or scan.get("expiry") or "")[:10]
        else:
            trade_week = str(scan.get("current_expiry") or "")[:10]
        if trade_week and str(pair_exp or "")[:10] != trade_week:
            self.state.last_message = (
                f"skip wrong week {pair_exp} — want {self.week_mode}={trade_week}"
            )
            return

        # Breakout-only (default): do NOT buy both legs — wait for side, buy winner only
        if breakout_only:
            side = self._detect_breakout_side(cfg, ctx, ce_ltp, pe_ltp)
            if side not in ("CE", "PE"):
                gap = abs(ce_ltp - pe_ltp)
                lean = (self.state.lean_signal or {}).get("reason") or "waiting side"
                self.state.last_message = (
                    f"WATCH merge gap={gap:.1f} · {lean}{rhythm_tag(self.state.rhythm)}"
                )
                return
            if not try_acquire(self.algo_id):
                self.state.last_message = "pair locked by other algo"
                return
            self._enter_breakout_side(
                cfg, mode, pair, side, ce_sym, pe_sym, ce_ltp, pe_ltp, ctx, release
            )
            return

        # Dual: both legs only while still converged and inside 125–145
        if not converged:
            z = self.state.conv_zone
            zmsg = f" zone {z['low']:.0f}-{z['high']:.0f}" if z else ""
            self.state.last_message = (
                f"waiting converge gap={abs(ce_ltp - pe_ltp):.1f}{zmsg}{rhythm_tag(self.state.rhythm)}"
            )
            return
        if not (low <= float(ce_ltp) <= high and low <= float(pe_ltp) <= high):
            self.state.last_message = (
                f"wait dual in-band CE@{ce_ltp:.1f} PE@{pe_ltp:.1f} "
                f"({low:g}–{high:g})"
            )
            return

        ok_conv, conv_why = self._accept_day_convergence(
            cfg, (float(ce_ltp) + float(pe_ltp)) / 2.0
        )
        if not ok_conv:
            self.state.last_message = conv_why
            return

        if not try_acquire(self.algo_id):
            self.state.last_message = "pair locked by other algo"
            return

        self._enter_dual(cfg, mode, pair, ce_sym, pe_sym, ce_ltp, pe_ltp, ctx, release)

    def _detect_breakout_side(
        self,
        cfg: dict[str, Any],
        ctx: dict[str, Any],
        ce_ltp: float,
        pe_ltp: float,
    ) -> str | None:
        """Pick CE/PE breakout side after merge — take expansion LEADER, not pullback."""
        gap = abs(ce_ltp - pe_ltp)
        hard = float(cfg.get("algo2_breakout_hard_gap", 12.0))
        merge_gap = float(cfg.get("early_winner_lean_merge_gap", 8.0))
        leader = "CE" if ce_ltp > pe_ltp else ("PE" if pe_ltp > ce_ltp else None)

        # Remember peak expansion after merge — visual break side
        if self.state.merge_armed and gap >= self.state.peak_gap_seen:
            self.state.peak_gap_seen = gap
            if leader in ("CE", "PE"):
                self.state.expansion_leader = leader

        # Breakout-only: prefer mid+N entry, else rail/hard + confirm
        if cfg.get("algo2_breakout_only", True):
            if not self.state.merge_armed:
                self.state.lean_signal = {"ok": False, "reason": "wait merge arm"}
                return None
            entry_pts = float(cfg.get("algo2_entry_above_conv_pts", 12.0) or 0)
            if entry_pts > 0:
                mid_hit = detect_entry_above_convergence(
                    ce=ce_ltp,
                    pe=pe_ltp,
                    conv_mid=self.state.last_convergence_mid,
                    pts=entry_pts,
                )
                if mid_hit.get("ok") and mid_hit.get("side") in ("CE", "PE"):
                    self.state.lean_side = str(mid_hit["side"])
                    self.state.lean_signal = {
                        "ok": True,
                        "side": mid_hit["side"],
                        "reason": mid_hit.get("reason"),
                        "threshold": mid_hit.get("threshold"),
                    }
                    return str(mid_hit["side"])
                self.state.lean_signal = {
                    "ok": False,
                    "side": mid_hit.get("side"),
                    "reason": mid_hit.get("reason")
                    or f"wait ≥conv+{entry_pts:g}",
                    "threshold": mid_hit.get("threshold"),
                }
                # Still allow rail/hard path below if mid unknown; otherwise wait mid+N
                if self.state.last_convergence_mid is not None:
                    return None
            confirm_n = int(cfg.get("algo2_break_confirm_points", 3))
            buf = float(cfg.get("algo2_break_rail_buffer", 1.0))
            force = leader if gap >= hard and leader in ("CE", "PE") else None
            zone = self.state.conv_zone or self.state.pipe_chart
            rb = detect_rail_breakout(
                zone=zone if isinstance(zone, dict) else None,
                ce=ce_ltp,
                pe=pe_ltp,
                hist_len=max(len(self.state.ce_hist), len(self.state.pe_hist)),
                buffer=buf,
                confirm_points=confirm_n,
                confirm_side=self.state.rail_confirm_side,
                confirm_count=self.state.rail_confirm_count,
                winner_px_hist=self.state.rail_winner_px,
                hard_gap=hard,
                force_side=force,
            )
            if rb.get("aborted"):
                self.state.rail_confirm_side = None
                self.state.rail_confirm_count = 0
                self.state.rail_winner_px = []
                self.state.lean_signal = {"ok": False, "reason": rb.get("reason")}
                return None
            if rb.get("side"):
                new_side = str(rb["side"])
                if self.state.rail_confirm_side and self.state.rail_confirm_side != new_side:
                    self.state.rail_winner_px = []
                self.state.rail_confirm_side = new_side
                self.state.rail_confirm_count = int(rb.get("confirm_count") or 0)
                px = ce_ltp if new_side == "CE" else pe_ltp
                self.state.rail_winner_px.append(px)
                self.state.rail_winner_px = self.state.rail_winner_px[-12:]
            if rb.get("ok") and rb.get("side") in ("CE", "PE"):
                self.state.lean_side = str(rb["side"])
                self.state.lean_signal = {
                    "ok": True,
                    "side": rb["side"],
                    "reason": rb.get("reason"),
                    "rails": rb.get("rails"),
                }
                return str(rb["side"])
            self.state.lean_signal = {
                "ok": False,
                "side": rb.get("side"),
                "reason": rb.get("reason") or f"wait rail/hard+{confirm_n} up",
                "rails": rb.get("rails"),
            }
            return None

        # Legacy dual path helpers (lean / candle) — not used for breakout-only
        if self.state.merge_armed and gap >= hard and leader in ("CE", "PE"):
            self.state.lean_side = leader
            return leader

        market = ctx.get("market")
        candles_1m = list(getattr(market, "nifty_candles_1m", None) or [])
        spot_now = (ctx.get("index") or {}).get("spot")
        try:
            spot_now_f = float(spot_now) if spot_now is not None else None
        except (TypeError, ValueError):
            spot_now_f = None
        candle = spot_1m_candle_side(candles_1m, spot_now=spot_now_f)
        lean = detect_early_winner_lean(
            ce_hist=self.state.ce_hist,
            pe_hist=self.state.pe_hist,
            spot_hist=self.state.spot_hist,
            atm_ce_hist=self.state.atm_ce_hist or None,
            atm_pe_hist=self.state.atm_pe_hist or None,
            spot_candle=candle,
            cfg=cfg,
        )
        self.state.lean_signal = {**lean, "candle": candle}
        if lean.get("ok") and lean.get("side") in ("CE", "PE"):
            self.state.lean_side = str(lean["side"])
            return str(lean["side"])
        candle_side = candle.get("side")
        if (
            cfg.get("early_winner_lean_candle_break", True)
            and candle_side in ("CE", "PE")
            and len(self.state.ce_hist) >= 3
            and len(self.state.pe_hist) >= 3
        ):
            ce_move = ce_ltp - self.state.ce_hist[-3]
            pe_move = pe_ltp - self.state.pe_hist[-3]
            if candle_side == "CE" and ce_move >= 3 and ce_move > pe_move:
                return "CE"
            if candle_side == "PE" and pe_move >= 3 and pe_move > ce_move:
                return "PE"
        return None

    def _enter_breakout_side(
        self,
        cfg: dict[str, Any],
        mode: str,
        pair: dict[str, Any],
        side: str,
        ce_sym: str,
        pe_sym: str,
        ce_ltp: float,
        pe_ltp: float,
        ctx: dict[str, Any],
        release: Callable[[str], None],
    ) -> None:
        """Buy ONLY the breakout leg — never the fallen side."""
        merge_after = str(cfg.get("algo2_merge_after") or "09:20")
        now_hm = datetime.now(IST).strftime("%H:%M")
        if now_hm < merge_after:
            self.state.last_message = f"skip breakout — before {merge_after} (now {now_hm})"
            release(self.algo_id)
            return
        lots = int(cfg.get("algo2_lots", 1))
        lot_size = int(cfg.get("lot_size", 75))
        setup_id = str(uuid.uuid4())[:8]
        sym = ce_sym if side == "CE" else pe_sym
        px = ce_ltp if side == "CE" else pe_ltp
        band_lo = float(cfg.get("premium_low", 125))
        band_hi = float(cfg.get("premium_high", 145))
        entry_pts = float(cfg.get("algo2_entry_above_conv_pts", 12.0) or 0)
        # mid+N often prints above premium_high (e.g. 135+12=147) — allow that ceiling
        band_ceil = band_hi
        if (
            entry_pts > 0
            and self.state.last_convergence_mid is not None
        ):
            band_ceil = max(
                band_hi, float(self.state.last_convergence_mid) + entry_pts + 2.0
            )
        if px is None or not (band_lo <= float(px) <= band_ceil):
            self.state.last_message = (
                f"skip {side}@{px} outside {band_lo:g}–{band_ceil:g}"
            )
            release(self.algo_id)
            return
        if (
            self.state.last_convergence_mid is None
            and abs(ce_ltp - pe_ltp) > float(cfg.get("early_winner_lean_merge_gap", 8.0))
        ):
            self.state.last_message = (
                f"skip {side} — no convergence yet gap={abs(ce_ltp - pe_ltp):.1f}"
            )
            release(self.algo_id)
            return
        leg = self._pair_leg(pair, side)
        entry_reason = "breakout_only"
        if (
            entry_pts > 0
            and self.state.last_convergence_mid is not None
            and float(px) >= float(self.state.last_convergence_mid) + entry_pts
        ):
            entry_reason = "conv_mid_plus"
        feats = build_entry_features(
            side=side,
            confidence=0.75,
            reason=entry_reason,
            index=ctx.get("index"),
            macro=ctx.get("macro"),
            pair_regime=ctx.get("pair_regime"),
            gap=abs(ce_ltp - pe_ltp),
        )
        trade = broker.open_long(
            mode=mode,
            algo=self.algo_id,
            strategy="BreakoutOnly",
            symbol=sym,
            side=side,
            entry_price=px,
            lots=lots,
            lot_size=lot_size,
            cfg=cfg,
            allow_add=False,
            extra={
                "expiry": pair.get("expiry") or leg.get("expiry"),
                "strike": pair.get("strike") or leg.get("strike"),
                "instrument_token": int(leg.get("instrument_token") or 0),
                "leg_role": "breakout",
                "setup_id": setup_id,
                "entry_features": {
                    **feats,
                    "tags": ["convergence", entry_reason, side],
                    "conv_mid": self.state.last_convergence_mid,
                    "entry_above_conv_pts": entry_pts,
                },
                "rhythm": rhythm_payload(self.state.rhythm),
            },
        )
        if trade.get("status") != "OPEN":
            tip = ((trade.get("diagnosis") or {}).get("tips") or [None])[0]
            self.state.last_message = (
                trade.get("message") or trade.get("note") or "breakout buy rejected"
            ) + (f" · {tip}" if tip else "")
            release(self.algo_id)
            return

        self.state.setup_id = setup_id
        self.state.pair = pair
        self.state.ce_symbol = ce_sym
        self.state.pe_symbol = pe_sym
        self.state.ce_entry = ce_ltp
        self.state.pe_entry = pe_ltp
        # Freeze last convergence mid for fall-back SL (never replace with flip entry)
        hard = float(cfg.get("algo2_breakout_hard_gap", 12.0))
        merge_gap = float(cfg.get("early_winner_lean_merge_gap", 8.0))
        gap_now = abs(ce_ltp - pe_ltp)
        if gap_now <= merge_gap or gap_now < hard * 0.75:
            self.state.last_convergence_mid = (ce_ltp + pe_ltp) / 2
        if self.state.last_convergence_mid is None and gap_now < hard:
            self.state.last_convergence_mid = (ce_ltp + pe_ltp) / 2
        self.state.mid_at_entry = self.state.last_convergence_mid
        if side == "CE":
            self.state.ce_trade_id = trade["id"]
            self.state.pe_trade_id = None
        else:
            self.state.pe_trade_id = trade["id"]
            self.state.ce_trade_id = None
        self.state.breakout_side = side
        self.state.fallen_side = None  # intentionally unused
        self.state.breakout_trade_id = trade["id"]
        self.state.fallen_trade_id = None
        self.state.breakout_peak = px
        self.state.breakout_at = datetime.now(IST)
        self.state.lean_side = side
        self.state.lean_added = True  # no lean-add on top
        self.state.phase = "BREAKOUT_WATCH"
        # Fresh reverse-confirm for the new breakout leg
        self.state.reverse_confirm_side = None
        self.state.reverse_confirm_count = 0
        self.state.reverse_winner_px = []
        self.state.rail_confirm_side = None
        self.state.rail_confirm_count = 0
        self.state.rail_winner_px = []
        self.state.fallen_rev_arm = None
        strike = leg.get("strike") or pair.get("strike")
        strike_txt = f"{int(strike)}" if strike is not None else ""
        mid_txt = (
            f" · mid {self.state.last_convergence_mid:.0f}+{entry_pts:g}"
            if entry_reason == "conv_mid_plus" and self.state.last_convergence_mid is not None
            else ""
        )
        msg = (
            f"BOUGHT BREAKOUT {side}{strike_txt} {sym} @{px:.1f}"
            f" (only · no fallen){mid_txt}"
        )
        self.state.last_message = msg
        self._notify(ctx, msg)
        # Arm both-side high/low channels for chart + fallen-TL book gate
        if cfg.get("algo2_breakout_tl_enabled", True):
            try:
                self._arm_day_trendlines(cfg, ctx, side=side)
            except Exception:
                pass

    def _seed_day_conv_from_trades(self) -> None:
        """If we already traded today, lock day_conv_mid from that setup's mid."""
        today = datetime.now(IST).date().isoformat()
        if self.state.day_conv_date != today:
            self.state.day_conv_date = today
            self.state.day_conv_mid = None
        if self.state.day_conv_mid is not None:
            return
        try:
            rows = [
                t
                for t in trade_store.list_trades()
                if t.get("algo") == self.algo_id
                and trade_store.is_pnl_trade(t)
                and str(t.get("opened_at") or "").startswith(today)
            ]
        except Exception:
            return
        if not rows:
            return
        # Earliest setup mid from CE+PE entries
        by_setup: dict[str, list[dict[str, Any]]] = {}
        for t in rows:
            sid = str(t.get("setup_id") or t.get("id"))
            by_setup.setdefault(sid, []).append(t)
        earliest = min(
            by_setup.keys(),
            key=lambda s: min(str(x.get("opened_at") or "") for x in by_setup[s]),
        )
        legs = by_setup[earliest]
        px = [float(t["entry_price"]) for t in legs if t.get("entry_price") is not None]
        if len(px) >= 2:
            self.state.day_conv_mid = (px[0] + px[1]) / 2.0
        elif px:
            self.state.day_conv_mid = px[0]

    def _scrub_day_conv_outside_band(self, cfg: dict[str, Any]) -> None:
        """Drop locked day mid if it sits outside the live premium band (band reset)."""
        if self.state.day_conv_mid is None:
            return
        lo = float(cfg.get("premium_low", 125))
        hi = float(cfg.get("premium_high", 145))
        mid = float(self.state.day_conv_mid)
        if mid < lo or mid > hi:
            self.state.day_conv_mid = None
            self.state.merge_band_high = None
            self.state.merge_band_low = None

    def _accept_day_convergence(
        self, cfg: dict[str, Any], mid: float
    ) -> tuple[bool, str]:
        """
        First same-day convergence sets the reference mid.
        A later re-merge at a *higher* mid is upside→decay — reject entry/TLs.
        """
        if not cfg.get("algo2_reject_higher_remerge", True):
            return True, "ok"
        self._scrub_day_conv_outside_band(cfg)
        self._seed_day_conv_from_trades()
        self._scrub_day_conv_outside_band(cfg)
        today = datetime.now(IST).date().isoformat()
        if self.state.day_conv_date != today:
            self.state.day_conv_date = today
            self.state.day_conv_mid = None
        buf = float(cfg.get("algo2_higher_remerge_buffer", 2.0))
        mid_f = float(mid)
        if self.state.day_conv_mid is None:
            self.state.day_conv_mid = mid_f
            return True, "ok"
        if mid_f > float(self.state.day_conv_mid) + buf:
            return (
                False,
                f"skip higher re-merge mid {mid_f:.0f}>"
                f"day conv {self.state.day_conv_mid:.0f} (decay)",
            )
        return True, "ok"

    def _second_entry_blocked(
        self, cfg: dict[str, Any], mode: str, now_hm: str, setups: int
    ) -> str | None:
        """
        Optional 2nd dual: only [second_after, last_before) and skip if first paid well.
        Returns a status message when blocked, else None. 1st setup (setups==0) always ok.
        """
        if setups < 1:
            return None
        second_after = str(cfg.get("algo2_second_entry_after") or "11:00").strip()
        last_before = str(cfg.get("algo2_last_entry_before") or "13:00").strip()
        if second_after and now_hm < second_after:
            return f"2nd dual wait ≥{second_after} (now {now_hm}, setups={setups})"
        if last_before and now_hm >= last_before:
            return f"no 2nd dual after {last_before} (setups={setups})"
        skip_ge = float(cfg.get("algo2_skip_second_if_day_pnl_ge") or 0)
        if skip_ge > 0:
            day_stats = trade_store.day_pnl_stats(self.algo_id, mode)
            day_net = float(day_stats.get("realized_pnl") or day_stats.get("pnl") or 0)
            if day_net >= skip_ge:
                return f"skip 2nd — day net ₹{day_net:,.0f} ≥ ₹{skip_ge:,.0f}"
        return None

    def _enter_dual(
        self,
        cfg: dict[str, Any],
        mode: str,
        pair: dict[str, Any],
        ce_sym: str,
        pe_sym: str,
        ce_ltp: float,
        pe_ltp: float,
        ctx: dict[str, Any],
        release: Callable[[str], None],
    ) -> None:
        merge_after = str(cfg.get("algo2_merge_after") or "09:20")
        now_hm = datetime.now(IST).strftime("%H:%M")
        if now_hm < merge_after:
            self.state.last_message = f"skip dual — before {merge_after} (now {now_hm})"
            release(self.algo_id)
            return
        a2_setups = trade_store.count_day_setups(self.algo_id, mode)
        blocked = self._second_entry_blocked(cfg, mode, now_hm, a2_setups)
        if blocked:
            self.state.merge_armed = False
            self.state.last_message = blocked
            release(self.algo_id)
            return
        lot_size = int(cfg.get("lot_size", 75))
        mid = (ce_ltp + pe_ltp) / 2
        if cfg.get("algo2_dual_size_from_capital", True):
            lots = size_dual_pair_lots(
                cfg,
                ce_entry=float(ce_ltp),
                pe_entry=float(pe_ltp),
                algo=self.algo_id,
            )
        else:
            lots = int(cfg.get("algo2_lots", 1))
        ok_conv, conv_why = self._accept_day_convergence(cfg, mid)
        if not ok_conv:
            self.state.last_message = conv_why
            release(self.algo_id)
            return
        setup_id = str(uuid.uuid4())[:8]
        feats_ce = build_entry_features(
            side="CE",
            confidence=0.5,
            reason="dual entry",
            index=ctx.get("index"),
            macro=ctx.get("macro"),
            pair_regime=ctx.get("pair_regime"),
            gap=abs(ce_ltp - pe_ltp),
        )
        feats_pe = build_entry_features(
            side="PE",
            confidence=0.5,
            reason="dual entry",
            index=ctx.get("index"),
            macro=ctx.get("macro"),
            pair_regime=ctx.get("pair_regime"),
            gap=abs(ce_ltp - pe_ltp),
        )

        ce_leg = pair.get("ce") if isinstance(pair.get("ce"), dict) else {}
        pe_leg = pair.get("pe") if isinstance(pair.get("pe"), dict) else {}
        # Fire CE+PE MARKET orders in parallel — do not wait for CE fill before PE.
        def _buy_ce() -> dict[str, Any]:
            return broker.open_long(
                mode=mode,
                algo=self.algo_id,
                strategy="DualConvergence",
                symbol=ce_sym,
                side="CE",
                entry_price=ce_ltp,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                allow_add=False,
                extra={
                    "expiry": pair.get("expiry") or ce_leg.get("expiry"),
                    "strike": pair.get("strike") or ce_leg.get("strike"),
                    "instrument_token": int(ce_leg.get("instrument_token") or 0),
                    "leg_role": "dual",
                    "setup_id": setup_id,
                    "entry_features": feats_ce,
                    "rhythm": rhythm_payload(self.state.rhythm),
                },
            )

        def _buy_pe() -> dict[str, Any]:
            return broker.open_long(
                mode=mode,
                algo=self.algo_id,
                strategy="DualConvergence",
                symbol=pe_sym,
                side="PE",
                entry_price=pe_ltp,
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                allow_add=True,  # peer leg of same dual setup
                extra={
                    "expiry": pair.get("expiry") or pe_leg.get("expiry"),
                    "strike": pair.get("strike") or pe_leg.get("strike"),
                    "instrument_token": int(pe_leg.get("instrument_token") or 0),
                    "leg_role": "dual",
                    "setup_id": setup_id,
                    "entry_features": feats_pe,
                    "rhythm": rhythm_payload(self.state.rhythm),
                },
            )

        ce_trade: dict[str, Any] = {}
        pe_trade: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-buy") as pool:
            f_ce = pool.submit(_buy_ce)
            f_pe = pool.submit(_buy_pe)
            ce_trade = f_ce.result()
            pe_trade = f_pe.result()
        if ce_trade.get("status") != "OPEN" or pe_trade.get("status") != "OPEN":
            # Roll back a one-sided fill if the other leg was blocked
            lot_size = int(cfg.get("lot_size", 75))
            if ce_trade.get("status") == "OPEN" and ce_trade.get("id"):
                broker.close_long(ce_trade["id"], ce_ltp, "dual peer blocked same-day expiry", lot_size)
            if pe_trade.get("status") == "OPEN" and pe_trade.get("id"):
                broker.close_long(pe_trade["id"], pe_ltp, "dual peer blocked same-day expiry", lot_size)
            diag = ce_trade.get("diagnosis") or pe_trade.get("diagnosis") or {}
            tip = (diag.get("tips") or [None])[0]
            self.state.last_message = (
                ce_trade.get("message")
                or pe_trade.get("message")
                or ce_trade.get("note")
                or pe_trade.get("note")
                or "dual entry rejected"
            ) + (f" · {tip}" if tip else "")
            release(self.algo_id)
            return
        self.state.setup_id = setup_id
        self.state.ce_trade_id = ce_trade["id"]
        self.state.pe_trade_id = pe_trade["id"]
        self.state.ce_symbol = ce_sym
        self.state.pe_symbol = pe_sym
        self.state.ce_entry = ce_ltp
        self.state.pe_entry = pe_ltp
        self.state.mid_at_entry = mid
        self.state.last_convergence_mid = mid
        # Flat merge band for chart (same as playback) — does not close anything
        hw = max(4.0, abs(float(ce_ltp) - float(pe_ltp)) / 2.0 + 2.0)
        self.state.merge_band_high = float(mid) + hw
        self.state.merge_band_low = float(mid) - hw
        self.state.phase = "DUAL_OPEN"
        pair_exit = float(cfg.get("algo2_pair_exit_mult") or 0)
        hold_below = float(cfg.get("algo2_pair_hold_fallen_below", 45) or 45)
        wait_rt = bool(cfg.get("algo2_pair_exit_wait_retrace", False))
        rmin = float(cfg.get("algo2_pair_exit_retrace_min_pct", 5) or 5)
        rmax = float(cfg.get("algo2_pair_exit_retrace_max_pct", 30) or 30)
        if pair_exit > 0 and wait_rt:
            exit_note = (
                f" → arm @{pair_exit:g}× then retrace {rmin:g}–{rmax:g}% "
                f"· hold fallen if <{hold_below:g}"
            )
        elif pair_exit > 0:
            exit_note = (
                f" → book @{pair_exit:g}× · hold fallen if <{hold_below:g}"
            )
        else:
            exit_note = ""
        self.state.last_message = (
            f"dual entry {lots}L CE@{ce_ltp:.1f} PE@{pe_ltp:.1f}{exit_note}"
        )
        # Chart from 09:15 even when merge/entry is after 09:20
        if ctx:
            self._sync_day_live_series(cfg, ctx)
        # Lock TL origin to this dual's convergence bar
        if self.state.live_series:
            self.state.conv_series_i = self._locate_convergence_index(
                list(self.state.live_series), cfg
            )
        # Pair-exit dual never waits for band-break — arm both channels now
        self._ensure_dual_channels(cfg, ctx)
        self._notify(
            ctx,
            f"BOUGHT DUAL {lots}L CE {ce_sym} @{ce_ltp:.1f} + PE {pe_sym} @{pe_ltp:.1f} "
            f"(mid~{mid:.1f}){exit_note}",
        )

    def _manage(
        self,
        cfg: dict[str, Any],
        ce_ltp: float,
        pe_ltp: float,
        index: dict[str, Any],
        release: Callable[[str], None],
        pair_regime: Any = None,
        ctx: dict[str, Any] | None = None,
    ) -> None:
        ctx = ctx or {}
        lot_size = int(cfg.get("lot_size", 75))
        lots = int(cfg.get("algo2_lots", 1))
        qty = lots * lot_size
        expand_cap = float(cfg.get("expand_target_mult", 2.5))

        if self.state.ce_trade_id and self.state.ce_entry is not None:
            trade_store.update_trade(
                self.state.ce_trade_id,
                {
                    "live_price": ce_ltp,
                    "live_pnl_rupees": round((ce_ltp - self.state.ce_entry) * qty, 2),
                    "live_mult": round(ce_ltp / self.state.ce_entry, 2),
                },
            )
        if self.state.pe_trade_id and self.state.pe_entry is not None:
            trade_store.update_trade(
                self.state.pe_trade_id,
                {
                    "live_price": pe_ltp,
                    "live_pnl_rupees": round((pe_ltp - self.state.pe_entry) * qty, 2),
                    "live_mult": round(pe_ltp / self.state.pe_entry, 2),
                },
            )

        if self.state.phase == "DUAL_OPEN":
            ce_move = ce_ltp - (self.state.ce_entry or ce_ltp)
            pe_move = pe_ltp - (self.state.pe_entry or pe_ltp)
            ce_mult = ce_ltp / self.state.ce_entry if self.state.ce_entry else 1
            pe_mult = pe_ltp / self.state.pe_entry if self.state.pe_entry else 1

            # Pair-exit ladder via Nifty channel:
            # Arm @1.45× → wait 5–30% peak retrace (or fallen reverse) → book
            # Optional Index stretch toward 2×/2.5× while channel favors winner
            pair_exit = float(cfg.get("algo2_pair_exit_mult") or 0)
            if pair_exit > 0:
                self._ensure_dual_channels(cfg, ctx)
                try:
                    self._fit_index_channel_and_sr(cfg, ctx)
                except Exception:
                    pass

                win = "CE" if ce_mult >= pe_mult else "PE"
                win_mult = ce_mult if win == "CE" else pe_mult
                win_ltp = ce_ltp if win == "CE" else pe_ltp
                fallen = "PE" if win == "CE" else "CE"
                fallen_ltp = pe_ltp if fallen == "PE" else ce_ltp
                max_mult = float(cfg.get("algo2_index_hold_max_mult", 2.5) or 2.5)
                giveback_pct = float(cfg.get("algo2_index_hold_giveback_pct", 15) or 15)
                trail_floor = bool(cfg.get("algo2_index_hold_trail_at_pair_exit", True))
                hold_on = bool(cfg.get("algo2_index_channel_hold", True))
                wait_retrace = bool(cfg.get("algo2_pair_exit_wait_retrace", False))
                retrace_min = float(cfg.get("algo2_pair_exit_retrace_min_pct", 5) or 5)
                retrace_max = float(cfg.get("algo2_pair_exit_retrace_max_pct", 30) or 30)
                if retrace_max < retrace_min:
                    retrace_min, retrace_max = retrace_max, retrace_min
                fallen_rev_pts = float(cfg.get("algo2_pair_exit_fallen_reverse_pts", 8) or 8)
                fallen_rev_pct = float(cfg.get("algo2_pair_exit_fallen_reverse_pct", 12) or 12)
                ladder = self._stretch_ladder(cfg, pair_exit)
                bias, frac = self._index_channel_pos(cfg)
                frac_s = f"{frac:.0%}" if frac is not None else "—"

                def _book(side: str, why: str) -> None:
                    self.state.index_trend_hold = False
                    self.state.pair_retrace_armed = False
                    self._pair_exit_winner(
                        cfg,
                        win_side=side,
                        ce_ltp=ce_ltp,
                        pe_ltp=pe_ltp,
                        ce_mult=ce_mult,
                        pe_mult=pe_mult,
                        pair_exit=pair_exit,
                        lot_size=lot_size,
                        ctx=ctx,
                        release=release,
                    )
                    self.state.last_message = f"{self.state.last_message} · {why}"

                def _arm_retrace(side: str, mult: float, ltp: float, f_ltp: float) -> None:
                    self.state.pair_retrace_armed = True
                    self.state.pair_retrace_side = side
                    self.state.pair_retrace_peak_mult = float(mult)
                    self.state.pair_retrace_peak_ltp = float(ltp)
                    self.state.pair_retrace_fallen_low = float(f_ltp)

                def _fallen_reversed(f_ltp: float) -> bool:
                    low = self.state.pair_retrace_fallen_low
                    if low is None:
                        return False
                    if f_ltp < low:
                        self.state.pair_retrace_fallen_low = float(f_ltp)
                        return False
                    up = float(f_ltp) - float(low)
                    pct_need = max(0.0, float(low) * fallen_rev_pct / 100.0)
                    return up >= fallen_rev_pts or (pct_need > 0 and up >= pct_need)

                # ——— manage armed retrace / Index stretch ———
                if self.state.pair_retrace_armed or self.state.index_trend_hold:
                    hold_side = (
                        str(self.state.pair_retrace_side or self.state.index_hold_side or win)
                    )
                    if hold_side not in ("CE", "PE"):
                        hold_side = win
                    hold_mult = ce_mult if hold_side == "CE" else pe_mult
                    hold_ltp = ce_ltp if hold_side == "CE" else pe_ltp
                    f_side = "PE" if hold_side == "CE" else "CE"
                    f_ltp = pe_ltp if f_side == "PE" else ce_ltp

                    if self.state.pair_retrace_fallen_low is None or f_ltp < float(
                        self.state.pair_retrace_fallen_low
                    ):
                        self.state.pair_retrace_fallen_low = float(f_ltp)

                    # Track peak on winner
                    if hold_mult >= float(self.state.pair_retrace_peak_mult or 0):
                        self.state.pair_retrace_peak_mult = float(hold_mult)
                        self.state.pair_retrace_peak_ltp = float(hold_ltp)
                    self.state.index_hold_peak_mult = max(
                        float(self.state.index_hold_peak_mult or 0),
                        float(self.state.pair_retrace_peak_mult or hold_mult),
                    )
                    peak = float(
                        self.state.pair_retrace_peak_mult
                        or self.state.index_hold_peak_mult
                        or hold_mult
                    )

                    # Fallen reverse while still dual-open → exit both NOW
                    if _fallen_reversed(f_ltp):
                        low = float(self.state.pair_retrace_fallen_low or f_ltp)
                        _book(
                            hold_side,
                            f"fallen {f_side} reverse +{f_ltp - low:.1f} "
                            f"from low {low:.1f} → exit now",
                        )
                        return

                    supports = self._index_supports_extension(cfg, hold_side)
                    at_cap = hold_mult >= max_mult
                    gb = (1.0 - (hold_mult / peak)) * 100.0 if peak > 1e-9 else 0.0

                    # Index stretch still allowed while channel favors winner
                    if (
                        hold_on
                        and self.state.index_trend_hold
                        and supports
                        and not at_cap
                        and hold_mult > pair_exit
                    ):
                        next_tgt = next((t for t in ladder if hold_mult < t - 1e-6), None)
                        tgt_s = f"{next_tgt:g}×" if next_tgt else f"cap {max_mult:g}×"
                        self.state.last_message = (
                            f"Index stretch {hold_side} {hold_mult:.2f}× "
                            f"(peak {peak:.2f}× → next {tgt_s}) "
                            f"ix={bias}/{frac_s} · retrace exit "
                            f"{retrace_min:g}–{retrace_max:g}% · watch {f_side} reverse"
                        )
                        return

                    if at_cap:
                        _book(hold_side, f"hit {max_mult:g}× stretch")
                        return

                    # Retrace band: book once giveback ≥ min%, force by max%
                    if wait_retrace and peak > pair_exit - 1e-6:
                        if gb >= retrace_max - 1e-9:
                            _book(
                                hold_side,
                                f"retrace {gb:.1f}% ≥ max {retrace_max:g}% "
                                f"from peak {peak:.2f}×",
                            )
                            return
                        if gb >= retrace_min - 1e-9:
                            _book(
                                hold_side,
                                f"retrace {gb:.1f}% "
                                f"({retrace_min:g}–{retrace_max:g}%) "
                                f"from peak {peak:.2f}×",
                            )
                            return
                        # Index faded → still wait for min retrace (no instant dump)
                        if self.state.index_trend_hold and not supports:
                            self.state.index_trend_hold = False
                        lo_stop = peak * (1.0 - retrace_min / 100.0)
                        hi_stop = peak * (1.0 - retrace_max / 100.0)
                        self.state.last_message = (
                            f"wait retrace {hold_side} {hold_mult:.2f}× "
                            f"peak {peak:.2f}× giveback {gb:.1f}% "
                            f"→ sell @{lo_stop:.2f}–{hi_stop:.2f}× "
                            f"({retrace_min:g}–{retrace_max:g}%) · "
                            f"watch {f_side} reverse · ix={bias}/{frac_s}"
                        )
                        return

                    # Legacy path (wait_retrace off): Index trail / giveback @ pair-exit
                    floor_hit = trail_floor and hold_mult <= pair_exit * 1.001
                    trail_stop = max(
                        pair_exit,
                        peak * (1.0 - giveback_pct / 100.0),
                    )
                    trail_hit = peak > pair_exit + 0.02 and hold_mult <= trail_stop
                    if floor_hit or (not supports) or trail_hit:
                        why = (
                            f"trail @{pair_exit:g}× (Index faded)"
                            if floor_hit or (not supports and hold_mult <= pair_exit * 1.05)
                            else f"giveback trail {trail_stop:.2f}× from peak {peak:.2f}×"
                        )
                        _book(hold_side, why)
                        return
                    self.state.last_message = (
                        f"dual hold {hold_side} {hold_mult:.2f}× peak {peak:.2f}× "
                        f"trail≥{pair_exit:g}× · ix={bias}/{frac_s}"
                    )
                    return

                # ——— first touch of 1.45× ———
                if ce_mult >= pair_exit or pe_mult >= pair_exit:
                    _arm_retrace(win, win_mult, win_ltp, fallen_ltp)
                    if (
                        hold_on
                        and win_mult < max_mult
                        and self._index_supports_extension(cfg, win)
                    ):
                        self.state.index_trend_hold = True
                        self.state.index_hold_side = win
                        self.state.index_hold_peak_mult = float(win_mult)
                        nxt = ladder[0] if ladder else max_mult
                        self.state.last_message = (
                            f"armed @{pair_exit:g}× · Index stretch {win} "
                            f"toward {nxt:g}×/{max_mult:g}× "
                            f"(now {win_mult:.2f}×) · else retrace "
                            f"{retrace_min:g}–{retrace_max:g}% · "
                            f"watch {fallen} reverse · ix={bias}/{frac_s}"
                        )
                        return
                    if wait_retrace:
                        lo_stop = win_mult * (1.0 - retrace_min / 100.0)
                        self.state.last_message = (
                            f"armed @{pair_exit:g}× {win} {win_mult:.2f}× "
                            f"→ wait retrace {retrace_min:g}–{retrace_max:g}% "
                            f"(sell ~{lo_stop:.2f}×+) · "
                            f"watch {fallen} reverse · ix={bias}/{frac_s}"
                        )
                        return
                    # Instant book only when wait_retrace disabled
                    _book(
                        win,
                        f"trail @{pair_exit:g}× (Index not stretched · ix={bias})",
                    )
                    return

                hold_below = float(cfg.get("algo2_pair_hold_fallen_below", 45) or 45)
                ladder_s = "/".join(f"{t:g}" for t in ladder) or f"{max_mult:g}"
                retrace_s = (
                    f"retrace {retrace_min:g}–{retrace_max:g}%"
                    if wait_retrace
                    else f"trail @{pair_exit:g}×"
                )
                self.state.last_message = (
                    f"dual hold CE {ce_mult:.2f}× PE {pe_mult:.2f}× "
                    f"→ arm @{pair_exit:g}× then {retrace_s} "
                    f"/ stretch {ladder_s}× if Index OK · fallen<{hold_below:g} "
                    f"· ix={bias}/{frac_s}"
                )
                return

            # Regime-aware early square
            if pair_regime is not None and pair_regime.regime == "DECAY":
                if ce_move <= -5 and pe_move <= -5:
                    self._square_both(lot_size, ce_ltp, pe_ltp, "decay regime both", release)
                    return
            if pair_regime is not None and pair_regime.regime == "RANGE_BOUND":
                if abs(ce_move) < 3 and abs(pe_move) < 3:
                    self.state.last_message = "dual open range-bound — waiting skew"
                elif ce_move <= -8 and pe_move <= -8:
                    self._square_both(lot_size, ce_ltp, pe_ltp, "range decay both", release)
                    return

            if ce_move <= -8 and pe_move <= -8:
                self._square_both(lot_size, ce_ltp, pe_ltp, "range decay both", release)
                return

            tl_on = bool(cfg.get("algo2_breakout_tl_enabled", True))
            mid0 = float(self.state.mid_at_entry or ((ce_ltp + pe_ltp) / 2))
            b_hi = float(self.state.merge_band_high if self.state.merge_band_high is not None else mid0 + 6)
            b_lo = float(self.state.merge_band_low if self.state.merge_band_low is not None else mid0 - 6)
            # Seed flat band once if missing
            if self.state.merge_band_high is None:
                hw = max(4.0, abs(ce_ltp - pe_ltp) / 2 + 2.0)
                self.state.merge_band_high = mid0 + hw
                self.state.merge_band_low = mid0 - hw
                b_hi, b_lo = self.state.merge_band_high, self.state.merge_band_low

            side = None
            if tl_on:
                if ce_ltp >= b_hi and ce_ltp > pe_ltp:
                    side = "CE"
                elif pe_ltp >= b_hi and pe_ltp > ce_ltp:
                    side = "PE"
            else:
                ready = dual_book_winner(
                    float(ce_ltp),
                    float(pe_ltp),
                    float(self.state.ce_entry or ce_ltp),
                    float(self.state.pe_entry or pe_ltp),
                    cfg,
                )
                if ready and ready.get("side") in ("CE", "PE"):
                    side = str(ready["side"])

            if side in ("CE", "PE"):
                px = float(ce_ltp if side == "CE" else pe_ltp)
                f_px = float(pe_ltp if side == "CE" else ce_ltp)
                self._mark_breakout(side, px, ctx=ctx)
                self.state.fallen_low = f_px
                # Chart TLs + winner exits below up-TL (fallen kept until its own exit)
                if tl_on:
                    self._arm_day_trendlines(cfg, ctx, side=side)
                    self.state.phase = "TL_HOLD"
                    self.state.last_message = (
                        f"band break {side} · up-TL armed · exit when below TL"
                    )
                    self._notify(ctx, self.state.last_message)
                    return
                # Legacy: book winner immediately at target
                reason = "dual book"
                tid = self.state.breakout_trade_id
                if tid:
                    broker.close_long(tid, px, reason, lot_size)
                    trade_store.update_trade(tid, {"leg_role": "breakout_closed"})
                if side == "CE":
                    self.state.ce_trade_id = None
                else:
                    self.state.pe_trade_id = None
                self.state.phase = "FALLEN_HOLD"
                self._notify(ctx, f"BOOKED {side} @{px:.1f}; hold opposite retrace")
                self.state.last_message = f"booked {side}; hold opposite retrace"
                return

            crush = (
                f" crush≤{pair_regime.crush_level:.0f}"
                if pair_regime and pair_regime.crush_level
                else ""
            )
            self.state.last_message = (
                f"dual hold ΔCE {ce_move:+.1f} ΔPE {pe_move:+.1f} "
                f"wait break >{b_hi:.0f}{crush}"
            )
            return

        if self.state.phase in ("TL_MAP", "TL_HOLD"):
            # TL_MAP legacy → same as TL_HOLD: book breakout when it falls below up-TL
            b_side = self.state.breakout_side
            f_side = self.state.fallen_side
            w_px = ce_ltp if b_side == "CE" else pe_ltp
            f_px = pe_ltp if b_side == "CE" else ce_ltp
            self._correct_day_trendlines(cfg, ctx)
            if (self.state.up_tl or {}).get("index_space") == "series":
                self.state.tl_tick = max(0, len(self.state.live_series or []) - 1)
            else:
                self.state.tl_tick += 1
            tick = self.state.tl_tick
            buf = float(cfg.get("algo2_breakout_tl_buffer", 1.5))
            self._attach_chart_trendlines()
            if self.state.breakout_peak is None or w_px > self.state.breakout_peak:
                self.state.breakout_peak = w_px
            if self.state.fallen_low is None or f_px < self.state.fallen_low:
                self.state.fallen_low = f_px
            # Safety: both legs crushed
            ce_m = ce_ltp - (self.state.ce_entry or ce_ltp)
            pe_m = pe_ltp - (self.state.pe_entry or pe_ltp)
            if ce_m <= -8 and pe_m <= -8:
                self._square_both(lot_size, ce_ltp, pe_ltp, "range decay both", release)
                return
            # Breakout exit: price falls below rising up-TL (as in chart)
            if self._book_winner_below_up_tl(
                cfg,
                w_px=float(w_px),
                tick=tick,
                lot_size=lot_size,
                ctx=ctx,
                release=release,
            ):
                return
            # Fallen down-TL exit is opt-in (default off — hold for retrace / other exits)
            if (
                bool(cfg.get("algo2_breakout_tl_exit_live", False))
                and self.state.fallen_trade_id
                and self.state.down_tl
                and fallen_broke_down_tl(
                    float(f_px),
                    self.state.down_tl,
                    tick,
                    buffer=buf,
                    armed=self.state.tl_armed,
                )
            ):
                reason = (
                    f"fallen broke down-TL {trendline_value(self.state.down_tl, tick):.1f}"
                )
                broker.close_long(self.state.fallen_trade_id, f_px, reason, lot_size)
                self.state.fallen_trade_id = None
                if f_side == "CE":
                    self.state.ce_trade_id = None
                else:
                    self.state.pe_trade_id = None
                self._notify(ctx, f"closed fallen: {reason}")
            if not self.state.breakout_trade_id and not self.state.fallen_trade_id:
                self._clear(release)
                self.state.last_message = "TL cycle done"
                return
            if not self.state.breakout_trade_id and self.state.fallen_trade_id:
                self.state.phase = "FALLEN_HOLD"
                self.state.last_message = f"fallen {f_side} hold · down-TL"
                return
            up_v = trendline_value(self.state.up_tl, tick)
            dn_v = trendline_value(self.state.down_tl, tick)
            self.state.phase = "TL_HOLD"
            self.state.last_message = (
                f"TL hold W {b_side}@{w_px:.1f} tl={up_v:.1f} · "
                f"exit if < TL · F {f_side}@{f_px:.1f}"
                if up_v is not None and w_px is not None
                else "TL hold · exit breakout below up-TL"
            )
            return

        if self.state.phase == "BREAKOUT_WATCH":
            b_side = self.state.breakout_side
            px = ce_ltp if b_side == "CE" else pe_ltp
            entry = self.state.ce_entry if b_side == "CE" else self.state.pe_entry
            if self.state.breakout_peak is None or px > self.state.breakout_peak:
                self.state.breakout_peak = px

            mins = 0.0
            if self.state.breakout_at:
                mins = (datetime.now(IST) - self.state.breakout_at).total_seconds() / 60

            mult = px / entry if entry else 1.0
            f_side = self.state.fallen_side or ("PE" if b_side == "CE" else "CE")
            f_px = pe_ltp if b_side == "CE" else ce_ltp
            self._note_prem_tick(ce_ltp, pe_ltp)

            book_reason: str | None = None

            # 0) SL only after leave, then fall-back to last convergence mid
            book_reason = self._convergence_fallback_sl(cfg, px)

            # Keep both-side channel TLs fresh while holding breakout
            if cfg.get("algo2_breakout_tl_enabled", True):
                if not self.state.tl_armed and self.state.breakout_side in ("CE", "PE"):
                    self._arm_day_trendlines(
                        cfg, ctx, side=str(self.state.breakout_side)
                    )
                else:
                    self._correct_day_trendlines(cfg, ctx)
                if (self.state.up_tl or {}).get("index_space") == "series":
                    self.state.tl_tick = max(0, len(self.state.live_series or []) - 1)
                else:
                    self.state.tl_tick += 1

            # 1) Book 168–175: hard at 175; soft needs fallen channel TL break
            t_lo = float(cfg.get("breakout_premium_target_low", 168))
            t_hi = float(cfg.get("breakout_premium_target_high", 175))
            if book_reason is None and cfg.get("breakout_premium_target_enabled", True):
                need_ftl = bool(cfg.get("breakout_book_needs_fallen_tl", True))
                buf = float(cfg.get("algo2_breakout_tl_buffer", 1.0))
                tick = int(self.state.tl_tick or 0)
                fallen_broke = bool(
                    self.state.down_tl
                    and fallen_broke_down_tl(
                        float(f_px),
                        self.state.down_tl,
                        tick,
                        buffer=buf,
                        armed=self.state.tl_armed,
                    )
                )
                book_reason = premium_target_hit(
                    float(px),
                    target_low=t_lo,
                    target_high=t_hi,
                    fallen_tl_broke=fallen_broke,
                    require_fallen_tl=need_ftl,
                )

            # Flip targets (fallen candle or opposite-rail)
            reverse_flip = False
            reverse_side: str | None = None

            # 2) Fallen reverse-hammer / hammer on 30s or 1m → break that candle
            #    (+ optional flip into the reversing fallen leg)
            if book_reason is None and cfg.get("breakout_fallen_rev_exit", True):
                fallen_reason = self._fallen_rev_break_reason(cfg, f_side, f_px)
                if fallen_reason:
                    book_reason = fallen_reason
                    if (
                        cfg.get("algo2_fallen_rev_flip", True)
                        and f_side in ("CE", "PE")
                        and cfg.get("algo2_breakout_only", True)
                    ):
                        reverse_flip = True
                        reverse_side = str(f_side)

            # 3) Rare stretch: ~2.5× only if still holding past premium band
            if book_reason is None and cfg.get("breakout_expand_stretch_enabled", True):
                if mult >= expand_cap and px >= t_hi:
                    book_reason = f"expand stretch {mult:.1f}x"

            # 4) Opposite-rail reverse (book + optional flip)
            if book_reason is None and cfg.get("algo2_rail_reverse_enabled", True):
                zone = self.state.conv_zone or self.state.pipe_chart
                rr = detect_rail_reversal(
                    breakout_side=str(b_side or ""),
                    zone=zone if isinstance(zone, dict) else None,
                    ce=ce_ltp,
                    pe=pe_ltp,
                    hist_len=max(len(self.state.ce_hist), len(self.state.pe_hist), 1),
                    buffer=float(cfg.get("algo2_break_rail_buffer", 1.0)),
                    confirm_points=int(cfg.get("algo2_break_confirm_points", 3)),
                    confirm_side=self.state.reverse_confirm_side,
                    confirm_count=self.state.reverse_confirm_count,
                    winner_px_hist=self.state.reverse_winner_px,
                    hard_gap=float(cfg.get("algo2_breakout_hard_gap", 12.0)),
                )
                if rr.get("aborted"):
                    self.state.reverse_confirm_side = None
                    self.state.reverse_confirm_count = 0
                    self.state.reverse_winner_px = []
                elif rr.get("side"):
                    new_side = str(rr["side"])
                    if self.state.reverse_confirm_side and self.state.reverse_confirm_side != new_side:
                        self.state.reverse_winner_px = []
                    self.state.reverse_confirm_side = new_side
                    self.state.reverse_confirm_count = int(rr.get("confirm_count") or 0)
                    rpx = ce_ltp if new_side == "CE" else pe_ltp
                    if rpx is not None:
                        self.state.reverse_winner_px.append(float(rpx))
                        self.state.reverse_winner_px = self.state.reverse_winner_px[-12:]
                if rr.get("ok") and rr.get("side") in ("CE", "PE"):
                    reverse_side = str(rr["side"])
                    book_reason = str(rr.get("reason") or f"rail reverse → {reverse_side}")
                    reverse_flip = bool(cfg.get("algo2_rail_reverse_flip", True))

            # 5) Classic winner reverse (index patterns) — keep as backup
            if book_reason is None:
                reverse = self._breakout_reverse(index, b_side or "PE", px)
                if pair_regime is not None:
                    if b_side == "CE" and pair_regime.near_resistance and reverse:
                        reverse = True
                    if b_side == "PE" and pair_regime.near_support and reverse:
                        reverse = True
                in_window = mins >= 5
                drop_from_peak = (self.state.breakout_peak or px) - px
                if reverse and (in_window or drop_from_peak >= 5 or mult >= 2.0):
                    book_reason = f"breakout reverse @ {px:.1f} ({mult:.1f}x)"

            if book_reason:
                tid = self.state.breakout_trade_id
                if tid:
                    broker.close_long(tid, px, book_reason, lot_size)
                    trade_store.update_trade(tid, {"leg_role": "breakout_closed"})
                if b_side == "CE":
                    self.state.ce_trade_id = None
                else:
                    self.state.pe_trade_id = None
                self.state.fallen_rev_arm = None
                self.state.reverse_confirm_side = None
                self.state.reverse_confirm_count = 0
                self.state.reverse_winner_px = []
                # Reverse flip only if opposite leg is back inside 125–145
                pair = self.state.pair or {}
                ce_sym = self.state.ce_symbol
                pe_sym = self.state.pe_symbol
                mode = str(ctx.get("mode") or ("live" if cfg.get("live_trading") else "paper"))
                band_lo = float(cfg.get("premium_low", 125))
                band_hi = float(cfg.get("premium_high", 145))
                flip_px = (
                    ce_ltp if reverse_side == "CE" else pe_ltp
                ) if reverse_side in ("CE", "PE") else None
                if (
                    reverse_flip
                    and reverse_side in ("CE", "PE")
                    and flip_px is not None
                    and band_lo <= float(flip_px) <= band_hi
                    and cfg.get("algo2_breakout_only", True)
                    and pair
                    and ce_sym
                    and pe_sym
                ):
                    self._notify(
                        ctx,
                        f"REVERSAL ({book_reason}) booked {b_side} @{px:.1f} · flip {reverse_side}",
                    )
                    self._enter_breakout_side(
                        cfg,
                        mode,
                        pair,
                        reverse_side,
                        ce_sym,
                        pe_sym,
                        ce_ltp,
                        pe_ltp,
                        ctx,
                        release,
                    )
                    return
                # Default: breakout-only — book winner and flat (no fallen buy/hold)
                if cfg.get("algo2_breakout_only", True) or not self.state.fallen_trade_id:
                    skip = ""
                    if reverse_flip and reverse_side in ("CE", "PE"):
                        skip = f" · no flip {reverse_side}@{flip_px} outside band"
                    self._notify(ctx, f"BOOKED {b_side} @{px:.1f} · {book_reason}{skip}")
                    self._clear(release)
                    self.state.last_message = f"booked {b_side}: {book_reason}{skip}"
                    return
                self.state.fallen_low = f_px
                self.state.phase = "FALLEN_HOLD"
                self.state.last_message = f"booked {b_side}: {book_reason}; holding fallen"
                return

            arm = self.state.fallen_rev_arm
            arm_txt = ""
            if arm and arm.get("ok"):
                arm_txt = f" | opp {arm.get('kind')}→brk {arm.get('level')}"
            rev_txt = ""
            if self.state.reverse_confirm_side:
                rev_txt = (
                    f" | rev {self.state.reverse_confirm_side}"
                    f"{self.state.reverse_confirm_count}"
                )
            crush = ""
            if pair_regime and pair_regime.crush_level:
                crush = f" | loser≤{pair_regime.crush_level:.0f}"
            sl_mid = self.state.last_convergence_mid or self.state.mid_at_entry
            leave = float(cfg.get("algo2_sl_leave_pts", 3.0))
            sl_below = float(cfg.get("algo2_sl_below_convergence", 0.0))
            sl_txt = ""
            if sl_mid is not None:
                armed = (
                    self.state.breakout_peak is not None
                    and float(self.state.breakout_peak) >= float(sl_mid) + leave
                )
                stop = float(sl_mid) - sl_below
                sl_txt = (
                    f" | SL@{stop:.1f}{' armed' if armed else ' wait'}"
                )
            self.state.last_message = (
                f"breakout {b_side} peak {self.state.breakout_peak:.1f} "
                f"now {px:.1f} tgt {t_lo:g}–{t_hi:g} ({mult:.1f}x) "
                f"t={mins:.0f}m{arm_txt}{rev_txt}{crush}{sl_txt}"
            )
            return

        if self.state.phase == "FALLEN_HOLD":
            # Breakout-only mode never holds fallen — bail to flat
            if cfg.get("algo2_breakout_only", True) and not self.state.fallen_trade_id:
                self._clear(release)
                return
            f_side = self.state.fallen_side
            f_px = ce_ltp if f_side == "CE" else pe_ltp
            f_entry = self.state.ce_entry if f_side == "CE" else self.state.pe_entry
            tid = self.state.fallen_trade_id
            if self.state.fallen_low is None or f_px < self.state.fallen_low:
                self.state.fallen_low = f_px

            # Holding fallen PUT for reverse → roll to next-week PUT once crushed
            # (skip when intentionally holding crushed pair-exit fallen for bounce)
            if (
                not self.state.pair_fallen_hold
                and f_side == "PE"
                and self._try_fallen_pe_roll(cfg, f_px, f_entry, tid, ctx)
            ):
                return

            bounce = float(cfg.get("fallen_bounce_points", 10))
            bounce_min = float(cfg.get("algo2_pair_fallen_bounce_min", 5) or 5)
            bounce_max = float(cfg.get("algo2_pair_fallen_bounce_max", 10) or 10)
            if bounce_max < bounce_min:
                bounce_min, bounce_max = bounce_max, bounce_min
            sl_pts = float(cfg.get("fallen_sl_points", 10))
            band_lo = float(cfg.get("premium_low", 125))
            # Spot confirms against fallen leg → tighten stop (don't bleed into trend)
            tr = (index or {}).get("session_trend") or {}
            if (
                tr.get("block_counter_trend")
                and tr.get("prefer_side")
                and f_side
                and f_side != tr.get("prefer_side")
            ):
                sl_pts = min(sl_pts, float(cfg.get("algo2_trend_fallen_sl_pts", 6)))
            # After PE calendar roll, targets are vs new NW entry — not old CW mid
            if self.state.fallen_rolled and f_side == "PE":
                mid = float(f_entry or self.state.pe_entry or 135) + bounce
            else:
                mid = float(
                    self.state.last_convergence_mid
                    or self.state.mid_at_entry
                    or 135
                )
            stop_line = (self.state.fallen_low or f_px) - sl_pts
            crush_cut = float(cfg.get("crush_cut", 85))
            low = float(self.state.fallen_low or f_px)
            up_from_low = float(f_px) - low
            # Keep correcting down-TL onto newer tops while fallen is held
            if self.state.down_tl and ctx:
                self._correct_day_trendlines(cfg, ctx)
            if (self.state.down_tl or {}).get("index_space") == "series":
                self.state.tl_tick = max(0, len(self.state.live_series or []) - 1)
            else:
                self.state.tl_tick += 1

            self._attach_chart_trendlines()
            # Primary: break above falling down-TL (live only if exit flag on)
            if (
                cfg.get("algo2_breakout_tl_enabled", True)
                and cfg.get("algo2_breakout_tl_exit_live", False)
                and self.state.down_tl
                and tid
                and fallen_broke_down_tl(
                    float(f_px),
                    self.state.down_tl,
                    self.state.tl_tick,
                    buffer=float(cfg.get("algo2_breakout_tl_buffer", 1.5)),
                    armed=True,
                )
            ):
                reason = (
                    f"fallen broke down-TL "
                    f"{trendline_value(self.state.down_tl, self.state.tl_tick):.1f}"
                )
                broker.close_long(tid, f_px, reason, lot_size)
                self._clear(release)
                self.state.last_message = f"closed fallen: {reason}"
                return

            # Pair-exit hold: sell on +5–10 bounce from low (do not crush-cut ≤50)
            if self.state.pair_fallen_hold and tid:
                # Sold winner reversed down → dump fallen immediately (trend flip)
                if bool(cfg.get("algo2_pair_sold_reverse_exit", True)):
                    sold_side = self.state.sold_winner_side or self.state.breakout_side
                    sold_exit = self.state.sold_winner_exit
                    sold_px = (
                        ce_ltp
                        if sold_side == "CE"
                        else pe_ltp
                        if sold_side == "PE"
                        else None
                    )
                    sold_ref = float(
                        self.state.sold_winner_peak or sold_exit or 0
                    )
                    rev_pct = float(cfg.get("algo2_pair_sold_reverse_pct", 8) or 8)
                    if (
                        sold_px is not None
                        and sold_ref > 0
                        and float(sold_px) <= sold_ref * (1.0 - rev_pct / 100.0)
                    ):
                        drop = (1.0 - float(sold_px) / sold_ref) * 100.0
                        reason = (
                            f"sold {sold_side} reverse −{drop:.1f}% "
                            f"({sold_ref:.1f}→{float(sold_px):.1f}) → exit fallen"
                        )
                        broker.close_long(tid, f_px, reason, lot_size)
                        self._clear(release)
                        self.state.last_message = f"closed fallen: {reason}"
                        return
                if up_from_low >= bounce_min:
                    reason = (
                        f"fallen retrace +{up_from_low:.1f} from low {low:.1f} "
                        f"(target +{bounce_min:g}–{bounce_max:g})"
                    )
                    broker.close_long(tid, f_px, reason, lot_size)
                    self._clear(release)
                    self.state.last_message = f"closed fallen: {reason}"
                    return
                sold_txt = ""
                if self.state.sold_winner_side and self.state.sold_winner_exit:
                    s_px = (
                        ce_ltp
                        if self.state.sold_winner_side == "CE"
                        else pe_ltp
                    )
                    sold_txt = (
                        f" · sold {self.state.sold_winner_side} "
                        f"@{s_px:.1f} (exit {self.state.sold_winner_exit:.1f})"
                    )
                self.state.last_message = (
                    f"hold fallen {f_side} @{f_px:.1f} low {low:.1f} "
                    f"+{up_from_low:.1f} → sell @{bounce_min:g}–{bounce_max:g}pt bounce"
                    f"{sold_txt}"
                )
                return

            # Ride: reverse from crush back near merge mid → promote to breakout
            if (
                cfg.get("algo2_fallen_ride_enabled", True)
                and f_side in ("CE", "PE")
                and tid
                and not self.state.fallen_rolled
                and up_from_low >= bounce
                and float(f_px) >= mid - 2.0
                and float(f_px) >= band_lo
            ):
                self._promote_fallen_to_breakout(
                    cfg, f_side, float(f_px), tid, ctx, reason="fallen reverse → ride"
                )
                return

            reason = None
            # Deep crush ≤50: skip on PE until calendar-roll done (holding for reverse)
            pe_awaiting_roll = (
                f_side == "PE"
                and cfg.get("fallen_pe_roll_enabled", True)
                and not self.state.fallen_rolled
            )
            if f_px <= 50 and not pe_awaiting_roll:
                reason = "fallen deep crush ≤50"
            elif f_px <= stop_line and self.state.fallen_low is not None:
                reason = f"fallen SL ({f_px:.1f} <= {stop_line:.1f})"
            elif f_px <= crush_cut and pair_regime and pair_regime.regime == "DECAY" and not self.state.fallen_rolled:
                reason = f"crush+decay cut @{f_px:.1f}"
            elif not cfg.get("algo2_fallen_ride_enabled", True):
                # Exit on reverse +bounce from low OR near merge mid
                if up_from_low >= bounce:
                    reason = f"fallen reverse +{up_from_low:.0f} from low"
                elif not self.state.fallen_rolled and f_px >= mid - 2.0:
                    reason = "fallen back to convergence"
                elif f_entry is not None and f_px >= f_entry + bounce:
                    reason = f"fallen bounce +{bounce:g}"

            if reason and tid:
                broker.close_long(tid, f_px, reason, lot_size)
                self._clear(release)
                self.state.last_message = f"closed fallen: {reason}"
            else:
                lvl = ""
                for x in (80, 70, 50):
                    if f_px <= x:
                        lvl = f" ≤{x}"
                        break
                roll_tag = " · ROLLED→NW" if self.state.fallen_rolled else ""
                ride_tag = " · wait reverse→ride" if cfg.get("algo2_fallen_ride_enabled", True) else ""
                self.state.last_message = (
                    f"fallen {f_side} @{f_px:.1f}{lvl} low {self.state.fallen_low:.1f} "
                    f"mid~{mid:.0f}{roll_tag}{ride_tag}"
                )

    def _promote_fallen_to_breakout(
        self,
        cfg: dict[str, Any],
        f_side: str,
        f_px: float,
        tid: str,
        ctx: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Fallen reversed near merge mid — ride it as the new breakout leg."""
        trade_store.update_trade(tid, {"leg_role": "breakout"})
        self.state.breakout_side = f_side
        self.state.breakout_trade_id = tid
        self.state.breakout_peak = f_px
        self.state.breakout_at = datetime.now(IST)
        self.state.fallen_side = None
        self.state.fallen_trade_id = None
        self.state.fallen_low = None
        self.state.fallen_rev_arm = None
        self.state.reverse_confirm_side = None
        self.state.reverse_confirm_count = 0
        self.state.reverse_winner_px = []
        # SL still anchored to last convergence mid
        if self.state.last_convergence_mid is not None:
            self.state.mid_at_entry = self.state.last_convergence_mid
        if f_side == "CE":
            self.state.ce_trade_id = tid
            self.state.pe_trade_id = None
        else:
            self.state.pe_trade_id = tid
            self.state.ce_trade_id = None
        self.state.lean_side = f_side
        self.state.phase = "BREAKOUT_WATCH"
        msg = f"FALLEN RIDE {f_side} @{f_px:.1f} · {reason} → breakout watch"
        self.state.last_message = msg
        self._notify(ctx, msg)

    def _try_fallen_pe_roll(
        self,
        cfg: dict[str, Any],
        f_px: float,
        f_entry: float | None,
        tid: str | None,
        ctx: dict[str, Any],
    ) -> bool:
        """
        Sell current-week fallen PUT, buy next-week same-strike PUT.
        Gives the reverse thesis more time (calendar roll).
        """
        if not cfg.get("fallen_pe_roll_enabled", True):
            return False
        if self.state.fallen_rolled or f_entry is None:
            return False

        crush_pct = float(cfg.get("fallen_pe_roll_crush_pct", 0.12))
        crushed = f_px <= float(f_entry) * (1.0 - crush_pct) or f_px <= float(
            cfg.get("crush_cut", 85)
        )

        # Resume if prior tick sold CW but buy failed (partial roll)
        prior = next((t for t in trade_store.list_trades() if t.get("id") == tid), None) if tid else None
        partial = bool(
            prior
            and prior.get("status") == "CLOSED"
            and "roll→NW" in str(prior.get("exit_reason") or "")
        )
        if not crushed and not partial:
            return False

        trade = next((t for t in trade_store.open_trades() if t.get("id") == tid), None) if tid else None
        strike = self.state.fallen_strike
        expiry = self.state.fallen_expiry
        src = trade or prior
        if src:
            strike = strike if strike is not None else src.get("strike")
            expiry = expiry or src.get("expiry")
        if strike is None and self.state.pe_symbol:
            digits = "".join(ch for ch in self.state.pe_symbol if ch.isdigit())
            if len(digits) >= 5:
                try:
                    strike = float(digits[-5:])
                except ValueError:
                    strike = None
        if strike is None:
            self.state.last_message = "fallen PE roll: no strike"
            return False

        try:
            nxt = find_next_week_option(
                side="PE", strike=float(strike), current_expiry=str(expiry) if expiry else None
            )
        except Exception as exc:  # noqa: BLE001
            self.state.last_message = f"fallen PE roll lookup fail: {exc}"
            return False
        if not nxt:
            self.state.last_message = "fallen PE roll: no next-week PE"
            return False

        lo = float(cfg.get("fallen_pe_roll_min_premium", 40))
        hi = float(cfg.get("fallen_pe_roll_max_premium", 180))
        if not (lo <= float(nxt["ltp"]) <= hi):
            self.state.last_message = (
                f"fallen PE roll skip: NW PE {nxt['tradingsymbol']} @{nxt['ltp']:.1f} outside {lo}-{hi}"
            )
            return False

        mode = "live" if cfg.get("live_trading") else "paper"
        lot_size = int(cfg.get("lot_size", 75))
        lots = int(cfg.get("algo2_lots", 1))
        old_sym = self.state.pe_symbol or (src or {}).get("symbol") or "?"

        try:
            # Prefer buy NW first so we are never flat mid-roll; then sell CW
            new_trade = broker.open_long(
                mode=mode,
                algo=self.algo_id,
                strategy="DualConvergence",
                symbol=nxt["tradingsymbol"],
                side="PE",
                entry_price=float(nxt["ltp"]),
                lots=lots,
                lot_size=lot_size,
                cfg=cfg,
                allow_add=True,  # roll replacement while old briefly still OPEN
                extra={
                    "expiry": nxt.get("expiry"),
                    "strike": nxt.get("strike"),
                    "leg_role": "fallen",
                    "setup_id": self.state.setup_id,
                    "note": f"calendar roll from {old_sym}",
                    "instrument_token": nxt.get("instrument_token"),
                    "rolled_from": old_sym,
                },
            )
            if trade is not None and tid:
                broker.close_long(tid, f_px, f"fallen PE roll→NW sell @{f_px:.1f}", lot_size)
        except Exception as exc:  # noqa: BLE001
            self.state.last_message = f"fallen PE roll failed: {exc}"
            return False

        self.state.fallen_rolled = True
        self.state.fallen_roll_from = old_sym
        self.state.fallen_roll_to = nxt["tradingsymbol"]
        self.state.pe_symbol = nxt["tradingsymbol"]
        self.state.pe_trade_id = new_trade["id"]
        self.state.pe_entry = float(new_trade.get("entry_price") or nxt["ltp"])
        self.state.fallen_trade_id = new_trade["id"]
        self.state.fallen_strike = float(nxt["strike"])
        self.state.fallen_expiry = str(nxt["expiry"])
        nw_px = float(new_trade.get("entry_price") or nxt["ltp"])
        self.state.fallen_low = nw_px
        self.state.mid_at_entry = nw_px  # NW premium is the new reference, not CW mid
        market = ctx.get("market")
        if market is not None:
            try:
                market.set_watch(
                    [
                        {
                            "tradingsymbol": nxt["tradingsymbol"],
                            "instrument_token": int(nxt["instrument_token"]),
                        }
                    ],
                    replace=False,
                )
            except Exception:
                pass

        msg = (
            f"ROLLED fallen PE {old_sym} @{f_px:.1f} → {nxt['tradingsymbol']} "
            f"@{nxt['ltp']:.1f} (next week reverse)"
        )
        self.state.last_message = msg
        note = {"ts": datetime.now(IST).isoformat(), "algo": self.algo_id, "message": msg}
        self.state.notification = note
        ctx.setdefault("notifications", []).append(note)
        return True

    def _maybe_lean_add(
        self,
        cfg: dict[str, Any],
        side: str,
        ce_ltp: float,
        pe_ltp: float,
        ctx: dict[str, Any],
    ) -> None:
        """Add lots on the early-lean winner once per setup (before gap opens)."""
        if self.state.lean_added:
            return
        # Breakout-only: single winner lot — no lean-add scaling
        if cfg.get("algo2_breakout_only", True):
            return
        if not cfg.get("early_winner_lean_add_enabled", True):
            return
        add_lots = int(cfg.get("early_winner_lean_add_lots", 1))
        if add_lots <= 0:
            return
        mode = "live" if cfg.get("live_trading") else "paper"
        lot_size = int(cfg.get("lot_size", 75))
        pair = self._normalize_pair(self.state.pair) or {}
        if side == "CE":
            sym = self.state.ce_symbol
            px = ce_ltp
            leg = self._pair_leg(pair, "CE")
        else:
            sym = self.state.pe_symbol
            px = pe_ltp
            leg = self._pair_leg(pair, "PE")
        if not sym or px <= 0:
            return
        try:
            trade = broker.open_long(
                mode=mode,
                algo=self.algo_id,
                strategy="ConvergenceLean",
                symbol=sym,
                side=side,
                entry_price=px,
                lots=add_lots,
                lot_size=lot_size,
                cfg=cfg,
                allow_add=True,
                extra={
                    "expiry": leg.get("expiry"),
                    "strike": leg.get("strike"),
                    "instrument_token": int(leg.get("instrument_token") or 0),
                    "setup_id": self.state.setup_id,
                    "leg_role": "lean_add",
                    "note": f"early winner lean add {side}",
                    "entry_features": {
                        **build_entry_features(
                            side=side,
                            confidence=0.8,
                            reason=f"early_lean_{side}",
                            index=ctx.get("index") or {},
                            macro={},
                        ),
                        "tags": ["convergence", "early_lean", side],
                    },
                },
            )
            if trade.get("status") == "OPEN":
                self.state.lean_added = True
                note = {
                    "ts": datetime.now(IST).isoformat(),
                    "algo": self.algo_id,
                    "message": f"LEAN ADD {side} {sym} @{px:.1f} x{add_lots}",
                }
                self.state.notification = note
                ctx.setdefault("notifications", []).append(note)
        except Exception:
            pass

    def _notify(self, ctx: dict[str, Any] | None, message: str, *, level: str = "info") -> None:
        note = {
            "ts": datetime.now(IST).isoformat(),
            "algo": self.algo_id,
            "level": level,
            "message": message,
        }
        self.state.notification = note
        if ctx is not None:
            ctx.setdefault("notifications", []).append(note)

    def _mark_breakout(
        self, side: str, px: float, *, ctx: dict[str, Any] | None = None
    ) -> None:
        self.state.breakout_side = side
        self.state.fallen_side = "PE" if side == "CE" else "CE"
        self.state.breakout_peak = px
        self.state.breakout_at = datetime.now(IST)
        if side == "CE":
            self.state.breakout_trade_id = self.state.ce_trade_id
            self.state.fallen_trade_id = self.state.pe_trade_id
            sym = self.state.ce_symbol or ""
            strike = (self.state.pair or {}).get("ce_strike") or (
                self.state.pair or {}
            ).get("strike")
            if self.state.ce_trade_id:
                trade_store.update_trade(self.state.ce_trade_id, {"leg_role": "breakout"})
            if self.state.pe_trade_id:
                trade_store.update_trade(self.state.pe_trade_id, {"leg_role": "fallen"})
                pe_t = next(
                    (t for t in trade_store.open_trades() if t.get("id") == self.state.pe_trade_id),
                    None,
                )
                if pe_t:
                    self.state.fallen_strike = pe_t.get("strike")
                    self.state.fallen_expiry = pe_t.get("expiry")
        else:
            self.state.breakout_trade_id = self.state.pe_trade_id
            self.state.fallen_trade_id = self.state.ce_trade_id
            sym = self.state.pe_symbol or ""
            strike = (self.state.pair or {}).get("pe_strike") or (
                self.state.pair or {}
            ).get("strike")
            if self.state.pe_trade_id:
                trade_store.update_trade(self.state.pe_trade_id, {"leg_role": "breakout"})
            if self.state.ce_trade_id:
                trade_store.update_trade(self.state.ce_trade_id, {"leg_role": "fallen"})
                ce_t = next(
                    (t for t in trade_store.open_trades() if t.get("id") == self.state.ce_trade_id),
                    None,
                )
                if ce_t:
                    self.state.fallen_strike = ce_t.get("strike")
                    self.state.fallen_expiry = ce_t.get("expiry")
        self.state.fallen_rolled = False
        self.state.fallen_rev_arm = None
        self.state.prem_ticks = []
        self.state.phase = "BREAKOUT_WATCH"
        strike_txt = f"{int(strike)}" if strike is not None else ""
        msg = (
            f"BREAKOUT BUY {side}{strike_txt} {sym} @{px:.1f} "
            f"(winner · fallen {self.state.fallen_side})"
        )
        self.state.last_message = msg
        self._notify(ctx, msg, level="info")

    def _note_prem_tick(self, ce_ltp: float, pe_ltp: float) -> None:
        now = time.time()
        self.state.prem_ticks.append(
            {"t": now, "ce": float(ce_ltp), "pe": float(pe_ltp)}
        )
        # keep ~20 minutes of ticks
        cut = now - 1200
        self.state.prem_ticks = [x for x in self.state.prem_ticks if float(x["t"]) >= cut][-2400:]

    def _convergence_fallback_sl(
        self, cfg: dict[str, Any], px: float | None
    ) -> str | None:
        """
        Wait until breakout has left the merge zone, then SL only if premium
        falls back to the last convergence mid (not entry−N while still running).
        """
        if px is None:
            return None
        mid = self.state.last_convergence_mid
        if mid is None:
            mid = self.state.mid_at_entry
        if mid is None:
            return None
        mid_f = float(mid)
        leave = float(cfg.get("algo2_sl_leave_pts", 3.0))
        sl_below = float(cfg.get("algo2_sl_below_convergence", 0.0))
        peak = self.state.breakout_peak
        if peak is None or float(peak) < mid_f + leave:
            return None  # still waiting — has not left convergence yet
        stop_px = mid_f - sl_below
        if float(px) <= stop_px:
            return (
                f"SL fell back to convergence {float(px):.1f}≤{stop_px:.1f} "
                f"(last mid {mid_f:.1f})"
            )
        return None

    def _fallen_rev_break_reason(
        self, cfg: dict[str, Any], f_side: str, f_px: float
    ) -> str | None:
        """
        Arm on fallen hammer / reverse-hammer (30s then 1m), book winner when
        that candle's high/low is broken.
        """
        periods = [
            float(cfg.get("breakout_fallen_rev_bar_sec", 30)),
            60.0,
        ]
        # Expire stale arm
        arm = self.state.fallen_rev_arm
        if arm and arm.get("t1"):
            max_age = float(cfg.get("breakout_fallen_rev_arm_sec", 180))
            if time.time() - float(arm["t1"]) > max_age:
                self.state.fallen_rev_arm = None
                arm = None

        if arm and fallen_reversal_broken(arm, fallen_px=f_px):
            return (
                f"fallen {arm.get('kind')} candle break "
                f"{arm.get('break_dir')} {arm.get('level')}"
            )

        key = "pe" if f_side == "PE" else "ce"
        ticks = [
            {"t": x["t"], "px": x[key]}
            for x in self.state.prem_ticks
            if x.get(key) is not None
        ]
        for period in periods:
            bars = build_ohlc_bars(ticks, period_sec=period)
            if len(bars) < 1:
                continue
            sig = detect_fallen_reversal_candle(bars, fallen_side=f_side)
            if not sig:
                continue
            # Only arm a new / stronger signal
            if (
                self.state.fallen_rev_arm is None
                or float(sig["level"]) != float(self.state.fallen_rev_arm.get("level") or 0)
            ):
                sig["period_sec"] = period
                self.state.fallen_rev_arm = sig
            # Same bar close can already be through level on next tick
            if fallen_reversal_broken(self.state.fallen_rev_arm, fallen_px=f_px):
                a = self.state.fallen_rev_arm
                return (
                    f"fallen {a.get('kind')} candle break "
                    f"{a.get('break_dir')} {a.get('level')}"
                )
        return None

    def _breakout_reverse(self, index: dict[str, Any], side: str, px: float) -> bool:
        patterns = index.get("patterns") or {}
        rsi_v = index.get("rsi14")
        peak = self.state.breakout_peak or px
        drop = peak - px
        if side == "CE":
            if patterns.get("shooting_star") or patterns.get("bearish_engulf"):
                return drop >= 3
            if rsi_v and rsi_v > 65 and drop >= 5:
                return True
        else:
            if patterns.get("hammer") or patterns.get("bullish_engulf") or patterns.get("inverted_hammer"):
                return drop >= 3
            if rsi_v and rsi_v < 35 and drop >= 5:
                return True
        return drop >= 8

    def _pair_exit_winner(
        self,
        cfg: dict[str, Any],
        *,
        win_side: str,
        ce_ltp: float,
        pe_ltp: float,
        ce_mult: float,
        pe_mult: float,
        pair_exit: float,
        lot_size: int,
        ctx: dict[str, Any],
        release: Callable[[str], None],
    ) -> None:
        """
        Book the ≥pair_exit winner. If the opposite (fallen) is still below
        algo2_pair_hold_fallen_below (default 45), keep it for a +5–10 bounce;
        otherwise square both.
        """
        fallen_side = "PE" if win_side == "CE" else "CE"
        w_px = float(ce_ltp if win_side == "CE" else pe_ltp)
        f_px = float(pe_ltp if fallen_side == "PE" else ce_ltp)
        hold_below = float(cfg.get("algo2_pair_hold_fallen_below", 45) or 45)
        reason = (
            f"pair exit {win_side} ≥{pair_exit:g}× "
            f"(CE {ce_mult:.2f}×@{ce_ltp:.1f} PE {pe_mult:.2f}×@{pe_ltp:.1f})"
        )
        if f_px >= hold_below:
            self._square_both(lot_size, ce_ltp, pe_ltp, reason, release)
            return

        # Close winner only — hold crushed fallen for bounce
        w_tid = self.state.ce_trade_id if win_side == "CE" else self.state.pe_trade_id
        f_tid = self.state.pe_trade_id if fallen_side == "PE" else self.state.ce_trade_id
        if not w_tid or not f_tid:
            self._square_both(lot_size, ce_ltp, pe_ltp, reason, release)
            return
        broker.close_long(w_tid, w_px, reason, lot_size)
        trade_store.update_trade(w_tid, {"leg_role": "breakout_closed"})
        trade_store.update_trade(f_tid, {"leg_role": "fallen"})
        self.state.breakout_side = win_side
        self.state.breakout_trade_id = None
        self.state.fallen_side = fallen_side
        self.state.fallen_trade_id = f_tid
        self.state.fallen_low = f_px
        self.state.pair_fallen_hold = True
        self.state.sold_winner_side = win_side
        self.state.sold_winner_exit = float(w_px)
        self.state.sold_winner_peak = float(
            self.state.pair_retrace_peak_ltp or self.state.breakout_peak or w_px
        )
        self.state.pair_retrace_armed = False
        if win_side == "CE":
            self.state.ce_trade_id = None
        else:
            self.state.pe_trade_id = None
        bounce_min = float(cfg.get("algo2_pair_fallen_bounce_min", 5) or 5)
        bounce_max = float(cfg.get("algo2_pair_fallen_bounce_max", 10) or 10)
        self.state.phase = "FALLEN_HOLD"
        msg = (
            f"BOOKED {win_side} @{w_px:.1f} ({pair_exit:g}×) · "
            f"hold fallen {fallen_side} @{f_px:.1f}<{hold_below:g} "
            f"→ sell +{bounce_min:g}–{bounce_max:g} bounce · "
            f"watch sold {win_side} reverse"
        )
        self.state.last_message = msg
        self._notify(ctx, msg)
        try:
            self._ensure_dual_channels(cfg, ctx, force=True)
        except Exception:
            pass

    def _square_both(
        self,
        lot_size: int,
        ce_ltp: float,
        pe_ltp: float,
        reason: str,
        release: Callable[[str], None],
    ) -> None:
        # Sell both legs together — don't serialize CE then PE fills.
        jobs: list[tuple[str, float]] = []
        if self.state.ce_trade_id:
            jobs.append((self.state.ce_trade_id, ce_ltp))
        if self.state.pe_trade_id:
            jobs.append((self.state.pe_trade_id, pe_ltp))
        if len(jobs) == 1:
            broker.close_long(jobs[0][0], jobs[0][1], reason, lot_size)
        elif len(jobs) >= 2:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-sell") as pool:
                futs = [
                    pool.submit(broker.close_long, tid, px, reason, lot_size)
                    for tid, px in jobs
                ]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception:
                        pass
        self._clear(release)
        # Unlock day-conv mid so the next dual cycle can lock a fresh merge
        self.state.day_conv_mid = None
        self.state.merge_armed = False
        self.state.last_message = reason

    def _clear(self, release: Callable[[str], None] | None = None) -> None:
        self.state.phase = "SCAN"
        self.state.pair = None
        self.state.ce_trade_id = None
        self.state.pe_trade_id = None
        self.state.breakout_trade_id = None
        self.state.fallen_trade_id = None
        self.state.ce_symbol = None
        self.state.pe_symbol = None
        self.state.ce_entry = None
        self.state.pe_entry = None
        self.state.mid_at_entry = None
        self.state.last_convergence_mid = None
        # Keep day_conv_date / day_conv_mid — same-day higher re-merge still blocked
        self.state.breakout_side = None
        self.state.fallen_side = None
        self.state.breakout_peak = None
        self.state.breakout_at = None
        self.state.fallen_low = None
        self.state.pair_fallen_hold = False
        self.state.setup_id = None
        self.state.fallen_rolled = False
        self.state.fallen_roll_from = None
        self.state.fallen_roll_to = None
        self.state.fallen_strike = None
        self.state.fallen_expiry = None
        self.state.lean_side = None
        self.state.lean_added = False
        self.state.lean_signal = None
        self.state.atm_ce_hist = []
        self.state.atm_pe_hist = []
        self.state.prem_ticks = []
        self.state.fallen_rev_arm = None
        self.state.pipe_chart = None
        self.state.live_series = []
        self.state.live_series_ts = 0.0
        self.state.tl_replot_ts = 0.0
        self.state.up_tl = None
        self.state.down_tl = None
        self.state.w_dn_tl = None
        self.state.f_up_tl = None
        self.state.ce_hi_tl = None
        self.state.ce_lo_tl = None
        self.state.pe_hi_tl = None
        self.state.pe_lo_tl = None
        self.state.ix_hi_tl = None
        self.state.ix_lo_tl = None
        self.state.ix_candles = []
        # Keep index_overlay painting on Nifty chart across setups; clear hold flags
        self.state.index_trend_hold = False
        self.state.index_hold_side = None
        self.state.index_hold_peak_mult = 0.0
        self.state.pair_retrace_armed = False
        self.state.pair_retrace_side = None
        self.state.pair_retrace_peak_mult = 0.0
        self.state.pair_retrace_peak_ltp = 0.0
        self.state.pair_retrace_fallen_low = None
        self.state.sold_winner_side = None
        self.state.sold_winner_exit = None
        self.state.sold_winner_peak = None
        self.state.conv_series_i = None
        self.state.tl_armed = False
        self.state.tl_tick = 0
        self.state.tl_arm_tick = 0
        self.state.merge_band_high = None
        self.state.merge_band_low = None
        self.state.merge_armed = False
        self.state.peak_gap_seen = 0.0
        self.state.expansion_leader = None
        self.state.rail_confirm_side = None
        self.state.rail_confirm_count = 0
        self.state.rail_winner_px = []
        self.state.reverse_confirm_side = None
        self.state.reverse_confirm_count = 0
        self.state.reverse_winner_px = []
        if release:
            release(self.algo_id)

    def _sync_flat_if_no_opens(self, release: Callable[[str], None] | None = None) -> bool:
        """If phase looks in-trade but store has no OPEN algo2 legs, reset (stale runtime)."""
        if self.state.phase == "SCAN" and not self.state.ce_trade_id and not self.state.pe_trade_id:
            return False
        open_ids = {t["id"] for t in trade_store.open_trades() if t.get("algo") == self.algo_id}
        still = [
            tid
            for tid in (
                self.state.ce_trade_id,
                self.state.pe_trade_id,
                self.state.breakout_trade_id,
                self.state.fallen_trade_id,
            )
            if tid and tid in open_ids
        ]
        if still:
            return False
        self._clear(release)
        self.state.last_message = "cleared stale dual state — no open legs"
        return True

    def snapshot(self, quotes: dict[str, float]) -> dict[str, Any]:
        pair = self._normalize_pair(self.state.pair)
        ce = pe = None
        if pair:
            ce_sym = self._pair_leg(pair, "CE").get("tradingsymbol")
            pe_sym = self._pair_leg(pair, "PE").get("tradingsymbol")
            if ce_sym:
                ce = {"symbol": ce_sym, "ltp": quotes.get(ce_sym)}
            if pe_sym:
                pe = {"symbol": pe_sym, "ltp": quotes.get(pe_sym)}
        ce_entry = float(self.state.ce_entry) if self.state.ce_entry is not None else None
        pe_entry = float(self.state.pe_entry) if self.state.pe_entry is not None else None
        if ce is not None and ce_entry is not None:
            ce = {**ce, "entry": ce_entry}
        if pe is not None and pe_entry is not None:
            pe = {**pe, "entry": pe_entry}
        return {
            "algo": self.algo_id,
            "name": self.display_name,
            "phase": self.state.phase,
            "message": self.state.last_message,
            "breakout_side": self.state.breakout_side,
            "fallen_side": self.state.fallen_side,
            "lean_side": self.state.lean_side,
            "lean_signal": self.state.lean_signal,
            "notification": self.state.notification,
            "ce": ce,
            "pe": pe,
            "ce_entry": ce_entry,
            "pe_entry": pe_entry,
            "convergence_zone": self.state.conv_zone,
            "index_overlay": self.state.index_overlay,
            "index_trend_hold": self.state.index_trend_hold,
            "index_hold_side": self.state.index_hold_side,
            "pair_retrace_armed": self.state.pair_retrace_armed,
            "pair_retrace_side": self.state.pair_retrace_side,
            "pair_retrace_peak_mult": self.state.pair_retrace_peak_mult,
            "sold_winner_side": self.state.sold_winner_side,
            "rhythm": self.state.rhythm,
            "live_series": self.state.live_series,
            "fallen_rolled": self.state.fallen_rolled,
            "fallen_roll_to": self.state.fallen_roll_to,
        }

    def is_flat(self) -> bool:
        # Disk truth — never flat while algo2 OPEN rows exist
        try:
            from foxtrade.position_integrity import open_for_algo

            if open_for_algo(self.algo_id):
                return False
        except Exception:
            pass
        if self.state.phase != "SCAN":
            return False
        if self.state.ce_trade_id or self.state.pe_trade_id:
            return False
        if self.state.ce_symbol or self.state.pe_symbol:
            return False
        return True

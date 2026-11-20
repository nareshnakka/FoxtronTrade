from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from foxtrade import broker
from foxtrade import trade_store
from foxtrade.learner import apply_learned_weights, build_entry_features
from foxtrade.convergence_zone import detect_convergence_zone, in_convergence_zone
from foxtrade.pair_regime import manage_long_decision
from foxtrade.scanner import expiry_allowed_for_entry, is_converged
from foxtrade.session_trend import (
    apply_trend_to_scores,
    compute_session_trend,
    counter_trend_blocked,
)
from foxtrade.expert_filters import (
    apply_entry_hold_gate,
    detect_opening_shakeout,
    evaluate_entry,
    size_lots,
    stop_price,
    target_price,
    daily_loss_halted,
    manage_percent_exit,
    monthly_target_reached,
    period_loss_halted,
)
from foxtrade.market_direction import analyze_market, pick_leg_for_side
from foxtrade.impulse import detect_impulse, impulse_exit_overrides
from foxtrade.daily_soft import (
    merge_soft_cfg,
    soft_enabled,
    soft_exit_overlay,
    soft_lots,
    tag_soft_event,
)
from foxtrade.micro_scalp import (
    detect_pullback_resume,
    micro_enabled,
    micro_exit_overlay,
    micro_lots,
    spot_bank_hit as micro_spot_bank_hit,
)
from foxtrade.daily_pa import (
    daily_pa_enabled,
    daily_pa_exit_overlay,
    daily_pa_lots,
    detect_daily_pa,
    spot_bank_hit as pa_spot_bank_hit,
)
from foxtrade.trend_follow import (
    detect_trend_follow,
    trend_follow_enabled,
    trend_follow_should_exit_open,
)
from foxtrade.balanced import (
    hedge_enabled,
    opposite_side,
    split_hedge_lots,
)
from foxtrade import position_integrity as pos_integrity
from foxtrade import trade_store as _trade_store

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Algo1State:
    phase: str = "SCAN"
    pair: dict[str, Any] | None = None
    predicted_side: str | None = None
    confidence: float = 0.0
    reason: str = ""
    notified: bool = False
    notification: dict[str, Any] | None = None
    open_trade_id: str | None = None
    open_symbol: str | None = None
    entry_price: float | None = None
    hedge_trade_id: str | None = None
    hedge_symbol: str | None = None
    setup_id: str | None = None
    ce_hist: list[float] = field(default_factory=list)
    pe_hist: list[float] = field(default_factory=list)
    last_message: str = ""
    conv_zone: dict[str, Any] | None = None
    # Minute-bucket persist (mirrors causal backtest bar streaks)
    signal_persist_side: str | None = None
    signal_persist_n: int = 0
    signal_persist_minute: str | None = None
    pd_persist_side: str | None = None
    pd_persist_n: int = 0
    pd_persist_minute: str | None = None
    trend_bias: str | None = None


def _clock_minute() -> str:
    return datetime.now(IST).strftime("%H:%M")


def _bump_minute_persist(
    *,
    side: str | None,
    cur_side: str | None,
    cur_n: int,
    cur_minute: str | None,
) -> tuple[str | None, int, str]:
    """Count at most once per HH:MM — live ticks are sub-minute."""
    minute = _clock_minute()
    if not side:
        return None, 0, minute
    if side != cur_side:
        return side, 1, minute
    if minute != cur_minute:
        return side, cur_n + 1, minute
    return side, cur_n, cur_minute or minute


def _reset_signal_persist(state: Algo1State) -> None:
    state.signal_persist_side = None
    state.signal_persist_n = 0
    state.signal_persist_minute = None


def _reset_pd_persist(state: Algo1State) -> None:
    state.pd_persist_side = None
    state.pd_persist_n = 0
    state.pd_persist_minute = None


class Algo1Breakout:
    """Directional one-side trader — market analysis picks CE or PE (no convergence)."""

    def __init__(self, algo_id: str = "algo1", underlying: str = "NIFTY") -> None:
        self.algo_id = str(algo_id or "algo1")
        self.underlying = str(underlying or "NIFTY").upper()
        self.state = Algo1State()

    def restore_from_store(self, runtime: dict[str, Any] | None = None) -> bool:
        """Re-attach OPEN algo1 trade(s) after process restart — trades.json is truth."""
        return self.ensure_bound_from_store(runtime=runtime, reason="restart restore")

    def ensure_bound_from_store(
        self,
        *,
        runtime: dict[str, Any] | None = None,
        reason: str = "rebind",
    ) -> bool:
        """
        Every tick / restart: bind memory to OPEN rows.
        - If OPEN exists → IN_TRADE (never pretend flat / never open fresh).
        - If memory points at a closed id → clear memory only (row already CLOSED).
        Never deletes OPEN rows.
        """
        opens = pos_integrity.open_for_algo(self.algo_id)
        if not opens:
            # Always clear ghost IN_TRADE (closed rows / stale runtime_state)
            if self.state.open_trade_id or self.state.phase == "IN_TRADE":
                self.state.open_trade_id = None
                self.state.open_symbol = None
                self.state.entry_price = None
                self.state.phase = "SCAN"
                self.state.setup_id = None
                self.state.last_message = "flat — no OPEN in store"
            return False
        primary = pos_integrity.bind_primary(opens)
        assert primary is not None
        saved = (runtime or {}).get(self.algo_id) or {}
        changed = self.state.open_trade_id != primary["id"]
        self.state.open_trade_id = primary["id"]
        self.state.open_symbol = primary.get("symbol")
        self.state.entry_price = float(primary.get("entry_price") or 0)
        self.state.setup_id = primary.get("setup_id") or saved.get("setup_id")
        self.state.phase = "IN_TRADE"
        n = len(opens)
        if changed or reason == "restart restore":
            self.state.last_message = (
                f"{reason}: {self.state.open_symbol} @ {self.state.entry_price}"
                + (f" (+{n - 1} more OPEN)" if n > 1 else "")
            )
        return True

    def runtime_snapshot(self) -> dict[str, Any]:
        opens = pos_integrity.open_for_algo(self.algo_id)
        return {
            "phase": self.state.phase,
            "open_trade_id": self.state.open_trade_id,
            "open_symbol": self.state.open_symbol,
            "entry_price": self.state.entry_price,
            "setup_id": self.state.setup_id,
            "open_count": len(opens),
            "open_ids": [t.get("id") for t in opens],
        }

    def on_tick(self, ctx: dict[str, Any]) -> None:
        cfg = apply_learned_weights(ctx["config"])
        if not cfg.get("algo1_enabled", True) and not self.state.open_trade_id:
            self.state.last_message = "disabled"
            return

        pair = ctx.get("pair")
        quotes = ctx.get("quotes") or {}
        index = ctx.get("index") or {}
        macro = ctx.get("macro") or {}
        in_session = ctx.get("in_session", False)
        mode = "live" if cfg.get("live_trading") else "paper"
        try_acquire: Callable[[str], bool] = ctx["try_acquire"]
        release: Callable[[str], None] = ctx["release"]
        decay_block = bool(ctx.get("decay_block"))
        pair_regime = ctx.get("pair_regime")
        # Spot for micro scalp bank (also from market path)
        spot_now = index.get("spot")
        if spot_now is None:
            mkt = ctx.get("market")
            if mkt is not None and hasattr(mkt, "spot_values"):
                vals = list(mkt.spot_values())
                if vals:
                    spot_now = vals[-1]
        self._tick_spot = float(spot_now) if spot_now is not None else None
        self._tick_spot_closes = list(
            (ctx.get("index") or {}).get("spot_closes") or []
        )

        # Keep CE/PE history + overlap zone even while managing an open trade
        if pair:
            ce_sym = pair["ce"]["tradingsymbol"]
            pe_sym = pair["pe"]["tradingsymbol"]
            ce_ltp = quotes.get(ce_sym)
            pe_ltp = quotes.get(pe_sym)
            if ce_ltp is not None and pe_ltp is not None:
                self.state.pair = pair
                self.state.ce_hist.append(ce_ltp)
                self.state.pe_hist.append(pe_ltp)
                hist_n = int(cfg.get("history_seconds", 500))
                self.state.ce_hist = self.state.ce_hist[-hist_n:]
                self.state.pe_hist = self.state.pe_hist[-hist_n:]
                zone_gap = float(cfg.get("convergence_zone_gap", 10.0))
                zone_min = int(cfg.get("convergence_zone_min_samples", 25))
                self.state.conv_zone = detect_convergence_zone(
                    self.state.ce_hist, self.state.pe_hist, gap_tol=zone_gap, min_samples=zone_min
                )

        # Source of truth = trades.json OPEN rows (survives restart; no memory cheat)
        self.ensure_bound_from_store(reason="tick rebind")
        opens = pos_integrity.open_for_algo(self.algo_id)
        if opens:
            # Manage EVERY open leg — never abandon extras / never open fresh on top
            managed_any = False
            for t in opens:
                tid = t.get("id")
                sym = t.get("symbol")
                if not tid or not sym:
                    continue
                # Point state at this leg for manage helpers
                self.state.open_trade_id = tid
                self.state.open_symbol = sym
                self.state.entry_price = float(t.get("entry_price") or 0)
                self.state.phase = "IN_TRADE"
                px = quotes.get(sym)
                fresh_fn = getattr(ctx.get("market"), "quote_fresh", None)
                if px is not None and callable(fresh_fn) and not fresh_fn(sym):
                    px = None
                if px is None:
                    # The 1s poller failed or dropped this leg: fetch a direct
                    # LTP so stops/targets keep being enforced on the position.
                    px = self._rest_ltp(sym, t)
                if px is not None:
                    self._manage_open(cfg, px, release, pair_regime)
                    managed_any = True
                else:
                    self.state.last_message = (
                        f"OPEN {sym} — no fresh quote (management stalled)"
                    )
            # Re-bind primary after possible closes
            self.ensure_bound_from_store(reason="post-manage")
            if pos_integrity.open_for_algo(self.algo_id):
                if not managed_any:
                    self.state.last_message = (
                        f"holding {len(pos_integrity.open_for_algo('algo1'))} OPEN — "
                        "no fresh entry until squared"
                    )
                return
            # All squared this tick — fall through only if truly flat
            release(self.algo_id)

        # Hard ban: any OPEN anywhere (if configured) already checked via store above
        ok_new, why = pos_integrity.can_open_new(self.algo_id, cfg=cfg)
        if not ok_new:
            self.state.last_message = why
            self.state.phase = "HOLD_OPEN"
            return

        if not in_session:
            start = cfg.get("session_start", "09:15")
            end = cfg.get("session_end", "13:30")
            self.state.last_message = f"outside {start}–{end} window"
            return

        # Directional mode: ignore decay/convergence — market analysis only
        directional = bool(cfg.get("directional_mode", True))
        if not directional and decay_block:
            self.state.last_message = f"decay guard: {ctx.get('decay_msg', 'blocked')}"
            return

        # Spot path early — needed for impulse cooldown override + Sensex Thu gamma
        spot_closes_early: list[float] = list((ctx.get("index") or {}).get("spot_closes") or [])
        if not spot_closes_early:
            mkt = ctx.get("market")
            if mkt is not None and hasattr(mkt, "spot_values"):
                spot_closes_early = list(mkt.spot_values())

        # Sensex: Tue/Wed always; Thu only if gamma blast; Mon = pre-expiry cutoff
        if self.underlying == "SENSEX":
            try:
                from foxtrade.sensex_calendar import (
                    is_sensex_trade_day,
                    sensex_day_role,
                )

                if not is_sensex_trade_day(cfg=cfg, closes=spot_closes_early):
                    self.state.last_message = (
                        f"sensex OFF ({sensex_day_role(cfg=cfg, closes=spot_closes_early)}) "
                        "— capital on Nifty"
                    )
                    return
            except Exception:
                pass

        cd_min = int(cfg.get("entry_cooldown_minutes", 8))
        cd_sec = cd_min * 60
        # Impulse / pre-event: re-enter in seconds, not minutes
        try:
            _imp_cd = detect_impulse(
                spot_closes_early, cfg=cfg, macro=ctx.get("macro") or {}
            )
            if _imp_cd.get("allow_entry"):
                cd_sec = int(cfg.get("impulse_cooldown_seconds", 25))
        except Exception:
            pass
        if cd_sec > 0:
            since = trade_store.seconds_since_last_close(self.algo_id, mode)
            if since is not None and since < cd_sec:
                left = int(cd_sec - since)
                self.state.last_message = f"cool-down {left}s left"
                return

        day_setups = trade_store.count_day_setups(None, mode)
        max_day = int(cfg.get("max_trades_per_day", 15))
        # Allow a few extra impulse/pre-event scalps beyond the slow ORB cap
        try:
            _imp_cap = detect_impulse(
                spot_closes_early, cfg=cfg, macro=ctx.get("macro") or {}
            )
            if _imp_cap.get("allow_entry"):
                max_day = max_day + int(cfg.get("impulse_extra_trades", 2))
        except Exception:
            pass
        if day_setups >= max_day:
            self.state.last_message = f"daily setup cap {max_day} reached"
            return

        mode_tag = "live" if cfg.get("live_trading") else "paper"
        day_stats = (
            _trade_store.day_pnl_stats(self.algo_id, mode_tag)
            if hasattr(_trade_store, "day_pnl_stats")
            else {}
        )
        if daily_loss_halted(float(day_stats.get("pnl") or 0), cfg):
            self.state.last_message = "daily loss circuit open"
            return
        # Scalp (PA/micro/soft) losses do not trip the primary day halt
        halt_losses = int(
            day_stats.get("primary_losses")
            if day_stats.get("primary_losses") is not None
            else day_stats.get("losses")
            or 0
        )
        if halt_losses >= int(cfg.get("max_losses_before_halt", 1)):
            self.state.last_message = (
                f"loss halt {halt_losses}/{cfg.get('max_losses_before_halt', 1)}"
            )
            return
        # Week / month capital protection (income mode)
        today = datetime.now(IST).date()
        week_start = (today.fromordinal(today.toordinal() - today.weekday())).isoformat()
        month_start = today.replace(day=1).isoformat()
        week_pnl = _trade_store.period_pnl(self.algo_id, mode_tag, since_date=week_start)
        month_pnl = _trade_store.period_pnl(self.algo_id, mode_tag, since_date=month_start)
        if period_loss_halted(week_pnl, cfg, pct_key="max_weekly_loss_pct"):
            self.state.last_message = (
                f"weekly loss circuit open (pnl ₹{week_pnl:,.0f})"
            )
            return
        if period_loss_halted(month_pnl, cfg, pct_key="max_monthly_loss_pct"):
            self.state.last_message = (
                f"monthly loss circuit open (pnl ₹{month_pnl:,.0f})"
            )
            return
        if monthly_target_reached(month_pnl, cfg):
            self.state.last_message = (
                f"month target hit ₹{month_pnl:,.0f} — banking rest of month"
            )
            return

        low = float(cfg.get("premium_low", 125))
        high = float(cfg.get("premium_high", 145))
        scan = ctx.get("scan") or {}
        spot_closes = list(index.get("spot_closes") or [])
        if not spot_closes:
            mkt = ctx.get("market")
            if mkt is not None and hasattr(mkt, "spot_values"):
                spot_closes = list(mkt.spot_values())
            elif index.get("spot") is not None:
                spot_closes = [float(index["spot"])]
        self._tick_spot_closes = list(spot_closes)

        # ——— DIRECTIONAL (default): one side from live market analysis ———
        if directional:
            is_exp = bool(scan.get("expiry_day") or scan.get("is_expiry_day"))
            if self.underlying == "SENSEX":
                try:
                    from foxtrade.sensex_calendar import (
                        is_sensex_expiry_day,
                        is_sensex_gamma_conditional_day,
                    )
                    from foxtrade.sensex_gamma import detect_sensex_gamma_blast

                    is_exp = is_sensex_expiry_day(cfg=cfg)
                    if is_sensex_gamma_conditional_day(cfg=cfg):
                        blast = detect_sensex_gamma_blast(spot_closes, cfg=cfg)
                        if blast.get("allow"):
                            is_exp = True
                except Exception:
                    pass
            if is_exp:
                # Expiry / Thursday gamma playbook: fast exits + decay-wait entry
                cfg = {
                    **cfg,
                    "treat_as_expiry": True,
                    "is_expiry_session": True,
                }
            analysis = analyze_market(
                spot_closes,
                cfg=cfg,
                ema9=index.get("ema9"),
                ema21=index.get("ema21"),
                is_expiry_day=is_exp,
            )
            side = analysis.get("side")
            conf = float(analysis.get("confidence") or 0)
            reason = str(analysis.get("reason") or "")
            allow = bool(analysis.get("allow_entry"))
            event = str(analysis.get("event") or "")
            try:
                tb = ((analysis.get("trend") or {}).get("bias") or "").upper()
                if tb in ("UP", "DOWN", "FLAT"):
                    self.state.trend_bias = tb
            except Exception:
                pass

            # Live macro / FII-OI / news overlay
            try:
                adj = float(macro.get("confidence_adj") or 0)
                conf = max(0.0, min(0.99, conf + adj))
                hb = str(macro.get("headline_bias") or "").lower()
                sm = str(macro.get("smart_money") or "").lower()
                hint = str(macro.get("side_hint") or "").upper()
                fii_reg = str(macro.get("fii_oi_regime") or "").lower()
                block_ce = bool(macro.get("block_ce"))
                caution_ce = bool(macro.get("caution_ce"))
                # OI/FII gates — post_decay crush bounce exempt (Oct22 +19k).
                # short_buildup alone → conf haircut; hard block only when
                # scanner says block_ce (sustained / fresh FII short) or unwind.
                _pd_peek = ctx.get("post_decay") or {}
                is_post = str(_pd_peek.get("action") or "").upper() in (
                    "ENTER_CE",
                    "ENTER_PE",
                    "WAIT",
                ) or "post_decay" in (event or "").lower()
                soft_short = (
                    sm == "short_pressure" or fii_reg == "short_buildup"
                )
                hard_block = (
                    block_ce
                    or fii_reg == "long_unwinding"
                    or sm == "long_unwinding"
                    or (hint == "PE" and hb == "risk_off")
                )
                if side == "CE" and hard_block and not is_post:
                    allow = False
                    reason = (
                        f"{reason} | FII/OI block CE "
                        f"({fii_reg or sm or hb})"
                    )
                elif side == "CE" and soft_short and not is_post and not hard_block:
                    conf = max(0.0, conf - 0.06)
                    reason = f"{reason} | OI {fii_reg or sm} CE conf-haircut"
                elif side == "CE" and is_post and (soft_short or hard_block):
                    reason = f"{reason} | OI {fii_reg or sm} ignored for post_decay"
                elif side == "CE" and caution_ce and sm == "short_covering":
                    if conf < float(cfg.get("fii_oi_covering_ce_min_conf", 0.82)):
                        allow = False
                        reason = f"{reason} | short_covering CE needs conf≥0.82"
                    else:
                        reason = f"{reason} | short_covering CE caution pass"
                elif hint in ("CE", "PE") and allow and side == hint:
                    conf = min(0.99, conf + 0.03)
                    reason = f"{reason} | macro aligns {hint}"
                if sm == "long_unwinding" and side == "CE" and not is_post:
                    allow = False
                    reason = f"{reason} | OI long_unwinding block CE"
                if macro.get("notes"):
                    reason = f"{reason} | {', '.join(str(n) for n in (macro.get('notes') or [])[:2])}"
            except Exception:
                pass

            # Post-decay FIRST (matches profitable causal backtest).
            # Impulse/pre-event must NOT steal slots while decay is WAIT/ENTER.
            impulse_mode: str | None = None
            is_post_decay = False
            pd = ctx.get("post_decay") or {}
            pd_action = str(pd.get("action") or "NONE")
            pd_side = pd.get("side")
            pd_conf = float(pd.get("confidence") or 0)
            pd_est = float(pd.get("expected_profit_pct") or 0)
            pd_min = float(cfg.get("post_decay_min_confidence", 0.62))
            pd_enabled = bool(cfg.get("post_decay_entry_enabled", True))
            pd_enter_ready = (
                pd_enabled
                and pd_action in ("ENTER_CE", "ENTER_PE")
                and pd_side in ("CE", "PE")
                and pd_conf >= pd_min
            )
            pd_waiting = pd_enabled and pd_action == "WAIT"

            # Bank after first primary win — micro/soft scalps must not lock the day
            day_pnl = float(day_stats.get("realized_pnl") or day_stats.get("pnl") or 0)
            primary_wins = int(
                day_stats.get("primary_wins")
                if day_stats.get("primary_wins") is not None
                else day_stats.get("wins")
                or 0
            )
            if (
                cfg.get("bank_after_win", True)
                and day_pnl > 0
                and primary_wins >= 1
            ):
                br_str = float((analysis.get("break") or {}).get("strength") or 0)
                if cfg.get("realtime_parity", True) or br_str < 0.7:
                    self.state.last_message = "banked — day green"
                    _reset_signal_persist(self.state)
                    _reset_pd_persist(self.state)
                    return

            if pd_waiting:
                if not side or side == pd_side:
                    self.state.predicted_side = pd_side
                    self.state.confidence = pd_conf
                    self.state.reason = str(pd.get("reason") or "wait decay")
                    self.state.phase = "WAIT_DECAY"
                    self.state.last_message = f"post-decay WAIT: {pd.get('reason')}"
                    _reset_pd_persist(self.state)
                    _reset_signal_persist(self.state)
                    return

            # Expiry crush knife filter (Sep29 vs Oct13)
            expiry_min_est = float(cfg.get("post_decay_expiry_min_est_pct", 300))
            if (
                is_exp
                and pd_enter_ready
                and pd_est < expiry_min_est
            ):
                self.state.predicted_side = pd_side
                self.state.confidence = pd_conf
                self.state.phase = "WAIT_DECAY"
                self.state.last_message = (
                    f"post-decay expiry est +{pd_est:.0f}% < {expiry_min_est:.0f}%"
                )
                _reset_pd_persist(self.state)
                _reset_signal_persist(self.state)
                return

            if pd_enter_ready:
                bars_n = len(spot_closes)
                entry_delay = int(cfg.get("entry_delay_minutes", 12))
                prefer_after = int(cfg.get("prefer_entry_after_minutes", entry_delay))
                if cfg.get("post_decay_respect_session_delay", True):
                    pd_ready = max(
                        entry_delay,
                        prefer_after,
                        int(cfg.get("post_decay_min_bar", 20)),
                    )
                else:
                    pd_ready = int(cfg.get("post_decay_min_bar", 0))
                if bars_n < pd_ready:
                    self.state.predicted_side = pd_side
                    self.state.confidence = pd_conf
                    self.state.phase = "WAIT_DECAY"
                    self.state.last_message = (
                        f"post-decay armed {pd_side} — wait bar {bars_n}/{pd_ready}"
                    )
                    return
                # Lesson 2026-11-02: never force PE into UP / CE into DOWN sessions
                spot_blocked = False
                block_why = ""
                if cfg.get("post_decay_require_spot_align", True) and spot_closes:
                    align_pts = float(
                        cfg.get("post_decay_spot_align_pts")
                        or cfg.get("trend_soft_pts")
                        or 35.0
                    )
                    open_px = float(spot_closes[0])
                    last_px = float(spot_closes[-1])
                    vs_open = last_px - open_px
                    up_extent = max(spot_closes) - open_px
                    dn_extent = open_px - min(spot_closes)
                    if pd_side == "PE" and (
                        vs_open >= align_pts or (up_extent >= align_pts and vs_open > 0)
                    ):
                        spot_blocked = True
                        block_why = (
                            f"UP session (vs {vs_open:+.0f}, high {up_extent:+.0f}) "
                            f"blocks post-decay PE"
                        )
                    elif pd_side == "CE" and (
                        vs_open <= -align_pts
                        or (dn_extent >= align_pts and vs_open < 0)
                    ):
                        spot_blocked = True
                        block_why = (
                            f"DOWN session (vs {vs_open:+.0f}, low {-dn_extent:.0f}) "
                            f"blocks post-decay CE"
                        )
                if spot_blocked:
                    self.state.predicted_side = pd_side
                    self.state.confidence = pd_conf
                    self.state.phase = "WAIT_DECAY"
                    self.state.reason = block_why
                    self.state.last_message = f"post-decay WAIT: {block_why}"
                    _reset_pd_persist(self.state)
                    return

                # Confirm streak (minute buckets) — same as simulate_day_detailed
                pd_need = int(cfg.get("post_decay_confirm_minutes", 0))
                if is_exp:
                    pd_need = max(
                        pd_need,
                        int(cfg.get("post_decay_expiry_confirm_minutes", 3)),
                    )
                fast_est = float(cfg.get("post_decay_fast_confirm_est_pct", 400))
                if pd_est >= fast_est:
                    pd_need = min(pd_need, 1) if pd_need > 0 else 0
                # Lesson 2026-10-26: weak est needs longer confirm
                strong_est = float(cfg.get("post_decay_strong_est_pct", 200))
                if 0 < pd_est < strong_est:
                    pd_need = max(
                        pd_need,
                        int(cfg.get("post_decay_weak_est_confirm_minutes", 5)),
                    )
                if cfg.get("post_decay_respect_session_delay", True) and pd_need <= 0:
                    pd_need = 3
                (
                    self.state.pd_persist_side,
                    self.state.pd_persist_n,
                    self.state.pd_persist_minute,
                ) = _bump_minute_persist(
                    side=str(pd_side),
                    cur_side=self.state.pd_persist_side,
                    cur_n=self.state.pd_persist_n,
                    cur_minute=self.state.pd_persist_minute,
                )
                if pd_need > 0 and self.state.pd_persist_n < pd_need:
                    self.state.predicted_side = pd_side
                    self.state.confidence = pd_conf
                    self.state.phase = "WAIT_DECAY"
                    self.state.last_message = (
                        f"post-decay confirm "
                        f"{self.state.pd_persist_n}/{pd_need}m {pd_side}"
                    )
                    return

                # Own the entry — ignore ORB/impulse conflict (spot already aligned)
                if allow and side == pd_side:
                    conf = max(conf, pd_conf)
                    reason = f"{reason} | +post-decay {pd.get('setup')}"
                    event = f"{event}+post_decay"
                    is_post_decay = True
                elif allow and side and side != pd_side and cfg.get(
                    "post_decay_prefer_over_mkt", True
                ):
                    side = str(pd_side)
                    conf = pd_conf
                    reason = (
                        f"post-decay override {pd.get('setup')}: {pd.get('reason')}"
                    )
                    event = f"post_decay_{pd.get('setup')}"
                    is_post_decay = True
                    allow = True
                elif (not allow or side not in ("CE", "PE")) and cfg.get(
                    "post_decay_standalone_entry", True
                ):
                    side = str(pd_side)
                    conf = pd_conf
                    reason = f"post-decay {pd.get('setup')}: {pd.get('reason')}"
                    event = f"post_decay_{pd.get('setup')}"
                    allow = True
                    is_post_decay = True
            else:
                _reset_pd_persist(self.state)
                # Secondary: sudden spike/fall + pre-event (only if decay idle)
                try:
                    imp = detect_impulse(spot_closes, cfg=cfg, macro=macro)
                    if imp.get("allow_entry") and imp.get("side") in ("CE", "PE"):
                        side = str(imp["side"])
                        conf = max(conf, float(imp.get("confidence") or 0))
                        reason = str(imp.get("reason") or "impulse")
                        event = str(imp.get("event") or "IMPULSE")
                        allow = True
                        impulse_mode = str(imp.get("mode") or "impulse")
                except Exception:
                    impulse_mode = None

            # Optional: only crush→bounce (blockbuster) path
            is_post_decay_ev = is_post_decay or str(event).startswith(
                "post_decay"
            ) or "+post_decay" in str(event)
            if (
                allow
                and cfg.get("post_decay_only_entries")
                and not is_post_decay_ev
                and not str(reason).startswith("post-decay")
            ):
                allow = False
                self.state.last_message = "post-decay-only: skip directional"
                _reset_signal_persist(self.state)
                return

            is_soft = False
            is_micro = False
            is_pa = False
            is_trend_follow = False
            day_flat = int(day_setups or 0) == 0
            bars_n_soft = len(spot_closes)

            # Trend Follow — always ride breakout/breakdown with the session trend
            # (and fire on confirmed trend change). Fills when primary path is idle.
            if (
                (not allow or side not in ("CE", "PE"))
                and trend_follow_enabled(cfg)
                and not is_post_decay
                and not impulse_mode
                and not pd_waiting
            ):
                tf = detect_trend_follow(
                    spot_closes,
                    cfg,
                    ema9=index.get("ema9"),
                    ema21=index.get("ema21"),
                    prior_bias=self.state.trend_bias,
                )
                if tf.get("bias") in ("UP", "DOWN", "FLAT"):
                    self.state.trend_bias = str(tf.get("bias") or self.state.trend_bias)
                if bool(tf.get("allow_entry")) and tf.get("side") in ("CE", "PE"):
                    allow = True
                    is_trend_follow = True
                    side = str(tf["side"])
                    conf = float(tf.get("confidence") or 0)
                    reason = str(tf.get("reason") or "trend_follow")
                    event = str(tf.get("event") or "TREND_FOLLOW")
                    analysis = tf

            # Micro 10–15pt pullback-resume (morning window, before soft PE)
            if (
                (not allow or side not in ("CE", "PE"))
                and micro_enabled(cfg)
                and (not cfg.get("micro_scalp_only_if_flat", True) or day_flat)
                and not is_post_decay
                and not impulse_mode
                and not pd_waiting
            ):
                ms = detect_pullback_resume(spot_closes, cfg)
                if bool(ms.get("allow_entry")) and ms.get("side") in ("CE", "PE"):
                    allow = True
                    is_micro = True
                    side = str(ms["side"])
                    conf = float(ms.get("confidence") or 0)
                    reason = str(ms.get("reason") or "micro scalp")
                    event = str(ms.get("event") or "MICRO_PB_RESUME")
                    analysis = ms

            # Daily soft PE scalp when primary path is flat (chop / vs-open gates).
            # Delay past morning so primary SR/ORB keeps first claim.
            soft_after = int(cfg.get("daily_soft_entry_after_minutes", 150))
            if (
                (not allow or side not in ("CE", "PE"))
                and soft_enabled(cfg)
                and bars_n_soft >= soft_after
                and not is_post_decay
                and not impulse_mode
                and not pd_waiting
            ):
                soft_ok = True
                if cfg.get("daily_soft_only_if_flat", True):
                    soft_ok = day_flat
                if soft_ok:
                    scfg = merge_soft_cfg(cfg)
                    soft_a = analyze_market(
                        spot_closes,
                        cfg=scfg,
                        ema9=index.get("ema9"),
                        ema21=index.get("ema21"),
                        is_expiry_day=is_exp,
                    )
                    if (
                        bool(soft_a.get("allow_entry"))
                        and soft_a.get("side") == "PE"
                    ):
                        allow = True
                        is_soft = True
                        side = "PE"
                        conf = float(soft_a.get("confidence") or 0)
                        reason = f"daily_soft: {soft_a.get('reason') or ''}"
                        event = tag_soft_event(str(soft_a.get("event") or "PE"))
                        analysis = soft_a

            # Daily PA / momentum / local S-R — try most flat sessions
            if (
                (not allow or side not in ("CE", "PE"))
                and daily_pa_enabled(cfg)
                and (not cfg.get("daily_pa_only_if_flat", True) or day_flat)
                and not is_post_decay
                and not impulse_mode
                and not pd_waiting
            ):
                pa = detect_daily_pa(spot_closes, cfg)
                if bool(pa.get("allow_entry")) and pa.get("side") in ("CE", "PE"):
                    allow = True
                    is_pa = True
                    side = str(pa["side"])
                    conf = float(pa.get("confidence") or 0)
                    reason = str(pa.get("reason") or "daily_pa")
                    event = str(pa.get("event") or "DAILY_PA")
                    analysis = pa

            self.state.predicted_side = side
            self.state.confidence = conf
            self.state.reason = reason
            self.state.phase = "WATCH" if side else "SCAN"
            self.state.last_message = (
                f"mkt {event}: {side or 'FLAT'} "
                f"conf {conf:.0%} | {reason}"
            )
            if not allow or side not in ("CE", "PE"):
                _reset_signal_persist(self.state)
                return

            # Signal persist N minutes (anti fake-break) — skip for post-decay
            persist_need = int(cfg.get("signal_persist_minutes", 0))
            if is_trend_follow:
                persist_need = int(cfg.get("trend_follow_persist_minutes", 1))
            elif is_pa:
                persist_need = int(cfg.get("daily_pa_persist_minutes", 1))
            elif is_micro:
                persist_need = int(cfg.get("micro_scalp_confirm_bars", 1))
            elif is_soft:
                persist_need = int(cfg.get("daily_soft_persist_minutes", 1))
            # CE grinds slow → confirm longer; PE dumps fast → enter quicker
            if (
                cfg.get("side_pace_rule_enabled", True)
                and side in ("CE", "PE")
                and not is_post_decay_ev
            ):
                if side == "CE":
                    persist_need = max(
                        persist_need, int(cfg.get("ce_signal_persist_minutes", 3))
                    )
                else:
                    persist_need = min(
                        persist_need if persist_need > 0 else 1,
                        int(cfg.get("pe_signal_persist_minutes", 1)),
                    )
            if is_post_decay_ev:
                persist_need = 0
            if (
                event in ("SR_RECLAIM", "SR_REJECT")
                and persist_need > 0
                and not is_soft
                and not is_micro
                and not is_pa
                and not is_trend_follow
            ):
                sr_vs = abs(float(analysis.get("vs_open") or 0))
                sr_mom = abs(float(analysis.get("momentum") or 0))
                hard_pts = float(cfg.get("trend_hard_pts", 70))
                mid_vs = float(cfg.get("sr_fast_persist_min_vs", 50))
                fast_mom = float(cfg.get("sr_fast_persist_min_mom", 18))
                fast = (sr_vs >= hard_pts and sr_mom >= fast_mom) or (
                    mid_vs <= sr_vs < hard_pts
                )
                if fast:
                    persist_need = int(cfg.get("sr_signal_persist_minutes", 1))
                else:
                    persist_need = max(persist_need, 2)
            if persist_need > 0:
                (
                    self.state.signal_persist_side,
                    self.state.signal_persist_n,
                    self.state.signal_persist_minute,
                ) = _bump_minute_persist(
                    side=str(side),
                    cur_side=self.state.signal_persist_side,
                    cur_n=self.state.signal_persist_n,
                    cur_minute=self.state.signal_persist_minute,
                )
                if self.state.signal_persist_n < persist_need:
                    self.state.phase = "WATCH"
                    self.state.last_message = (
                        f"persist {self.state.signal_persist_n}/{persist_need}m "
                        f"{side} {event}"
                    )
                    return
            else:
                _reset_signal_persist(self.state)

            # Build leg list from scanner candidates + optional pair legs
            legs: list[dict[str, Any]] = list(scan.get("candidates") or [])
            if pair:
                for k in ("ce", "pe"):
                    leg = pair.get(k)
                    if isinstance(leg, dict) and leg.get("tradingsymbol"):
                        row = dict(leg)
                        row["ltp"] = quotes.get(row["tradingsymbol"], row.get("ltp"))
                        legs.append(row)
            # Attach live LTPs
            for leg in legs:
                sym = leg.get("tradingsymbol")
                if sym and sym in quotes:
                    leg["ltp"] = quotes[sym]

            lot_size = int(cfg.get("lot_size", 75))
            budget_inr = float(cfg.get("capital_inr") or 0)
            max_premium = None
            if budget_inr > 0 and lot_size > 0:
                max_premium = (budget_inr * 0.98) / (
                    lot_size * max(1, int(cfg.get("min_lots_per_trade", 1) or 1))
                )
            strike_mode = str(cfg.get("strike_select_mode") or "atm").lower()
            pick = pick_leg_for_side(
                legs,
                side,
                premium_low=low,
                premium_high=high,
                atm=(
                    float(scan.get("atm") or 0) or None
                    if strike_mode == "atm"
                    else None
                ),
                max_premium=max_premium,
            )
            if not pick:
                self.state.last_message = f"no {side} leg in premium band {low}-{high}"
                return
            symbol = str(pick["tradingsymbol"])
            entry = float(pick.get("ltp") or 0)
            if entry <= 0:
                self.state.last_message = f"waiting quote {symbol}"
                return
            pair_exp = pick.get("expiry") or scan.get("current_expiry")
            trade_week = str(scan.get("current_expiry") or "")[:10]
            if self.underlying == "NIFTY":
                if not expiry_allowed_for_entry(pair_exp, symbol=symbol):
                    self.state.last_message = f"skip wrong/0DTE week {symbol} ({pair_exp})"
                    return
            elif trade_week and str(pair_exp or "")[:10] != trade_week:
                self.state.last_message = f"skip wrong week {pair_exp} — need {trade_week}"
                return
            if trade_week and str(pair_exp or "")[:10] != trade_week:
                self.state.last_message = f"skip wrong week {pair_exp} — need {trade_week}"
                return
            if not try_acquire(self.algo_id):
                self.state.last_message = "slot locked by other algo"
                return

            fake_pair = {
                "expiry": pair_exp,
                "strike": pick.get("strike"),
                "ce": pick if side == "CE" else (pair or {}).get("ce") or pick,
                "pe": pick if side == "PE" else (pair or {}).get("pe") or pick,
            }
            # Dual-book may inject capital_inr / lots_override for this underlying
            if cfg.get("lots_override") is not None:
                lots = int(cfg["lots_override"])
            else:
                lots = size_lots(cfg, entry=entry, algo=self.algo_id)
            if int(day_stats.get("losses") or 0) >= 2:
                lots = max(1, lots // 2)
            # Impulse: optional size bump (capped) for sharp moves
            if impulse_mode == "impulse" and cfg.get("impulse_lot_boost", False):
                lots = min(int(cfg.get("max_lots_per_trade", 6)), lots + 1)
            if is_pa:
                lots = daily_pa_lots(cfg, lots)
            elif is_micro:
                lots = micro_lots(cfg, lots)
            elif is_soft:
                lots = soft_lots(cfg, lots)
            # Final affordability check at the actual chosen premium. Estimated
            # lots from a scan price can exceed cash and get rejected by Kite.
            if budget_inr > 0 and entry > 0 and lot_size > 0:
                affordable = int((budget_inr * 0.98) // (entry * lot_size))
                if affordable < 1:
                    release(self.algo_id)
                    msg = (
                        f"1 lot {symbol} @ {entry:.2f} = ₹{entry*lot_size:,.0f} "
                        f"> budget ₹{budget_inr:,.0f}"
                    )
                    self.state.last_message = msg
                    self.state.notification = {
                        "ts": datetime.now(IST).isoformat(),
                        "message": msg,
                        "level": "error",
                    }
                    return
                lots = min(lots, affordable)
            # Exit profile: PA / micro / soft / impulse / primary
            if is_pa:
                exit_cfg = {**cfg, **daily_pa_exit_overlay(cfg)}
            elif is_micro:
                exit_cfg = {**cfg, **micro_exit_overlay(cfg)}
            elif is_soft:
                exit_cfg = {**cfg, **soft_exit_overlay(cfg)}
            elif impulse_mode:
                exit_cfg = impulse_exit_overrides(cfg, impulse_mode)
            else:
                exit_cfg = cfg
            if is_trend_follow:
                tag = "TREND"
            elif is_pa:
                tag = "PA"
            elif is_micro:
                tag = "MICRO"
            elif is_soft:
                tag = "SOFT"
            elif impulse_mode == "impulse":
                tag = "IMPULSE"
            elif impulse_mode == "pre_event":
                tag = "PRE"
            elif is_post_decay_ev:
                tag = "POST"
            else:
                tag = "DIR"
            est_pct = pd_est if is_post_decay_ev else 0.0
            est_tag = f" est+{est_pct:.0f}%" if est_pct > 0 else ""
            self.state.notification = {
                "ts": datetime.now(IST).isoformat(),
                "algo": self.algo_id,
                "side": side,
                "confidence": round(conf, 2),
                "reason": reason,
                "message": f"{tag} {side} {symbol} @ {entry:.1f}{est_tag} | {event} | {reason}",
            }
            ctx.setdefault("notifications", []).append(self.state.notification)
            features = build_entry_features(
                side=side,
                confidence=conf,
                reason=reason,
                index=index,
                macro=macro,
                pair_regime=None,
                gap=0.0,
            )
            features["post_decay"] = bool(is_post_decay_ev)
            features["post_decay_setup"] = pd.get("setup")
            features["expected_profit_pct"] = est_pct
            features["impulse"] = bool(impulse_mode)
            features["impulse_mode"] = impulse_mode
            features["daily_soft"] = bool(is_soft)
            features["micro_scalp"] = bool(is_micro)
            features["daily_pa"] = bool(is_pa)
            features["trend_follow"] = bool(is_trend_follow)
            features["trend_change"] = bool(
                (analysis or {}).get("trend_change")
            ) if is_trend_follow else False
            features["entry_spot"] = (
                float(spot_closes[-1]) if spot_closes else None
            )
            features["event"] = event
            features["realtime_parity"] = bool(cfg.get("realtime_parity", True))
            features["underlying"] = self.underlying
            features["algo_id"] = self.algo_id
            _reset_signal_persist(self.state)
            _reset_pd_persist(self.state)
            self._enter(
                cfg,
                mode,
                symbol,
                side,
                entry,
                fake_pair,
                release,
                features,
                lots=lots,
                stop_px=stop_price(entry, exit_cfg),
                target_px=target_price(entry, exit_cfg, trail=True),
            )
            return

        # ——— Legacy convergence path (only if directional_mode=false) ———
        if not pair:
            self.state.phase = "SCAN"
            self.state.last_message = "no CE/PE pair in band"
            return
        ce_sym = pair["ce"]["tradingsymbol"]
        pe_sym = pair["pe"]["tradingsymbol"]
        ce_ltp = quotes.get(ce_sym)
        pe_ltp = quotes.get(pe_sym)
        if ce_ltp is None or pe_ltp is None:
            self.state.last_message = "waiting quotes"
            return
        tol = float(cfg.get("convergence_tol", 3.0))
        if self.state.conv_zone:
            converged = in_convergence_zone(ce_ltp, pe_ltp, self.state.conv_zone, entry_tol=tol)
        else:
            converged = is_converged(ce_ltp, pe_ltp, tol, low, high)
        side, conf, reason = self._predict(cfg, index, macro, ce_ltp, pe_ltp, pair_regime)
        self.state.predicted_side = side
        self.state.confidence = conf
        self.state.reason = reason
        if not converged:
            self.state.phase = "SCAN"
            self.state.last_message = f"gap={abs(ce_ltp - pe_ltp):.1f} waiting converge"
            return
        self.state.phase = "WATCH"
        symbol = ce_sym if side == "CE" else pe_sym
        if not expiry_allowed_for_entry(pair.get("expiry"), symbol=symbol):
            return
        if not try_acquire(self.algo_id):
            return
        entry = ce_ltp if side == "CE" else pe_ltp
        self._enter(
            cfg, mode, symbol, side, entry, pair, release,
            build_entry_features(
                side=side, confidence=conf, reason=reason, index=index,
                macro=macro, pair_regime=pair_regime, gap=abs(ce_ltp - pe_ltp),
            ),
            lots=size_lots(cfg, entry=entry, algo=self.algo_id),
            stop_px=stop_price(entry, cfg),
            target_px=target_price(entry, cfg),
        )

    def _predict(
        self,
        cfg: dict[str, Any],
        index: dict[str, Any],
        macro: dict[str, Any],
        ce_ltp: float,
        pe_ltp: float,
        pair_regime: Any = None,
    ) -> tuple[str, float, str]:
        w = cfg.get("signal_weights") or {}
        def sw(name: str, default: float = 1.0) -> float:
            return float(w.get(name, default))

        prior_pe = float(cfg.get("prior_pe", 0.65))
        score_pe = prior_pe
        score_ce = 1 - prior_pe
        reasons: list[str] = []

        patterns = index.get("patterns") or {}
        spot = index.get("spot")
        support = index.get("support")
        resistance = index.get("resistance")
        rsi_v = index.get("rsi14")
        ema9 = index.get("ema9")
        ema21 = index.get("ema21")

        if pair_regime is not None:
            if pair_regime.regime == "AT_RESISTANCE":
                score_pe += 0.1 * sw("near_resistance")
                reasons.append("S/R: at resistance")
            elif pair_regime.regime == "AT_SUPPORT":
                score_ce += 0.1 * sw("near_support")
                reasons.append("S/R: at support")
            elif pair_regime.regime == "DECAY":
                reasons.append("decay regime — cautious")
                score_pe *= 0.9
                score_ce *= 0.9
            elif pair_regime.regime == "RANGE_BOUND":
                reasons.append("range-bound")
            if pair_regime.winner == "PE" and (pair_regime.expand_mult or 0) >= 1.2:
                score_pe += 0.08
                reasons.append("PE already expanding")
            if pair_regime.winner == "CE" and (pair_regime.expand_mult or 0) >= 1.2:
                score_ce += 0.05
                reasons.append("CE already expanding")

        if patterns.get("shooting_star") or patterns.get("bearish_engulf"):
            score_pe += 0.12 * sw("bearish_candle")
            reasons.append("5m bearish candle")
        if patterns.get("hammer") or patterns.get("bullish_engulf") or patterns.get("inverted_hammer"):
            score_ce += 0.10 * sw("bullish_candle")
            reasons.append("5m bullish/reversal candle")

        if rsi_v is not None:
            if rsi_v >= 60:
                score_pe += 0.08 * sw("rsi_high")
                reasons.append(f"RSI {rsi_v:.0f} high")
            elif rsi_v <= 40:
                score_ce += 0.08 * sw("rsi_low")
                reasons.append(f"RSI {rsi_v:.0f} low")

        if ema9 is not None and ema21 is not None:
            if ema9 < ema21:
                score_pe += 0.07 * sw("ema_bear")
                reasons.append("EMA9<EMA21")
            else:
                score_ce += 0.05 * sw("ema_bull")
                reasons.append("EMA9>EMA21")

        if spot is not None and resistance is not None and support is not None:
            if spot >= resistance * 0.998:
                score_pe += 0.08 * sw("near_resistance")
                reasons.append("near resistance")
            if spot <= support * 1.002:
                score_ce += 0.08 * sw("near_support")
                reasons.append("near support")

        if len(self.state.ce_hist) >= 30 and len(self.state.pe_hist) >= 30:
            ce_slope = self.state.ce_hist[-1] - self.state.ce_hist[-30]
            pe_slope = self.state.pe_hist[-1] - self.state.pe_hist[-30]
            if pe_slope > ce_slope + 1:
                score_pe += 0.06 * sw("pe_firming")
                reasons.append("PE premium firming")
            elif ce_slope > pe_slope + 1:
                score_ce += 0.04 * sw("ce_firming")
                reasons.append("CE premium firming")

        if score_ce > score_pe and (patterns.get("shooting_star") or (rsi_v and rsi_v > 65)):
            score_ce -= 0.1 * sw("ce_trap_filter")
            score_pe += 0.05 * sw("ce_trap_filter")
            reasons.append("CE trap filter")

        # Session market movement (spot vs open) — strongest directional prior
        trend = index.get("session_trend")
        if not isinstance(trend, dict) or not trend.get("bias"):
            trend = compute_session_trend(
                spot=spot,
                day_open=index.get("day_open"),
                closes=[],
                ema9=ema9,
                ema21=ema21,
                cfg=cfg,
            )
        score_ce, score_pe, trend_tags = apply_trend_to_scores(
            score_ce, score_pe, trend, weight=float(sw("session_trend", 1.0))
        )
        reasons.extend(trend_tags)

        # First 5m candle opening-range playbook (with indicators/news above)
        # Strong session trend overrides OR fade (fade-high→PE failed on trend days)
        orb = index.get("opening_range") or {}
        orb_bias = orb.get("bias")
        or_overridden = bool(
            cfg.get("trend_or_fade_override", True)
            and trend.get("block_counter_trend")
            and trend.get("prefer_side")
        )
        if orb_bias == "FADE_HIGH":
            if or_overridden and trend.get("prefer_side") == "CE":
                score_ce += 0.08 * sw("or_fade_high")
                reasons.append("OR high but session UP→keep CE")
            else:
                score_pe += 0.14 * sw("or_fade_high")
                score_ce -= 0.06 * sw("or_fade_high")
                reasons.append("OR fade high→PE")
        elif orb_bias == "EXPECT_RECOVER":
            score_ce += 0.12 * sw("or_expect_recover")
            score_pe -= 0.04 * sw("or_expect_recover")
            reasons.append("OR recover dip→CE")
        elif orb_bias == "BREAKDOWN":
            if or_overridden and trend.get("prefer_side") == "CE":
                reasons.append("OR breakdown ignored — session UP")
            else:
                score_pe += 0.14 * sw("or_breakdown")
                score_ce -= 0.06 * sw("or_breakdown")
                reasons.append("OR confirmed breakdown→PE")
        elif orb_bias == "INSIDE":
            reasons.append("OR inside first 5m")

        # Macro / news overlay
        adj = float(macro.get("confidence_adj") or 0)
        regime = macro.get("regime")
        if regime == "risk_off":
            score_pe += (0.08 - adj) * sw("macro_risk_off")
            score_ce -= 0.06 * sw("macro_risk_off")
            reasons.append("macro risk_off→PE")
        elif regime == "risk_on":
            score_ce += (0.05 + adj) * sw("macro_risk_on")
            reasons.append("macro risk_on→CE")
        if macro.get("vix") and macro["vix"] >= 18:
            score_pe += 0.05 * sw("macro_risk_off")
            reasons.append("elevated VIX")
        # Headline / macro / smart-money overlay (crude, USDINR, US futures, fut short pressure)
        hb = str(macro.get("headline_bias") or macro.get("news_bias") or "").lower()
        if hb in ("bearish", "risk_off", "negative"):
            score_pe += 0.06 * sw("macro_risk_off")
            reasons.append("news bearish→PE")
        elif hb in ("bullish", "risk_on", "positive"):
            score_ce += 0.05 * sw("macro_risk_on")
            reasons.append("news bullish→CE")
        hint = str(macro.get("side_hint") or "").upper()
        if hint == "PE":
            score_pe += 0.05
            reasons.append("macro side_hint→PE")
        elif hint == "CE":
            score_ce += 0.04
            reasons.append("macro side_hint→CE")
        sm = str(macro.get("smart_money") or "").lower()
        fii_reg = str(macro.get("fii_oi_regime") or "").lower()
        if sm == "short_pressure" or fii_reg == "short_buildup":
            score_pe += 0.08
            score_ce -= 0.05
            reasons.append("OI short_buildup→PE")
        elif sm == "long_pressure" or fii_reg == "long_buildup":
            score_ce += 0.06
            reasons.append("OI long_buildup→CE")
        elif sm == "short_covering" or fii_reg == "short_covering":
            score_ce -= 0.03
            reasons.append("short_covering→CE caution")
        elif sm == "long_unwinding" or fii_reg == "long_unwinding":
            score_pe += 0.05
            reasons.append("long_unwinding→PE")
        try:
            if float(macro.get("usdinr_change_pct") or 0) >= 0.4:
                score_pe += 0.04
                reasons.append("USDINR spike→PE")
        except Exception:
            pass
        try:
            es = float(macro.get("es_change_pct") or 0)
            if es <= -1.0:
                score_pe += 0.04
                reasons.append("US ES weak→PE")
            elif es >= 1.0:
                score_ce += 0.03
                reasons.append("US ES firm→CE")
        except Exception:
            pass

        # Opening shakeout (fake dump/spike) — institutional head-fake
        spot_closes = list(index.get("spot_closes") or [])
        shake = detect_opening_shakeout(spot_closes, cfg=cfg) if spot_closes else {"active": False}
        if shake.get("active") and shake.get("prefer_side") == "CE":
            score_ce += 0.16 * sw("session_trend")
            score_pe -= 0.10 * sw("session_trend")
            reasons.append(str(shake.get("reason")))
        elif shake.get("active") and shake.get("prefer_side") == "PE":
            score_pe += 0.16 * sw("session_trend")
            score_ce -= 0.10 * sw("session_trend")
            reasons.append(str(shake.get("reason")))

        total = max(score_ce + score_pe, 1e-6)
        p_pe = score_pe / total
        if p_pe >= 0.5:
            side, conf = "PE", p_pe
        else:
            side, conf = "CE", 1 - p_pe
        if shake.get("active") and shake.get("block_side") == side and shake.get("prefer_side"):
            side = str(shake["prefer_side"])
            conf = max(conf, 0.6)
            reasons.append(f"shakeout flip→{side}")
        return side, conf, "; ".join(reasons) or "balanced"

    def _enter(
        self,
        cfg: dict[str, Any],
        mode: str,
        symbol: str,
        side: str,
        entry: float,
        pair: dict[str, Any],
        release: Callable[[str], None],
        features: dict[str, Any] | None = None,
        lots: int | None = None,
        stop_px: float | None = None,
        target_px: float | None = None,
    ) -> None:
        lots = int(lots if lots is not None else cfg.get("algo1_lots", 1))
        lot_size = int(cfg.get("lot_size", 75))
        setup_id = str(uuid.uuid4())[:8]
        primary_lots, hedge_lots = split_hedge_lots(lots, cfg)
        leg = pair.get("ce") if side == "CE" else pair.get("pe")
        if not isinstance(leg, dict):
            leg = {}
        token = int(leg.get("instrument_token") or pair.get("instrument_token") or 0)
        sl = float(stop_px if stop_px is not None else stop_price(entry, cfg))
        tg = float(target_px if target_px is not None else target_price(entry, cfg))
        feats = dict(features or {})
        if hedge_lots > 0:
            feats["balanced"] = True
            feats["hedge_lots"] = hedge_lots
            feats["primary_lots"] = primary_lots
        strategy = "BalancedHedge" if hedge_lots > 0 else "ConvergenceBreakout"
        trade = broker.open_long(
            mode=mode,
            algo=self.algo_id,
            strategy=strategy,
            symbol=symbol,
            side=side,
            entry_price=entry,
            lots=primary_lots,
            lot_size=lot_size,
            cfg=cfg,
            allow_add=False,
            extra={
                "expiry": pair.get("expiry") or leg.get("expiry"),
                "strike": pair.get("strike") or leg.get("strike"),
                "instrument_token": token,
                "underlying": self.underlying,
                "note": self.state.reason,
                "setup_id": setup_id,
                "leg_role": "primary",
                "entry_features": feats,
                "stop_px": sl,
                "target_px": tg,
                "peak_px": float(entry),
            },
        )
        if trade.get("status") != "OPEN":
            diag = trade.get("diagnosis") or {}
            tip = (diag.get("tips") or [None])[0]
            self.state.last_message = (
                trade.get("message")
                or trade.get("note")
                or "entry rejected"
            ) + (f" · {tip}" if tip else "")
            self.state.notification = {
                "ts": datetime.now(IST).isoformat(),
                "message": self.state.last_message,
                "level": "error",
                "diagnosis": diag,
            }
            release(self.algo_id)
            return
        self.state.setup_id = setup_id
        self.state.open_trade_id = trade["id"]
        self.state.open_symbol = symbol
        self.state.entry_price = float(trade["entry_price"])
        self.state.hedge_trade_id = None
        self.state.hedge_symbol = None
        self.state.phase = "IN_TRADE"

        # Balanced: buy opposite ATM leg (~30%) as hedge
        if hedge_lots > 0 and hedge_enabled(cfg):
            h_side = opposite_side(side)
            h_leg = pair.get("ce") if h_side == "CE" else pair.get("pe")
            if isinstance(h_leg, dict) and h_leg.get("tradingsymbol"):
                h_sym = str(h_leg["tradingsymbol"])
                h_entry = float(h_leg.get("ltp") or entry)
                h_token = int(h_leg.get("instrument_token") or 0)
                h_trade = broker.open_long(
                    mode=mode,
                    algo=self.algo_id,
                    strategy="BalancedHedge",
                    symbol=h_sym,
                    side=h_side,
                    entry_price=h_entry,
                    lots=hedge_lots,
                    lot_size=lot_size,
                    cfg=cfg,
                    allow_add=True,
                    extra={
                        "expiry": pair.get("expiry") or h_leg.get("expiry"),
                        "strike": pair.get("strike") or h_leg.get("strike"),
                        "instrument_token": h_token,
                        "underlying": self.underlying,
                        "note": f"hedge for {setup_id}",
                        "setup_id": setup_id,
                        "leg_role": "hedge",
                        "parent_trade_id": trade["id"],
                        "entry_features": {
                            **feats,
                            "hedge": True,
                            "parent_side": side,
                        },
                        "stop_px": stop_price(h_entry, cfg),
                        "target_px": target_price(h_entry, cfg),
                        "peak_px": float(h_entry),
                    },
                )
                if h_trade.get("status") == "OPEN":
                    self.state.hedge_trade_id = h_trade["id"]
                    self.state.hedge_symbol = h_sym
                else:
                    self.state.last_message = (
                        f"primary OK; hedge rejected: "
                        f"{h_trade.get('message') or h_trade.get('note')}"
                    )

        conf = "CONFIRMED" if trade.get("broker_confirmed") or mode == "paper" else "pending"
        hedge_tag = (
            f" +hedge {self.state.hedge_symbol} x{hedge_lots}"
            if self.state.hedge_trade_id
            else ""
        )
        self.state.last_message = (
            f"entered {side} {symbol} @ {entry} x{primary_lots} SL{sl:g}/TG{tg:g} "
            f"{hedge_tag} [{trade.get('broker_status')}|{conf}]"
        )

    def _rest_ltp(self, symbol: str, trade: dict[str, Any]) -> float | None:
        """Direct Kite LTP fallback, throttled per symbol to avoid REST spam."""
        now = time.time()
        ts_map = getattr(self, "_rest_ltp_ts", None)
        if ts_map is None:
            ts_map = self._rest_ltp_ts = {}
        if now - float(ts_map.get(symbol) or 0.0) < 2.0:
            return None
        ts_map[symbol] = now
        try:
            return float(
                broker._market_ltp(
                    symbol,
                    fallback=None,
                    trade=trade,
                    extra={"underlying": self.underlying},
                )
            )
        except Exception:
            return None

    def _manage_open(
        self,
        cfg: dict[str, Any],
        px: float,
        release: Callable[[str], None],
        pair_regime: Any = None,
    ) -> None:
        assert self.state.open_trade_id and self.state.entry_price is not None
        lot_size = int(cfg.get("lot_size", 75))
        open_row = next(
            (t for t in trade_store.open_trades() if t.get("id") == self.state.open_trade_id),
            {},
        )
        qty = int(open_row.get("qty") or int(cfg.get("algo1_lots", 1)) * lot_size)
        entry = float(self.state.entry_price)
        peak = max(float(open_row.get("peak_px") or entry), px, entry)
        pnl = (px - entry) * qty
        mult = px / entry if entry else 1.0

        held_m = 0.0
        opened_raw = open_row.get("opened_at")
        if opened_raw:
            try:
                ots = datetime.fromisoformat(str(opened_raw).replace("Z", "+00:00"))
                if ots.tzinfo is None:
                    ots = ots.replace(tzinfo=IST)
                held_m = (datetime.now(IST) - ots.astimezone(IST)).total_seconds() / 60.0
            except Exception:
                held_m = 0.0

        feats = open_row.get("entry_features") or {}
        impulse_mode = feats.get("impulse_mode") if feats.get("impulse") else None
        if feats.get("daily_pa"):
            exit_cfg = {**cfg, **daily_pa_exit_overlay(cfg)}
        elif feats.get("micro_scalp"):
            exit_cfg = {**cfg, **micro_exit_overlay(cfg)}
        elif feats.get("daily_soft"):
            exit_cfg = {**cfg, **soft_exit_overlay(cfg)}
        elif impulse_mode:
            exit_cfg = impulse_exit_overrides(cfg, impulse_mode)
        else:
            exit_cfg = cfg

        entry_conf = feats.get("confidence")
        try:
            entry_conf_f = float(entry_conf) if entry_conf is not None else None
        except (TypeError, ValueError):
            entry_conf_f = None
        open_side = str(open_row.get("side") or self.state.predicted_side or "")
        decision = manage_percent_exit(
            entry=entry,
            px=px,
            peak=peak,
            cfg=exit_cfg,
            held_minutes=held_m,
            confidence=entry_conf_f,
            side=open_side,
        )
        if decision.get("action") != "exit" and (
            feats.get("trend_follow")
            or cfg.get("enable_trend_follow", True)
        ):
            spot_closes = list(getattr(self, "_tick_spot_closes", None) or [])
            if len(spot_closes) >= 20:
                flip = trend_follow_should_exit_open(
                    open_side=open_side,
                    closes=spot_closes,
                    cfg=cfg,
                )
                if flip.get("action") == "exit":
                    decision = {
                        **decision,
                        "action": "exit",
                        "reason": str(flip.get("reason") or "trend flip"),
                    }
        if decision.get("action") != "exit" and (
            feats.get("micro_scalp") or feats.get("daily_pa")
        ):
            entry_spot = feats.get("entry_spot")
            spot_now = getattr(self, "_tick_spot", None)
            if entry_spot is not None and spot_now is not None:
                bank_fn = (
                    pa_spot_bank_hit if feats.get("daily_pa") else micro_spot_bank_hit
                )
                bank = bank_fn(
                    side=open_side or "CE",
                    entry_spot=float(entry_spot),
                    spot=float(spot_now),
                    entry_px=entry,
                    px=px,
                    cfg=cfg,
                )
                if bank:
                    decision = {**decision, **bank}
        decision = apply_entry_hold_gate(
            decision,
            entry=entry,
            px=px,
            held_minutes=held_m,
            cfg=exit_cfg,
            confidence=entry_conf_f,
            side=open_side,
        )
        sl = float(decision.get("stop_px") or stop_price(entry, exit_cfg))
        tg = float(decision.get("target_px") or target_price(entry, exit_cfg, trail=True))

        # Ultra-fast scalp: bank small green within N seconds on impulse trades
        # Still respects the global min-hold / confidence loss-points gate.
        reason = None
        if impulse_mode and cfg.get("impulse_quick_scalp", True):
            held_s = held_m * 60.0
            scalp_s = float(cfg.get("impulse_scalp_seconds", 45))
            scalp_pct = float(cfg.get("impulse_scalp_pct", 2.0)) / 100.0
            if held_s <= scalp_s and px >= entry * (1.0 + scalp_pct):
                reason = f"impulse scalp +{scalp_pct*100:.0f}% in {held_s:.0f}s"
            # Also cut if impulse turns red fast (failed spike)
            fail_s = float(cfg.get("impulse_fail_seconds", 40))
            fail_pct = float(cfg.get("impulse_fail_pct", 2.0)) / 100.0
            if reason is None and held_s >= fail_s and px <= entry * (1.0 - fail_pct):
                reason = f"impulse fail −{fail_pct*100:.0f}% in {held_s:.0f}s"
            if reason:
                gated = apply_entry_hold_gate(
                    {"action": "exit", "reason": reason},
                    entry=entry,
                    px=px,
                    held_minutes=held_m,
                    cfg=exit_cfg,
                    confidence=entry_conf_f,
                    side=open_side,
                )
                if gated.get("action") != "exit":
                    reason = None
                else:
                    reason = str(gated.get("reason") or reason)

        trade_store.update_trade(
            self.state.open_trade_id,
            {
                "live_price": px,
                "live_pnl_rupees": round(pnl, 2),
                "live_mult": round(mult, 2),
                "peak_px": peak,
                "stop_px": sl,
                "target_px": tg,
            },
        )

        # Legacy pair-regime only when percent exits disabled
        if reason is None and not exit_cfg.get("use_percent_exits", True) and pair_regime is not None:
            side = "CE" if (self.state.open_symbol or "").endswith("CE") else "PE"
            reason = manage_long_decision(
                side=side,
                entry=entry,
                px=px,
                regime=pair_regime,
                cfg=exit_cfg,
            )
        if reason is None and decision.get("action") == "exit":
            reason = str(decision.get("reason") or "exit")

        if reason:
            # Close hedge first (same exit clock as primary FastTrack rules)
            if self.state.hedge_trade_id:
                hedge_row = next(
                    (
                        t
                        for t in trade_store.open_trades()
                        if t.get("id") == self.state.hedge_trade_id
                    ),
                    {},
                )
                h_px = float(
                    hedge_row.get("live_price")
                    or hedge_row.get("entry_price")
                    or 0
                )
                if h_px <= 0:
                    h_px = px
                hedge_closed = broker.close_long(
                    self.state.hedge_trade_id,
                    h_px,
                    f"hedge with primary: {reason}",
                    lot_size,
                )
                if not hedge_closed:
                    self.state.last_message = (
                        f"EXIT PENDING — hedge sell not confirmed ({reason})"
                    )
                    return
                self.state.hedge_trade_id = None
                self.state.hedge_symbol = None
            closed = broker.close_long(self.state.open_trade_id, px, reason, lot_size)
            if not closed:
                # Keep the leg OPEN and retry; never pretend a live sell filled.
                self.state.last_message = (
                    f"EXIT PENDING — sell not confirmed ({reason})"
                )
                return
            self.state.last_message = f"closed: {reason}"
            self.state.open_trade_id = None
            self.state.open_symbol = None
            self.state.entry_price = None
            self.state.phase = "SCAN"
            self.state.notified = False
            release(self.algo_id)
        else:
            reg = pair_regime.regime if pair_regime else "pct"
            self.state.last_message = (
                f"managing {self.state.open_symbol} @ {px:.1f} ({mult:.2f}x) "
                f"PnL₹ {pnl:.0f} SL{sl:g}/TG{tg:g} [{reg}]"
            )

    def snapshot(self, quotes: dict[str, float]) -> dict[str, Any]:
        pair = self.state.pair
        ce = pe = None
        if pair:
            ce = {"symbol": pair["ce"]["tradingsymbol"], "ltp": quotes.get(pair["ce"]["tradingsymbol"])}
            pe = {"symbol": pair["pe"]["tradingsymbol"], "ltp": quotes.get(pair["pe"]["tradingsymbol"])}
        return {
            "algo": self.algo_id,
            "name": "Market Direction (one side)",
            "phase": self.state.phase,
            "predicted_side": self.state.predicted_side,
            "confidence": round(self.state.confidence, 3),
            "reason": self.state.reason,
            "message": self.state.last_message,
            "notification": self.state.notification,
            "ce": ce,
            "pe": pe,
            "open_trade_id": self.state.open_trade_id,
            "convergence_zone": self.state.conv_zone,
        }

    def is_flat(self) -> bool:
        # Disk truth — never report flat while OPEN rows exist
        if pos_integrity.open_for_algo(self.algo_id):
            return False
        return self.state.open_trade_id is None

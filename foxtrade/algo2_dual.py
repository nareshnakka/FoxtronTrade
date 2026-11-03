from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from foxtrade import broker
from foxtrade import trade_store
from foxtrade.convergence_zone import detect_convergence_zone, in_convergence_zone
from foxtrade.learner import build_entry_features
from foxtrade.scanner import expiry_allowed_for_entry, find_next_week_option, is_converged
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
    mid_at_entry: float | None = None
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


class Algo2Dual:
    """Buy both at convergence; book breakout on reverse; ride fallen bounce."""

    def __init__(self) -> None:
        self.state = Algo2State()

    def restore_from_store(self, runtime: dict[str, Any] | None = None) -> bool:
        """Re-attach newest OPEN algo2 setup after process restart."""
        opens = [t for t in trade_store.open_trades() if t.get("algo") == "algo2"]
        if not opens:
            return False
        saved = (runtime or {}).get("algo2") or {}
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
        elif "breakout" in roles and "fallen" in roles:
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
        else:
            self.state.phase = "DUAL_OPEN"
        if self.state.ce_entry and self.state.pe_entry:
            self.state.mid_at_entry = (self.state.ce_entry + self.state.pe_entry) / 2
        elif saved.get("mid_at_entry"):
            self.state.mid_at_entry = float(saved["mid_at_entry"])
        self.state.fallen_rolled = bool(saved.get("fallen_rolled"))
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
            "mid_at_entry": self.state.mid_at_entry,
            "fallen_rolled": self.state.fallen_rolled,
            "fallen_roll_from": self.state.fallen_roll_from,
            "fallen_roll_to": self.state.fallen_roll_to,
            "fallen_strike": self.state.fallen_strike,
            "fallen_expiry": self.state.fallen_expiry,
        }

    def on_tick(self, ctx: dict[str, Any]) -> None:
        cfg = ctx["config"]
        quotes = ctx.get("quotes") or {}
        index = ctx.get("index") or {}
        release: Callable[[str], None] = ctx["release"]
        pair_regime = ctx.get("pair_regime")

        # Drop ghost DUAL_OPEN after legs closed outside _clear (orphan/manual)
        if self._sync_flat_if_no_opens(release):
            return

        # Manage restored / open positions even without scan pair
        if self.state.phase in ("DUAL_OPEN", "BREAKOUT_WATCH", "FALLEN_HOLD"):
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
            if ce_ltp is not None and pe_ltp is not None:
                self._manage(cfg, ce_ltp, pe_ltp, index, release, pair_regime, ctx)
                return
            self.state.last_message = "restored — waiting CE/PE quotes"
            return

        if not cfg.get("algo2_enabled", True):
            self.state.last_message = "disabled"
            return

        pair = ctx.get("pair")
        in_session = ctx.get("in_session", False)
        mode = "live" if cfg.get("live_trading") else "paper"
        try_acquire: Callable[[str], bool] = ctx["try_acquire"]
        decay_block = bool(ctx.get("decay_block"))

        if not pair:
            self.state.last_message = "no pair"
            return

        ce_sym = pair["ce"]["tradingsymbol"]
        pe_sym = pair["pe"]["tradingsymbol"]
        ce_ltp = quotes.get(ce_sym)
        pe_ltp = quotes.get(pe_sym)
        if ce_ltp is None or pe_ltp is None:
            return

        self.state.pair = pair
        self.state.ce_hist.append(ce_ltp)
        self.state.pe_hist.append(pe_ltp)
        n = int(cfg.get("history_seconds", 500))
        self.state.ce_hist = self.state.ce_hist[-n:]
        self.state.pe_hist = self.state.pe_hist[-n:]
        self.state.conv_zone = detect_convergence_zone(
            self.state.ce_hist,
            self.state.pe_hist,
            gap_tol=float(cfg.get("convergence_zone_gap", 10.0)),
            min_samples=int(cfg.get("convergence_zone_min_samples", 25)),
        )

        if not in_session:
            start = cfg.get("session_start", "09:15")
            end = cfg.get("session_end", "13:30")
            self.state.last_message = f"outside {start}–{end} window"
            return

        if decay_block:
            self.state.last_message = f"decay guard: {ctx.get('decay_msg', 'blocked')}"
            return

        cd_min = int(cfg.get("entry_cooldown_minutes", 60))
        if cd_min > 0:
            since = trade_store.seconds_since_last_close("algo2", mode)
            if since is not None and since < cd_min * 60:
                left = int(cd_min * 60 - since)
                self.state.last_message = f"cool-down {left // 60}m {left % 60}s left"
                return

        if trade_store.count_day_setups(None, mode) >= int(cfg.get("max_trades_per_day", 15)):
            self.state.last_message = "daily setup cap"
            return

        tol = float(cfg.get("convergence_tol", 3.0))
        low = float(cfg.get("premium_low", 125))
        high = float(cfg.get("premium_high", 145))
        if self.state.conv_zone:
            converged = in_convergence_zone(ce_ltp, pe_ltp, self.state.conv_zone, entry_tol=tol)
        else:
            converged = is_converged(ce_ltp, pe_ltp, tol, low, high)
        if not converged:
            z = self.state.conv_zone
            zmsg = f" zone {z['low']:.0f}-{z['high']:.0f}" if z else ""
            self.state.last_message = f"waiting converge gap={abs(ce_ltp - pe_ltp):.1f}{zmsg}"
            return

        # Expiry day: never enter current-week / 0DTE (field + both symbols)
        pair_exp = (
            pair.get("expiry")
            or (pair.get("ce") or {}).get("expiry")
            or (pair.get("pe") or {}).get("expiry")
        )
        if not expiry_allowed_for_entry(pair_exp, symbol=ce_sym) or not expiry_allowed_for_entry(
            pair_exp, symbol=pe_sym
        ):
            self.state.last_message = (
                f"skip same-day expiry {ce_sym}/{pe_sym} ({pair_exp}) — use next week"
            )
            return
        scan = ctx.get("scan") or {}
        trade_week = str(scan.get("current_expiry") or "")[:10]
        if trade_week and str(pair_exp or "")[:10] != trade_week:
            self.state.last_message = (
                f"skip wrong week {pair_exp} — trade week is {trade_week}"
            )
            return

        if not try_acquire("algo2"):
            self.state.last_message = "pair locked by other algo"
            return

        self._enter_dual(cfg, mode, pair, ce_sym, pe_sym, ce_ltp, pe_ltp, ctx, release)

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
        lots = int(cfg.get("algo2_lots", 1))
        lot_size = int(cfg.get("lot_size", 75))
        mid = (ce_ltp + pe_ltp) / 2
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
        ce_trade = broker.open_long(
            mode=mode,
            algo="algo2",
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
            },
        )
        pe_trade = broker.open_long(
            mode=mode,
            algo="algo2",
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
            },
        )
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
            release("algo2")
            return
        self.state.setup_id = setup_id
        self.state.ce_trade_id = ce_trade["id"]
        self.state.pe_trade_id = pe_trade["id"]
        self.state.ce_symbol = ce_sym
        self.state.pe_symbol = pe_sym
        self.state.ce_entry = ce_ltp
        self.state.pe_entry = pe_ltp
        self.state.mid_at_entry = mid
        self.state.phase = "DUAL_OPEN"
        self.state.last_message = f"dual entry CE@{ce_ltp} PE@{pe_ltp}"
        note = {
            "ts": datetime.now(IST).isoformat(),
            "algo": "algo2",
            "message": f"DUAL ENTRY both legs @ converge ~{mid:.1f}",
            "ce": ce_sym,
            "pe": pe_sym,
        }
        self.state.notification = note
        ctx.setdefault("notifications", []).append(note)

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

            # Regime-aware early square
            if pair_regime is not None and pair_regime.regime == "DECAY":
                if ce_move <= -5 and pe_move <= -5:
                    self._square_both(lot_size, ce_ltp, pe_ltp, "decay regime both", release)
                    return
            if pair_regime is not None and pair_regime.regime == "RANGE_BOUND":
                if abs(ce_move) < 3 and abs(pe_move) < 3:
                    self.state.last_message = "dual open range-bound — waiting skew"
                    # don't force exit yet
                elif ce_move <= -8 and pe_move <= -8:
                    self._square_both(lot_size, ce_ltp, pe_ltp, "range decay both", release)
                    return

            # Prefer breakout side = session trend, else opening-range playbook
            trend = (index or {}).get("session_trend")
            if not isinstance(trend, dict):
                trend = compute_session_trend(
                    spot=(index or {}).get("spot"),
                    day_open=(index or {}).get("day_open"),
                    cfg=cfg,
                )
            trend_prefer = trend.get("prefer_side") if cfg.get("algo2_trend_breakout_boost", True) else None
            orb = (index or {}).get("opening_range") or {}
            orb_prefer = orb.get("prefer_side")  # CE | PE | None
            prefer = trend_prefer or orb_prefer

            # Trend day: lock breakout earlier when winner aligns with spot move
            if trend_prefer == "CE" and ce_move >= 4 and ce_move > pe_move + 2:
                self._mark_breakout("CE", ce_ltp)
                return
            if trend_prefer == "PE" and pe_move >= 4 and pe_move > ce_move + 2:
                self._mark_breakout("PE", pe_ltp)
                return

            # Crush loser tracking 80/70/50 while winner expands
            if pair_regime is not None and pair_regime.crush_level is not None:
                if pair_regime.winner == "CE" and ce_move >= 5:
                    self._mark_breakout("CE", ce_ltp)
                    return
                if pair_regime.winner == "PE" and pe_move >= 5:
                    self._mark_breakout("PE", pe_ltp)
                    return

            ce_break = ce_mult >= 1.5 or (ce_move >= 8 and ce_move > pe_move + 4)
            pe_break = pe_mult >= 1.5 or (pe_move >= 8 and pe_move > ce_move + 4)
            if ce_break and pe_break and prefer in ("CE", "PE"):
                self._mark_breakout(prefer, ce_ltp if prefer == "CE" else pe_ltp)
            elif ce_break and (not pe_break or prefer != "PE"):
                self._mark_breakout("CE", ce_ltp)
            elif pe_break and (not ce_break or prefer != "CE"):
                self._mark_breakout("PE", pe_ltp)
            else:
                if ce_move <= -8 and pe_move <= -8:
                    self._square_both(lot_size, ce_ltp, pe_ltp, "range decay both", release)
                    return
                crush = f" crush≤{pair_regime.crush_level:.0f}" if pair_regime and pair_regime.crush_level else ""
                orb_tag = f" OR:{orb.get('bias')}" if orb.get("bias") else ""
                tr_tag = f" trend:{trend.get('bias')}" if trend.get("bias") else ""
                self.state.last_message = (
                    f"dual ΔCE {ce_move:+.1f}({ce_mult:.1f}x) ΔPE {pe_move:+.1f}({pe_mult:.1f}x)"
                    f"{crush}{orb_tag}{tr_tag}"
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
            reverse = self._breakout_reverse(index, b_side or "PE", px)
            # S/R against the breakout
            if pair_regime is not None:
                if b_side == "CE" and pair_regime.near_resistance and reverse:
                    reverse = True
                if b_side == "PE" and pair_regime.near_support and reverse:
                    reverse = True

            in_window = mins >= 5
            drop_from_peak = (self.state.breakout_peak or px) - px

            # Book at ~2–3x even before reverse if extended
            if mult >= expand_cap:
                tid = self.state.breakout_trade_id
                if tid:
                    broker.close_long(tid, px, f"expand {mult:.1f}x book", lot_size)
                    trade_store.update_trade(tid, {"leg_role": "breakout_closed"})
                if b_side == "CE":
                    self.state.ce_trade_id = None
                else:
                    self.state.pe_trade_id = None
                f_px = pe_ltp if b_side == "CE" else ce_ltp
                self.state.fallen_low = f_px
                self.state.phase = "FALLEN_HOLD"
                self.state.last_message = f"booked {b_side} at {mult:.1f}x; holding fallen"
                return

            if reverse and (in_window or drop_from_peak >= 5 or mult >= 2.0):
                tid = self.state.breakout_trade_id
                if tid:
                    broker.close_long(tid, px, f"breakout reverse @ {px:.1f} ({mult:.1f}x)", lot_size)
                    trade_store.update_trade(tid, {"leg_role": "breakout_closed"})
                if b_side == "CE":
                    self.state.ce_trade_id = None
                else:
                    self.state.pe_trade_id = None
                f_px = pe_ltp if b_side == "CE" else ce_ltp
                self.state.fallen_low = f_px
                self.state.phase = "FALLEN_HOLD"
                self.state.last_message = f"booked {b_side} reverse; holding fallen"
            else:
                crush = ""
                if pair_regime and pair_regime.crush_level:
                    crush = f" | loser≤{pair_regime.crush_level:.0f}"
                self.state.last_message = (
                    f"breakout {b_side} peak {self.state.breakout_peak:.1f} "
                    f"now {px:.1f} ({mult:.1f}x) t={mins:.0f}m{crush}"
                )
            return

        if self.state.phase == "FALLEN_HOLD":
            f_side = self.state.fallen_side
            f_px = ce_ltp if f_side == "CE" else pe_ltp
            f_entry = self.state.ce_entry if f_side == "CE" else self.state.pe_entry
            tid = self.state.fallen_trade_id
            if self.state.fallen_low is None or f_px < self.state.fallen_low:
                self.state.fallen_low = f_px

            # Holding fallen PUT for reverse → roll to next-week PUT once crushed
            if f_side == "PE" and self._try_fallen_pe_roll(cfg, f_px, f_entry, tid, ctx):
                return

            bounce = float(cfg.get("fallen_bounce_points", 10))
            sl_pts = float(cfg.get("fallen_sl_points", 10))
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
                mid = self.state.mid_at_entry or 135
            stop_line = (self.state.fallen_low or f_px) - sl_pts
            crush_cut = float(cfg.get("crush_cut", 85))

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
            elif f_entry is not None and f_px >= f_entry + bounce:
                reason = f"fallen bounce +{bounce}"
            elif not self.state.fallen_rolled and f_px >= mid:
                reason = "fallen back to convergence"
            elif (
                not self.state.fallen_rolled
                and f_px > mid + 5
                and self.state.breakout_peak
                and f_px >= self.state.breakout_peak * 0.95
            ):
                reason = "fallen flipped to breakout"
            # S/R bounce assist
            elif (
                pair_regime
                and f_side == "CE"
                and pair_regime.near_support
                and f_entry is not None
                and f_px >= f_entry + 5
            ):
                reason = "fallen CE bounce at support"
            elif (
                pair_regime
                and f_side == "PE"
                and pair_regime.near_resistance
                and f_entry is not None
                and f_px >= f_entry + 5
            ):
                reason = "fallen PE bounce at resistance"

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
                self.state.last_message = (
                    f"fallen {f_side} @{f_px:.1f}{lvl} low {self.state.fallen_low:.1f} mid~{mid:.0f}{roll_tag}"
                )

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
                algo="algo2",
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
        note = {"ts": datetime.now(IST).isoformat(), "algo": "algo2", "message": msg}
        self.state.notification = note
        ctx.setdefault("notifications", []).append(note)
        return True

    def _mark_breakout(self, side: str, px: float) -> None:
        self.state.breakout_side = side
        self.state.fallen_side = "PE" if side == "CE" else "CE"
        self.state.breakout_peak = px
        self.state.breakout_at = datetime.now(IST)
        if side == "CE":
            self.state.breakout_trade_id = self.state.ce_trade_id
            self.state.fallen_trade_id = self.state.pe_trade_id
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
        self.state.phase = "BREAKOUT_WATCH"
        self.state.last_message = f"breakout detected on {side} @{px:.1f}"

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

    def _square_both(
        self,
        lot_size: int,
        ce_ltp: float,
        pe_ltp: float,
        reason: str,
        release: Callable[[str], None],
    ) -> None:
        if self.state.ce_trade_id:
            broker.close_long(self.state.ce_trade_id, ce_ltp, reason, lot_size)
        if self.state.pe_trade_id:
            broker.close_long(self.state.pe_trade_id, pe_ltp, reason, lot_size)
        self._clear(release)
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
        self.state.breakout_side = None
        self.state.fallen_side = None
        self.state.breakout_peak = None
        self.state.breakout_at = None
        self.state.fallen_low = None
        self.state.setup_id = None
        self.state.fallen_rolled = False
        self.state.fallen_roll_from = None
        self.state.fallen_roll_to = None
        self.state.fallen_strike = None
        self.state.fallen_expiry = None
        if release:
            release("algo2")

    def _sync_flat_if_no_opens(self, release: Callable[[str], None] | None = None) -> bool:
        """If phase looks in-trade but store has no OPEN algo2 legs, reset (stale runtime)."""
        if self.state.phase == "SCAN" and not self.state.ce_trade_id and not self.state.pe_trade_id:
            return False
        open_ids = {t["id"] for t in trade_store.open_trades() if t.get("algo") == "algo2"}
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
        pair = self.state.pair
        ce = pe = None
        if pair:
            ce = {"symbol": pair["ce"]["tradingsymbol"], "ltp": quotes.get(pair["ce"]["tradingsymbol"])}
            pe = {"symbol": pair["pe"]["tradingsymbol"], "ltp": quotes.get(pair["pe"]["tradingsymbol"])}
        return {
            "algo": "algo2",
            "name": "Dual-leg Convergence",
            "phase": self.state.phase,
            "message": self.state.last_message,
            "breakout_side": self.state.breakout_side,
            "fallen_side": self.state.fallen_side,
            "notification": self.state.notification,
            "ce": ce,
            "pe": pe,
            "convergence_zone": self.state.conv_zone,
            "fallen_rolled": self.state.fallen_rolled,
            "fallen_roll_to": self.state.fallen_roll_to,
        }

    def is_flat(self) -> bool:
        # Disk truth — never flat while algo2 OPEN rows exist
        try:
            from foxtrade.position_integrity import open_for_algo

            if open_for_algo("algo2"):
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

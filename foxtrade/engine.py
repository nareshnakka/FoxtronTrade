from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade import trade_store
from foxtrade.algo1_breakout import Algo1Breakout
from foxtrade.algo2_dual import Algo2Dual
from foxtrade.dual_book import DualBook
from foxtrade.config_store import load_config
from foxtrade.decay import DecayGuard
from foxtrade.decay_memory import DecayMemory
from foxtrade import heartbeat as hb
from foxtrade import link_alert
from foxtrade import stall_alert
from foxtrade import position_integrity as pos_integrity
from foxtrade.macro import get_macro
from foxtrade.fii_oi_scanner import scan_fii_oi
from foxtrade.market import MarketHub
from foxtrade.convergence_tracker import ConvergenceTracker
from foxtrade.late_start import is_late_start, seed_series_from_history
from foxtrade.learner import run_daily_learn
from foxtrade.pair_regime import evaluate_pair_regime
from foxtrade import position_guard
from foxtrade.runtime_store import load_runtime, save_runtime

IST = ZoneInfo("Asia/Kolkata")


def _parse_hhmm(raw: str, default_h: int, default_m: int) -> tuple[int, int]:
    try:
        parts = str(raw).split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return default_h, default_m


def _in_session(cfg: dict[str, Any]) -> bool:
    """
    New-entry window only (open positions are managed even outside this).
    Aligns with causal replay: entry_window_* AND session_start+no_entry_after_minutes.
    """
    now = datetime.now(IST)
    start = cfg.get("entry_window_start") or cfg.get("session_start", "09:15")
    end = cfg.get("entry_window_end") or cfg.get("session_end", "15:15")
    sh, sm = _parse_hhmm(start, 9, 15)
    eh, em = _parse_hhmm(end, 15, 15)
    t = now.hour * 60 + now.minute
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    # Replay cut: minutes from session_start (default 09:15 + 300 = 14:15)
    sess = cfg.get("session_start") or start
    ssh, ssm = _parse_hhmm(str(sess), 9, 15)
    sess_start_m = ssh * 60 + ssm
    no_after = int(cfg.get("no_entry_after_minutes", 300) or 300)
    end_m = min(end_m, sess_start_m + no_after)
    if now.weekday() >= 5:
        # Weekend dry-run only when paper; never treat as live session
        return not bool(cfg.get("live_trading"))
    return start_m <= t <= end_m


class TradingEngine:
    def __init__(self) -> None:
        self.config = load_config()
        self.market = MarketHub(history_seconds=int(self.config.get("history_seconds", 23400)))
        self.market.dual_spot = bool(self.config.get("dual_mode", False))
        self.dual = DualBook()
        self.algo1 = self.dual.books["NIFTY"]  # dashboard / legacy alias
        self.algo2 = Algo2Dual()
        self.decay = DecayGuard()
        self.decay_memory = DecayMemory()
        self.tracker = ConvergenceTracker()
        self.scan: dict[str, Any] = {}
        self.macro: dict[str, Any] = {}
        self.fii_oi: dict[str, Any] = {}
        self.notifications: list[dict[str, Any]] = []
        self._activity_log: list[dict[str, Any]] = []
        self._last_activity_line: str = ""
        self.last_scan_at: float = 0
        self._setup_owner: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.decay.reset_for_session(int(self.config.get("decay_observe_seconds", 480)))
        self._post_decay: dict[str, Any] = {}

    def try_acquire(self, algo: str) -> bool:
        if not self.config.get("exclusive_entries", True):
            return True
        with self.lock:
            if self._setup_owner is None or self._setup_owner == algo:
                self._setup_owner = algo
                return True
            return False

    def release(self, algo: str) -> None:
        with self.lock:
            if self._setup_owner == algo:
                self._setup_owner = None

    def start(self) -> None:
        """Start market + engine threads; heavy Kite bootstrap runs inside engine loop."""
        self.market.start()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_learn_try = 0.0
        self._last_runtime_save = 0.0
        self._eod_reset_day: str | None = None
        self._needs_bootstrap = True
        # Restore open positions immediately (disk only — no Kite) so they are not lost
        try:
            runtime = load_runtime()
            if runtime.get("setup_owner"):
                self._setup_owner = runtime.get("setup_owner")
            restored = []
            if self.algo1.restore_from_store(runtime):
                restored.append("algo1")
            for uid in self.dual.restore(runtime):
                if uid not in restored and uid != "NIFTY":
                    restored.append(f"dual:{uid}")
            if self.algo2.restore_from_store(runtime):
                restored.append("algo2")
            self.market.set_watch(position_guard.open_watch_instruments(), replace=False)
            if restored:
                self.notifications.append(
                    {
                        "ts": datetime.now(IST).isoformat(),
                        "message": f"Restored open positions: {', '.join(restored)} — monitoring continues",
                    }
                )
            # Drop ghost IN_TRADE / FALLEN_HOLD when store has no OPEN legs
            self._sync_flat_runtime()
            self._persist_runtime()
        except Exception as exc:  # noqa: BLE001
            self.scan = {"error": f"restore: {exc}"}
        self._thread = threading.Thread(target=self._loop, name="trading-engine", daemon=True)
        self._thread.start()

    def _sync_flat_runtime(self) -> None:
        """If algos look in-trade but trades.json has no OPEN rows, force SCAN."""
        opens = trade_store.open_trades()
        open_ids = {t["id"] for t in opens}
        if self.algo1.state.open_trade_id and self.algo1.state.open_trade_id not in open_ids:
            self.algo1.state.open_trade_id = None
            self.algo1.state.open_symbol = None
            self.algo1.state.entry_price = None
            self.algo1.state.phase = "SCAN"
            self.algo1.state.setup_id = None
            self.algo1.state.last_message = "cleared ghost algo1 — no open trade"
            self.release("algo1")
        try:
            self.algo2._sync_flat_if_no_opens(self.release)
        except Exception:
            if not any(t.get("algo") == "algo2" for t in opens):
                self.algo2._clear(self.release)
                self.algo2.state.last_message = "cleared ghost algo2 — no open trade"

    def _run_eod_reset(self) -> None:
        """
        Once per day after eod_reset_time (default 15:30 IST):
        square off leftovers, clear algo/tracker locks, persist clean runtime.
        """
        from foxtrade import broker

        cfg = load_config()
        lot_size = int(cfg.get("lot_size", 75))
        ok_ids: list[str] = []
        for t in list(trade_store.open_trades()):
            try:
                r = broker.square_off_market(
                    t["id"],
                    reason="EOD reset 15:30",
                    lot_size=lot_size,
                )
                if r.get("ok"):
                    ok_ids.append(t["id"])
            except Exception:
                pass
        if ok_ids:
            self.detach_closed_trades(ok_ids)
        squared = len(ok_ids)

        # Force flat algos regardless of store race
        self.algo1.state.open_trade_id = None
        self.algo1.state.open_symbol = None
        self.algo1.state.entry_price = None
        self.algo1.state.phase = "SCAN"
        self.algo1.state.setup_id = None
        self.algo1.state.notified = False
        self.algo1.state.last_message = "EOD reset — ready for next session"
        self.algo2._clear(self.release)
        self.algo2.state.last_message = "EOD reset — ready for next session"
        with self.lock:
            self._setup_owner = None

        # Fresh tracker for next day
        self.tracker.active = None
        self.tracker.active_id = None
        self.tracker.locked_merge = False
        self.tracker.locked_for_trade = False
        self.tracker.decayed_ids.clear()
        self.tracker.converged_once.clear()
        self.tracker._late_dig_done = False
        self.tracker.switch_reason = "EOD reset"
        self.decay.reset_for_session(int(cfg.get("decay_observe_seconds", 480)))
        try:
            self.decay_memory.finalize_day()
        except Exception:
            pass

        try:
            self._persist_runtime()
        except Exception:
            pass
        with self.lock:
            self.notifications.append(
                {
                    "ts": datetime.now(IST).isoformat(),
                    "message": (
                        f"EOD reset done"
                        + (f" — squared {squared} open leg(s)" if squared else " — state cleared")
                        + ". Login again tomorrow before 09:15."
                    ),
                }
            )
            self.notifications = self.notifications[-50:]

    def refresh_auth(self, *, force_profile: bool = True) -> dict[str, Any]:
        from kite_client import check_session

        status = check_session(force_profile=force_profile)
        # Unattended: try TOTP auto-login when session missing/stale
        if status.get("needs_login") and self.config.get("unattended_auto_login", True):
            try:
                from foxtrade.auto_login import ensure_kite_session

                status = ensure_kite_session(force_login=True)
            except Exception as exc:  # noqa: BLE001
                status = {**status, "auto_login": f"err:{exc}"}
        self._auth_status = status
        if status.get("needs_login"):
            with self.lock:
                self.notifications.append(
                    {
                        "ts": datetime.now(IST).isoformat(),
                        "message": (
                            f"Kite login required: {status.get('reason')} "
                            f"(auto={status.get('auto_login')})"
                        ),
                    }
                )
                self.notifications = self.notifications[-50:]
        elif status.get("auto_login") == "ok":
            with self.lock:
                self.notifications.append(
                    {
                        "ts": datetime.now(IST).isoformat(),
                        "message": f"Auto-login OK — {status.get('user_name') or status.get('user_id')}",
                    }
                )
                self.notifications = self.notifications[-50:]
        return status

    def _bootstrap(self) -> None:
        """Slow Kite work — must not block Flask bind."""
        try:
            hb.set_meta(phase="bootstrap")
            hb.beat(phase="bootstrap_start")
            self.refresh_auth(force_profile=True)
            # Full cash session buffer so scrolling back on CE/PE charts has data
            self.market.resize_history(int(self.config.get("history_seconds", 23400)))
            filled = position_guard.backfill_open_trade_tokens()
            if filled:
                self.scan = {**self.scan, "tokens_backfilled": filled}
            self._rescan(dig_history=True)
            open_inst = position_guard.open_watch_instruments()
            self.market.set_watch(open_inst, replace=False)
            # Always seed today's minute history for OPEN legs (survive restart mid-session)
            try:
                n_open = seed_series_from_history(self.market, open_inst, self.config)
                self.scan = {**self.scan, "open_history_seeded": n_open}
            except Exception as exc:  # noqa: BLE001
                self.scan = {**self.scan, "open_history_seed_error": str(exc)}
            # Also seed sibling CE/PE for chart lock pair
            try:
                chart_pair = self._chart_pair_from_open(self.scan.get("pair"))
                if chart_pair:
                    sibs = [chart_pair["ce"], chart_pair["pe"]]
                    self.market.set_watch(sibs, replace=False)
                    seed_series_from_history(self.market, sibs, self.config)
            except Exception:
                pass
            self.market.refresh_nifty_candles()
            self.macro = get_macro(force=True)
            try:
                self.fii_oi = scan_fii_oi(force=True, cfg=self.config)
                hb.set_meta(
                    fii_oi_regime=self.fii_oi.get("regime"),
                    fii_oi_smart=self.fii_oi.get("smart_money"),
                    fii_block_ce=bool((self.fii_oi.get("actions") or {}).get("block_ce")),
                )
            except Exception as exc:  # noqa: BLE001
                self.fii_oi = {"error": str(exc), "regime": "unknown"}
            run_daily_learn(force=False)
            self._persist_runtime()
        except Exception as exc:  # noqa: BLE001
            self.scan = {**self.scan, "error": str(exc)}

    def _persist_runtime(self) -> None:
        snap = {
            "algo1": self.algo1.runtime_snapshot(),
            "algo2": self.algo2.runtime_snapshot(),
            "setup_owner": self._setup_owner,
            "saved_at": datetime.now(IST).isoformat(),
        }
        snap.update(self.dual.runtime_snapshot())
        save_runtime(snap)

    def _managed_trade_ids(self) -> set[str]:
        """All OPEN strategy legs are managed — never orphan-abandon after restart."""
        ids: set[str] = set()
        if self.algo1.state.open_trade_id:
            ids.add(self.algo1.state.open_trade_id)
        sx = self.dual.books.get("SENSEX")
        if sx and sx.state.open_trade_id:
            ids.add(sx.state.open_trade_id)
        for tid in (
            self.algo2.state.ce_trade_id,
            self.algo2.state.pe_trade_id,
            self.algo2.state.breakout_trade_id,
            self.algo2.state.fallen_trade_id,
        ):
            if tid:
                ids.add(tid)
        # Disk truth: every OPEN algo1/algo2 row is protected + managed
        for t in trade_store.open_trades():
            if t.get("algo") in ("algo1", "algo2") and t.get("id"):
                ids.add(t["id"])
        return ids

    def stop(self) -> None:
        try:
            self._persist_runtime()
        except Exception:
            pass
        self._stop.set()
        self.market.stop()

    def restart(self, *, reason: str = "manual") -> dict[str, Any]:
        """
        Soft-restart market + engine threads without killing Flask.
        If a thread is hung (join timeout), returns need_relaunch=True
        so the watchdog can spawn a full process relaunch.
        """
        self._push_activity_log(
            level="warn",
            message=f"engine restart: {reason}",
            pulse_sec=0,
        )
        try:
            self._persist_runtime()
        except Exception:
            pass
        self._stop.set()
        self.market.stop()
        old = self._thread
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=3.0)
        m_old = getattr(self.market, "_thread", None)
        if m_old is not None and m_old.is_alive() and m_old is not threading.current_thread():
            m_old.join(timeout=2.0)
        hung = bool(old and old.is_alive()) or bool(m_old and m_old.is_alive())
        if hung:
            return {
                "ok": False,
                "reason": reason,
                "need_relaunch": True,
                "detail": "thread_hung",
            }
        self._stop = threading.Event()
        self.market._stop = threading.Event()
        self.market._thread = None
        self._thread = None
        self._needs_bootstrap = True
        self.start()
        hb.beat(phase="restarted", reason=str(reason)[:80])
        return {"ok": True, "reason": reason, "need_relaunch": False}

    def reload_config(self) -> dict[str, Any]:
        self.config = load_config()
        self.market.resize_history(int(self.config.get("history_seconds", 23400)))
        return self.config

    def _chart_pair_from_open(self, scan_pair: dict[str, Any] | None) -> dict[str, Any] | None:
        """Lock charts to the open trade's CE/PE pair (premium-matched), not a random same-strike sibling."""
        # Flat / no open legs → always show scanner next-week pair (don't stick on 0DTE)
        if self.algo1.is_flat() and self.algo2.is_flat():
            return scan_pair
        sym = self.algo1.state.open_symbol
        if not sym:
            sym = self.algo2.state.ce_symbol or self.algo2.state.pe_symbol
        # Prefer the live scan pair when it already contains the open leg
        # (e.g. PE 22600 @135 paired with CE 22950 @133 — NOT same-strike CE @325)
        if scan_pair and sym:
            ce_s = (scan_pair.get("ce") or {}).get("tradingsymbol")
            pe_s = (scan_pair.get("pe") or {}).get("tradingsymbol")
            if sym in (ce_s, pe_s):
                return {**scan_pair, "locked_to_open": True}
        if self.algo2.state.ce_symbol and self.algo2.state.pe_symbol:
            ce_sym, pe_sym = self.algo2.state.ce_symbol, self.algo2.state.pe_symbol
        elif scan_pair and (scan_pair.get("ce") and scan_pair.get("pe")):
            # Single-leg algo1: keep scanner's other premium-band leg
            return {**scan_pair, "locked_to_open": True}
        elif sym:
            # Last resort only: same-strike sibling (may be far outside premium band)
            if sym.endswith("CE"):
                ce_sym, pe_sym = sym, sym[:-2] + "PE"
            elif sym.endswith("PE"):
                pe_sym, ce_sym = sym, sym[:-2] + "CE"
            else:
                return scan_pair
        else:
            return scan_pair

        meta = self.market.watch_meta
        ce_m = meta.get(ce_sym) or {}
        pe_m = meta.get(pe_sym) or {}
        # Ensure watched so series fills
        self.market.set_watch(
            [
                {
                    "tradingsymbol": ce_sym,
                    "instrument_token": int(ce_m.get("instrument_token") or 0),
                },
                {
                    "tradingsymbol": pe_sym,
                    "instrument_token": int(pe_m.get("instrument_token") or 0),
                },
            ],
            replace=False,
        )
        # Resolve tokens from universe / open trades if missing
        if not ce_m.get("instrument_token") or not pe_m.get("instrument_token"):
            for t in trade_store.open_trades():
                if t.get("symbol") == ce_sym and t.get("instrument_token"):
                    ce_m = {**ce_m, "instrument_token": t["instrument_token"], "strike": t.get("strike"), "expiry": t.get("expiry")}
                if t.get("symbol") == pe_sym and t.get("instrument_token"):
                    pe_m = {**pe_m, "instrument_token": t["instrument_token"], "strike": t.get("strike"), "expiry": t.get("expiry")}
        # Last resort: parse from scanner universe
        for leg in self.tracker.universe if hasattr(self.tracker, "universe") else []:
            if leg.get("tradingsymbol") == ce_sym:
                ce_m = {**ce_m, **leg}
            if leg.get("tradingsymbol") == pe_sym:
                pe_m = {**pe_m, **leg}

        if not ce_m.get("instrument_token") or not pe_m.get("instrument_token"):
            try:
                tok_map = getattr(self, "_nfo_token_map", None)
                if not tok_map:
                    from kite_client import get_authenticated_kite

                    kite = get_authenticated_kite()
                    tok_map = {
                        i["tradingsymbol"]: int(i["instrument_token"])
                        for i in kite.instruments("NFO")
                        if i.get("name") == "NIFTY"
                    }
                    self._nfo_token_map = tok_map
                if not ce_m.get("instrument_token") and ce_sym in tok_map:
                    ce_m = {**ce_m, "instrument_token": tok_map[ce_sym]}
                if not pe_m.get("instrument_token") and pe_sym in tok_map:
                    pe_m = {**pe_m, "instrument_token": tok_map[pe_sym]}
            except Exception:
                pass

        ce = {
            "tradingsymbol": ce_sym,
            "instrument_token": int(ce_m.get("instrument_token") or 0),
            "ltp": self.market.last_quotes.get(ce_sym),
            "strike": ce_m.get("strike"),
            "expiry": ce_m.get("expiry"),
            "instrument_type": "CE",
        }
        pe = {
            "tradingsymbol": pe_sym,
            "instrument_token": int(pe_m.get("instrument_token") or 0),
            "ltp": self.market.last_quotes.get(pe_sym),
            "strike": pe_m.get("strike"),
            "expiry": pe_m.get("expiry"),
            "instrument_type": "PE",
        }
        return {
            "id": f"{ce_sym}|{pe_sym}",
            "ce": ce,
            "pe": pe,
            "strike": ce.get("strike") or pe.get("strike"),
            "expiry": ce.get("expiry") or pe.get("expiry"),
            "gap": round(abs((ce.get("ltp") or 0) - (pe.get("ltp") or 0)), 2)
            if ce.get("ltp") is not None and pe.get("ltp") is not None
            else None,
            "locked_to_open": True,
        }

    def detach_closed_trades(self, trade_ids: list[str]) -> None:
        """Clear algo in-memory handles after manual square-off."""
        ids = set(trade_ids)
        if self.algo1.state.open_trade_id in ids:
            self.algo1.state.open_trade_id = None
            self.algo1.state.open_symbol = None
            self.algo1.state.entry_price = None
            self.algo1.state.phase = "SCAN"
            self.algo1.state.setup_id = None
            self.algo1.state.last_message = "manual square-off"
            self.release("algo1")
        a2 = self.algo2.state
        touched = False
        for attr in ("ce_trade_id", "pe_trade_id", "breakout_trade_id", "fallen_trade_id"):
            if getattr(a2, attr, None) in ids:
                setattr(a2, attr, None)
                touched = True
        if touched and not a2.ce_trade_id and not a2.pe_trade_id and not a2.fallen_trade_id:
            a2.phase = "SCAN"
            a2.breakout_side = None
            a2.fallen_side = None
            a2.breakout_peak = None
            a2.fallen_low = None
            a2.setup_id = None
            a2.last_message = "manual square-off"
            self.release("algo2")
        elif touched:
            a2.last_message = "manual square-off (partial)"
        try:
            self._persist_runtime()
        except Exception:
            pass

    def square_off_trades(self, trade_ids: list[str] | None = None, *, all_open: bool = False) -> dict[str, Any]:
        from foxtrade import broker

        cfg = load_config()
        if all_open:
            trade_ids = [t["id"] for t in trade_store.open_trades()]
        trade_ids = list(dict.fromkeys(trade_ids or []))
        # Never attempt closed ids — broker skips them; filter upfront for clarity
        open_ids = {t["id"] for t in trade_store.open_trades()}
        only_open = [tid for tid in trade_ids if tid in open_ids]
        skipped_closed = [tid for tid in trade_ids if tid not in open_ids]
        result = broker.square_off_many(
            only_open,
            reason="manual square-off",
            lot_size=int(cfg.get("lot_size", 75)),
        )
        ok_ids = [r["id"] for r in result.get("squared") or []]
        soft = [
            r
            for r in (result.get("squared") or [])
            if r.get("pnl_booked_without_broker_confirm")
        ]
        if ok_ids:
            self.detach_closed_trades(ok_ids)
            msg = (
                f"Manual square-off: {len(ok_ids)} leg(s) at market"
                + (" (live)" if cfg.get("live_trading") else " (paper)")
            )
            if soft:
                codes = [
                    (r.get("diagnosis") or {}).get("code") or "broker_fail" for r in soft
                ]
                msg += f" · {len(soft)} booked @LTP without Kite confirm ({', '.join(codes)})"
            with self.lock:
                self.notifications.append(
                    {"ts": datetime.now(IST).isoformat(), "message": msg}
                )
        result["skipped_closed_ids"] = skipped_closed
        result["live_trading"] = bool(cfg.get("live_trading"))
        return result

    def _rescan(self, *, dig_history: bool | None = None) -> None:
        """Refresh universe; dig 09:15 history when late login."""
        cfg = self.config
        self.market.dual_spot = bool(cfg.get("dual_mode", False))
        do_dig = dig_history if dig_history is not None else is_late_start(cfg)
        if cfg.get("dual_mode"):
            try:
                scans = self.dual.scan_all(cfg)
                # Primary UI scan = Nifty; Sensex attached under dual_scans
                self.scan = dict(scans.get("NIFTY") or {})
                self.scan["dual_scans"] = {
                    k: {
                        "spot": v.get("spot"),
                        "atm": v.get("atm"),
                        "current_expiry": v.get("current_expiry"),
                        "expiry_day": v.get("expiry_day"),
                        "pair": v.get("pair"),
                        "n_candidates": len(v.get("candidates") or []),
                        "error": v.get("error"),
                    }
                    for k, v in scans.items()
                }
                # Watch both books' candidates
                watch: list[dict[str, Any]] = []
                for uid, sc in scans.items():
                    exch = "BFO" if uid == "SENSEX" else "NFO"
                    for c in sc.get("candidates") or []:
                        watch.append({**c, "exchange": c.get("exchange") or exch})
                    pair = sc.get("pair") or {}
                    for leg in (pair.get("ce"), pair.get("pe")):
                        if isinstance(leg, dict) and leg.get("tradingsymbol"):
                            watch.append({**leg, "exchange": leg.get("exchange") or exch})
                open_inst = position_guard.open_watch_instruments()
                self.market.set_watch(watch, replace=False)
                self.market.set_watch(open_inst, replace=False)
            except Exception as exc:  # noqa: BLE001
                self.scan = {**self.scan, "error": f"dual rescan: {exc}"}
            self.last_scan_at = time.time()
            return

        self.scan = self.tracker.bootstrap(cfg, dig_history=do_dig)
        instruments = self.tracker.instruments_to_watch()
        open_inst = position_guard.open_watch_instruments()
        self.market.set_watch(instruments, replace=False)
        self.market.set_watch(open_inst, replace=False)
        if do_dig and not getattr(self, "_seeded_history", False):
            try:
                # Seed OPEN symbols first (survive restart), then scanner universe
                merged: dict[str, dict[str, Any]] = {}
                for i in list(open_inst) + list(instruments):
                    sym = i.get("tradingsymbol")
                    if not sym:
                        continue
                    prev = merged.get(sym) or {}
                    tok = int(i.get("instrument_token") or 0) or int(prev.get("instrument_token") or 0)
                    merged[sym] = {**prev, **i, "instrument_token": tok}
                seedable = [v for v in merged.values() if int(v.get("instrument_token") or 0) > 0]
                # Prefer opens at the front for late_start_max_legs truncation
                open_syms = {i.get("tradingsymbol") for i in open_inst}
                seedable.sort(key=lambda v: 0 if v.get("tradingsymbol") in open_syms else 1)
                n = seed_series_from_history(self.market, seedable, cfg)
                self._seeded_history = True
                self.scan["history_seeded_legs"] = n
            except Exception as exc:  # noqa: BLE001
                self.scan["history_seed_error"] = str(exc)
        self.last_scan_at = time.time()

    def _loop(self) -> None:
        candle_refresh = 0.0
        macro_refresh = 0.0
        fii_refresh = 0.0
        loop_i = 0
        while not self._stop.is_set():
            t0 = time.time()
            loop_i += 1
            try:
                open_n = len(trade_store.open_trades())
                fii = self.fii_oi or {}
                acts = fii.get("actions") or {}
                # Heartbeat first — supervisor restarts us if this goes stale
                hb.beat(
                    loop=loop_i,
                    phase="tick",
                    spot=self.market.last_spot,
                    auth_ok=not bool((getattr(self, "_auth_status", None) or {}).get("needs_login")),
                    open_count=open_n,
                    fii_oi_regime=fii.get("regime"),
                    fii_oi_smart=fii.get("smart_money"),
                    fii_block_ce=bool(acts.get("block_ce")),
                    fii_net=fii.get("fii_net"),
                )
                # Beep every 5s if Zerodha live link dead WHILE holding OPEN positions
                try:
                    if self.config.get("link_alert_enabled", True):
                        link_alert.tick(
                            open_count=open_n,
                            interval_sec=float(self.config.get("link_alert_beep_seconds", 5)),
                        )
                except Exception:
                    pass

                if getattr(self, "_needs_bootstrap", False):
                    self._bootstrap()
                    self._needs_bootstrap = False
                    if self.config.get("realtime_parity", True):
                        self._push_activity_log(
                            level="info",
                            message=(
                                "realtime_parity ON — entry→"
                                f"{self.config.get('entry_window_end')} "
                                f"persist={self.config.get('signal_persist_minutes')}m "
                                f"pd_confirm={self.config.get('post_decay_confirm_minutes')}m "
                                "bank_after_win strict"
                            ),
                        )
                    hb.set_meta(phase="running")

                self.config = load_config()
                # Re-bind OPEN positions every loop (trades.json truth — no memory loss)
                try:
                    self.algo1.ensure_bound_from_store(reason="engine loop")
                except Exception:
                    pass
                try:
                    if pos_integrity.open_for_algo("algo2") and self.algo2.is_flat():
                        self.algo2.restore_from_store(load_runtime())
                except Exception:
                    pass
                # Watch OPEN symbols before any entry logic
                try:
                    self.market.set_watch(position_guard.open_watch_instruments(), replace=False)
                except Exception:
                    pass
                # Force live off on weekends at config-use time
                if datetime.now(IST).weekday() >= 5 and self.config.get("live_trading"):
                    self.config["live_trading"] = False

                # Pre-market auto-login window (08:30–09:14 IST weekdays)
                now_ist = datetime.now(IST)
                mins = now_ist.hour * 60 + now_ist.minute
                if (
                    now_ist.weekday() < 5
                    and 8 * 60 + 30 <= mins < 9 * 60 + 15
                    and time.time() - getattr(self, "_last_premarket_login", 0) > 120
                ):
                    try:
                        self.refresh_auth(force_profile=True)
                    except Exception:
                        pass
                    self._last_premarket_login = time.time()

                # LTP is polled on market-hub thread — never block that with heavy Kite calls.
                # Do slow work after reading quotes, with isolated try/except.
                quotes = dict(self.market.last_quotes)
                if not quotes and self.market.last_spot is None:
                    # Bootstrap if market thread has not filled yet
                    try:
                        self.market.poll_once()
                        quotes = dict(self.market.last_quotes)
                    except Exception:
                        pass

                if time.time() - self.last_scan_at > 60:
                    try:
                        self._rescan(dig_history=False)
                    except Exception as exc:  # noqa: BLE001
                        self.scan = {**self.scan, "error": f"rescan: {exc}"}
                        self.last_scan_at = time.time()
                if time.time() - candle_refresh > 120:
                    # Hard-timeout inside refresh_nifty_candles — still mark due time so we don't spin
                    try:
                        self.market.refresh_nifty_candles()
                        if self.config.get("dual_mode"):
                            self.market.refresh_sensex_candles()
                    except Exception as exc:  # noqa: BLE001
                        self._push_activity_log(level="warn", message=f"candles: {exc}")
                    candle_refresh = time.time()
                if time.time() - getattr(self, "_last_auth_check", 0) > 180:
                    try:
                        need = bool((getattr(self, "_auth_status", None) or {}).get("needs_login"))
                        # Also re-check by date every cycle near session open
                        self.refresh_auth(force_profile=need or (mins < 9 * 60 + 20))
                    except Exception:
                        pass
                    self._last_auth_check = time.time()
                if time.time() - macro_refresh > 120:
                    try:
                        self.macro = get_macro(force=True)
                        # Macro already triggers FII scan; keep mirror on engine
                        if isinstance((self.macro or {}).get("factors", {}).get("fii_oi"), dict):
                            self.fii_oi = {
                                **(self.fii_oi or {}),
                                **(self.macro["factors"]["fii_oi"]),
                                "actions": (self.macro or {}).get("fii_oi_actions")
                                or (self.macro["factors"]["fii_oi"].get("actions") or {}),
                                "regime": (self.macro or {}).get("fii_oi_regime")
                                or self.macro["factors"]["fii_oi"].get("regime"),
                                "smart_money": (self.macro or {}).get("smart_money"),
                            }
                    except Exception as exc:  # noqa: BLE001
                        self.macro = {**(self.macro or {}), "error": str(exc)}
                    macro_refresh = time.time()
                # Dedicated FII/OI scan every 60s — save + gate (even if macro cache warm)
                if time.time() - fii_refresh > float(
                    self.config.get("fii_oi_scan_interval_sec", 60)
                ):
                    try:
                        prev_reg = (self.fii_oi or {}).get("regime")
                        self.fii_oi = scan_fii_oi(force=True, cfg=self.config)
                        reg = self.fii_oi.get("regime")
                        acts = self.fii_oi.get("actions") or {}
                        if reg and reg != prev_reg and reg not in ("unknown", "chop"):
                            self._push_activity_log(
                                level="info",
                                message=(
                                    f"FII/OI {reg}: "
                                    f"block_ce={acts.get('block_ce')} "
                                    f"hint={acts.get('side_hint')}"
                                ),
                            )
                        # Merge into macro so algo1 sees fresh actions next tick
                        self.macro = {
                            **(self.macro or {}),
                            "fii_oi_regime": reg,
                            "fii_oi_actions": acts,
                            "smart_money": self.fii_oi.get("smart_money")
                            or (self.macro or {}).get("smart_money"),
                            "block_ce": bool(acts.get("block_ce"))
                            or bool((self.macro or {}).get("block_ce")),
                            "caution_ce": bool(acts.get("caution_ce")),
                            "side_hint": acts.get("side_hint")
                            or (self.macro or {}).get("side_hint"),
                        }
                        try:
                            adj = float((self.macro or {}).get("confidence_adj") or 0)
                            adj += float(acts.get("confidence_adj") or 0)
                            self.macro["confidence_adj"] = round(adj, 3)
                        except Exception:
                            pass
                    except Exception as exc:  # noqa: BLE001
                        self.fii_oi = {**(self.fii_oi or {}), "error": str(exc)}
                    fii_refresh = time.time()

                # After 15:30 IST: one EOD reset/day (square leftovers + clear ghost locks)
                now_ist = datetime.now(IST)
                eod_raw = str(self.config.get("eod_reset_time", "15:30"))
                try:
                    eod_h, eod_m = map(int, eod_raw.split(":")[:2])
                except Exception:
                    eod_h, eod_m = 15, 30
                eod_mins = eod_h * 60 + eod_m
                now_mins = now_ist.hour * 60 + now_ist.minute
                day_key = now_ist.date().isoformat()
                if (
                    now_ist.weekday() < 5
                    and now_mins >= eod_mins
                    and getattr(self, "_eod_reset_day", None) != day_key
                ):
                    self._eod_reset_day = day_key
                    try:
                        self._run_eod_reset()
                    except Exception as exc:  # noqa: BLE001
                        with self.lock:
                            self.notifications.append(
                                {
                                    "ts": now_ist.isoformat(),
                                    "message": f"EOD reset error: {exc}",
                                }
                            )

                # After market hours, run daily self-learn once
                if (
                    now_ist.weekday() < 5
                    and now_ist.hour >= 15
                    and now_ist.minute >= 35
                    and time.time() - self._last_learn_try > 3600
                ):
                    self._last_learn_try = time.time()
                    try:
                        run_daily_learn(force=False)
                    except Exception:
                        pass
                index = self.market.index_context()
                trade_flat = self.algo1.is_flat() and self.algo2.is_flat()

                # Pre-score decay on current active hist (for rotate decision)
                pair_preview = self.tracker.active
                ce_hist = pe_hist = []
                if pair_preview:
                    ce_hist = [
                        p["value"]
                        for p in self.market.series_payload(pair_preview["ce"]["tradingsymbol"])
                    ]
                    pe_hist = [
                        p["value"]
                        for p in self.market.series_payload(pair_preview["pe"]["tradingsymbol"])
                    ]
                spot_hist = self.market.spot_values()
                decay_state = self.decay.update(
                    cfg=self.config,
                    index=index,
                    macro=self.macro,
                    ce_hist=ce_hist,
                    pe_hist=pe_hist,
                    spot_hist=spot_hist,
                )

                # Dynamic pair selection across all tracked strikes
                decay_rotate = bool(
                    decay_state.is_decay_day
                    and trade_flat
                    and pair_preview
                    and not self.tracker.converged_once.get(self.tracker.active_id or "")
                )
                self.scan = self.tracker.on_quotes(
                    quotes,
                    self.config,
                    decay_active_pair=decay_rotate,
                    trade_flat=trade_flat,
                    index=index,
                )
                # Ensure newly discovered legs + open position symbols are watched
                self.market.set_watch(self.tracker.instruments_to_watch(), replace=False)
                self.market.set_watch(position_guard.open_watch_instruments(), replace=False)
                quotes = dict(self.market.last_quotes)
                position_guard.update_mtm(quotes, self.config)

                pair = self.scan.get("pair")
                if pair:
                    ce_hist = [
                        p["value"] for p in self.market.series_payload(pair["ce"]["tradingsymbol"])
                    ]
                    pe_hist = [
                        p["value"] for p in self.market.series_payload(pair["pe"]["tradingsymbol"])
                    ]

                pair_regime = None
                if pair and ce_hist and pe_hist:
                    ce_ref = pair["ce"].get("ltp") or (ce_hist[0] if ce_hist else None)
                    pe_ref = pair["pe"].get("ltp") or (pe_hist[0] if pe_hist else None)
                    if self.algo2.state.ce_entry and self.algo2.state.pe_entry:
                        ce_ref = self.algo2.state.ce_entry
                        pe_ref = self.algo2.state.pe_entry
                    elif self.algo1.state.entry_price and self.algo1.state.open_symbol:
                        if self.algo1.state.open_symbol.endswith("CE"):
                            ce_ref = self.algo1.state.entry_price
                        else:
                            pe_ref = self.algo1.state.entry_price
                    pair_regime = evaluate_pair_regime(
                        ce_ltp=ce_hist[-1],
                        pe_ltp=pe_hist[-1],
                        ce_ref=ce_ref,
                        pe_ref=pe_ref,
                        ce_hist=ce_hist,
                        pe_hist=pe_hist,
                        index=index,
                        decay_score=decay_state.score,
                    )
                    # If pair itself is decaying hard after converge attempt, rotate next set
                    if (
                        trade_flat
                        and pair_regime.decaying
                        and self.tracker.active_id
                        and not self.tracker.converged_once.get(self.tracker.active_id)
                    ):
                        self.tracker.mark_decay_active()
                        self.scan = self.tracker.on_quotes(
                            quotes,
                            self.config,
                            decay_active_pair=True,
                            trade_flat=True,
                            index=index,
                        )
                        pair = self.scan.get("pair")

                if trade_flat:
                    with self.lock:
                        self._setup_owner = None

                # Persist CE/PE day bars + compute wait/enter-after-decay signal
                post_decay: dict[str, Any] = {}
                try:
                    if pair:
                        ce_sym = pair["ce"]["tradingsymbol"]
                        pe_sym = pair["pe"]["tradingsymbol"]
                        self.decay_memory.update_live(
                            ce=quotes.get(ce_sym),
                            pe=quotes.get(pe_sym),
                            spot=index.get("spot"),
                            strike=pair.get("strike"),
                            expiry=str(pair.get("expiry") or "")[:10] or None,
                            symbol_ce=ce_sym,
                            symbol_pe=pe_sym,
                            spot_open=index.get("day_open")
                            or index.get("spot_open")
                            or index.get("open"),
                            spot_high=index.get("day_high") or index.get("high"),
                            spot_low=index.get("day_low") or index.get("low"),
                        )
                    sig = self.decay_memory.analyze(self.config)
                    post_decay = sig.as_dict()
                    self._post_decay = post_decay
                except Exception as exc:  # noqa: BLE001
                    post_decay = {"action": "NONE", "reason": f"memory err: {exc}"}
                    self._post_decay = post_decay

                ctx: dict[str, Any] = {
                    "config": self.config,
                    "pair": pair,
                    "scan": self.scan,
                    "quotes": quotes,
                    "index": index,
                    "macro": self.macro,
                    "pair_regime": pair_regime,
                    "in_session": _in_session(self.config),
                    "notifications": [],
                    "try_acquire": self.try_acquire,
                    "release": self.release,
                    "decay_block": self.decay.block_entries(),
                    "decay_msg": decay_state.last_message,
                    "post_decay": post_decay,
                    "market": self.market,
                }

                if self.config.get("dual_mode"):
                    spot_closes = {
                        "NIFTY": self.market.session_closes_from_candles("NIFTY"),
                        "SENSEX": self.market.session_closes_from_candles("SENSEX"),
                    }
                    index_meta = {
                        "NIFTY": {
                            **index,
                            "spot": self.market.last_spot_by_id.get("NIFTY")
                            or index.get("spot"),
                            "day_open": self.market.day_open_by_id.get("NIFTY")
                            or index.get("day_open"),
                            "spot_closes": spot_closes["NIFTY"],
                        },
                        "SENSEX": {
                            "spot": self.market.last_spot_by_id.get("SENSEX"),
                            "day_open": self.market.day_open_by_id.get("SENSEX"),
                            "spot_closes": spot_closes["SENSEX"],
                        },
                    }
                    # Prefer dual_scans from last rescan; refresh lightly if empty
                    if not self.dual.last_scans:
                        try:
                            self.dual.scan_all(self.config)
                        except Exception:
                            pass
                    self.dual.on_tick(
                        base_cfg=self.config,
                        quotes=quotes,
                        spot_closes=spot_closes,
                        index_meta=index_meta,
                        ctx_common=ctx,
                        try_acquire=self.try_acquire,
                        release=self.release,
                    )
                    # Keep legacy algo1 pointer in sync
                    self.algo1 = self.dual.books["NIFTY"]
                else:
                    self.algo1.on_tick(ctx)
                self.algo2.on_tick(ctx)

                # Orphan OPEN legs (from prior runs) still get SL/target exits
                position_guard.manage_orphans(
                    quotes, self.config, self._managed_trade_ids(), pair_regime
                )

                if time.time() - getattr(self, "_last_runtime_save", 0) > 5:
                    self._persist_runtime()
                    self._last_runtime_save = time.time()

                if ctx["notifications"]:
                    with self.lock:
                        self.notifications.extend(ctx["notifications"])
                        self.notifications = self.notifications[-50:]
                        for n in ctx["notifications"]:
                            self._push_activity_log(
                                level=str(n.get("level") or "info"),
                                message=str(n.get("message") or n.get("reason") or ""),
                            )
                # Heartbeat line whenever Algo1 phase/message changes (+ pulse so UI never freezes)
                try:
                    a1 = self.algo1.state
                    line = (
                        f"{a1.phase} · {a1.predicted_side or '-'} "
                        f"{float(a1.confidence or 0):.0%} · {a1.last_message or ''}"
                    )
                    self._push_activity_log(level="tick", message=line, pulse_sec=10.0)
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.scan["error"] = str(exc)
                self._push_activity_log(level="error", message=f"engine: {exc}")
                hb.beat(phase="error", error=str(exc)[:200], loop=loop_i)
            # Cap one iteration — never let a hung call freeze forever (outer watchdog backs us)
            elapsed = time.time() - t0
            if elapsed > 25:
                hb.beat(phase="slow_tick", elapsed=round(elapsed, 1), loop=loop_i)
                self._push_activity_log(
                    level="warn",
                    message=f"slow tick {elapsed:.0f}s — check Kite/network",
                    pulse_sec=0,
                )
            time.sleep(max(0.05, float(self.config.get("poll_seconds", 1)) - elapsed))

    def dashboard_state(self) -> dict[str, Any]:
        cfg = load_config()
        if datetime.now(IST).weekday() >= 5:
            cfg = {**cfg, "live_trading": False}
        quotes = dict(self.market.last_quotes)
        pair = self.scan.get("pair")
        # While in a trade, lock chart pair to that strike (don't jump to another candidate)
        chart_pair = self._chart_pair_from_open(pair)
        a1 = self.algo1.snapshot(quotes)
        a2 = self.algo2.snapshot(quotes)
        screen_band = {
            "low": cfg["premium_low"],
            "high": cfg["premium_high"],
            "source": "screen",
        }

        series = {}
        display_pair = chart_pair or pair
        if display_pair:
            for leg in ("ce", "pe"):
                try:
                    sym = display_pair[leg]["tradingsymbol"]
                except Exception:
                    continue
                series[sym] = self.market.series_payload(sym)

        # Detected CE/PE overlap zone (algo still uses it; chart UI shows open-order LTP)
        from foxtrade.convergence_zone import detect_zone_from_day_history

        now_ts = time.time()
        cache = getattr(self, "_zone_cache", None)
        pid = (display_pair or {}).get("id")
        if (
            cache
            and cache.get("id") == pid
            and now_ts - float(cache.get("ts") or 0) < 45
            and cache.get("zone")
            and str((cache.get("zone") or {}).get("source", "")).startswith("detected")
        ):
            zone = cache["zone"]
        else:
            zone = a1.get("convergence_zone") or a2.get("convergence_zone")
            if not (zone and str(zone.get("source", "")).startswith("detected")) and display_pair:
                ce_sym = display_pair["ce"]["tradingsymbol"]
                pe_sym = display_pair["pe"]["tradingsymbol"]
                ce_v = [p["value"] for p in series.get(ce_sym) or []]
                pe_v = [p["value"] for p in series.get(pe_sym) or []]
                try:
                    zone = detect_zone_from_day_history(
                        int(display_pair["ce"]["instrument_token"]),
                        int(display_pair["pe"]["instrument_token"]),
                        gap_tol=float(cfg.get("convergence_zone_gap", 12.0)),
                        min_samples=int(cfg.get("convergence_zone_min_samples", 15)),
                        live_ce=ce_v,
                        live_pe=pe_v,
                    )
                except Exception:
                    zone = None
            self._zone_cache = {"id": pid, "ts": now_ts, "zone": zone}

        a1["screen_band"] = screen_band
        a2["screen_band"] = screen_band
        a1["convergence_band"] = zone if zone else screen_band
        a2["convergence_band"] = zone if zone else screen_band

        opens = trade_store.open_trades()
        for t in opens:
            px = quotes.get(t.get("symbol") or "")
            if px is not None and t.get("entry_price") is not None:
                t["live_price"] = px
                t["live_pnl_rupees"] = round(
                    (px - float(t["entry_price"])) * int(t.get("qty") or 0), 2
                )

        # Live order LTP series for dashboard chart (one line per OPEN trade)
        order_ltp: list[dict[str, Any]] = []
        for t in opens:
            sym = str(t.get("symbol") or "")
            if not sym:
                continue
            raw = self.market.series_payload(sym)
            series[sym] = raw
            entry_ts = None
            try:
                if t.get("opened_at"):
                    entry_ts = int(
                        datetime.fromisoformat(str(t["opened_at"]).replace("Z", "+00:00")).timestamp()
                    )
            except Exception:
                entry_ts = None
            entry_px = t.get("entry_price")
            clipped: list[dict[str, float]] = []
            if entry_ts is not None:
                for p in raw:
                    if int(p["time"]) >= entry_ts:
                        clipped.append(p)
                if entry_px is not None:
                    ep = float(entry_px)
                    if not clipped or int(clipped[0]["time"]) > entry_ts:
                        clipped = [{"time": entry_ts, "value": ep}, *clipped]
                    elif int(clipped[0]["time"]) == entry_ts:
                        clipped[0] = {"time": entry_ts, "value": ep}
            else:
                clipped = list(raw)
            # Always stamp latest LTP so the line moves even if series buffer is empty
            live = t.get("live_price")
            if live is not None:
                now_i = int(now_ts)
                if clipped and int(clipped[-1]["time"]) == now_i:
                    clipped[-1] = {"time": now_i, "value": float(live)}
                else:
                    clipped.append({"time": now_i, "value": float(live)})
            order_ltp.append(
                {
                    "id": t.get("id"),
                    "symbol": sym,
                    "side": t.get("side"),
                    "algo": t.get("algo"),
                    "qty": t.get("qty"),
                    "entry_price": entry_px,
                    "opened_at": t.get("opened_at"),
                    "live_price": live,
                    "live_pnl_rupees": t.get("live_pnl_rupees"),
                    "status": "OPEN",
                    "series": clipped,
                }
            )

        with self.lock:
            notes = list(self.notifications[-10:])
            owner = self._setup_owner

        # Live activity strip: what each algo is doing right now
        activity = self._activity_snapshot(a1, a2, opens, cfg)

        dual_status = {
            "enabled": bool(cfg.get("dual_mode")),
            "message": getattr(self.dual, "last_message", ""),
            "alloc": getattr(self.dual, "last_alloc", {}),
            "scores": {
                k: {
                    "score": (v or {}).get("score"),
                    "allow": (v or {}).get("allow"),
                    "side": (v or {}).get("side"),
                    "event": (v or {}).get("event"),
                    "reason": (v or {}).get("reason"),
                }
                for k, v in (getattr(self.dual, "last_scores", {}) or {}).items()
            },
            "opens": self.dual.open_count_by_underlying(),
            "spots": {
                "NIFTY": self.market.last_spot_by_id.get("NIFTY"),
                "SENSEX": self.market.last_spot_by_id.get("SENSEX"),
            },
            "scans": (self.scan or {}).get("dual_scans") or {},
        }

        return {
            "config": cfg,
            "server_time": datetime.now(IST).isoformat(),
            "in_session": _in_session(cfg),
            "market_error": self.market.error,
            "scan_error": self.scan.get("error"),
            "spot": self.market.last_spot or self.scan.get("spot"),
            "dual_book": dual_status,
            "activity": activity,
            "activity_log": list(getattr(self, "_activity_log", [])[-80:]),
            "engine_running": True,
            "scan": {
                "current_expiry": self.scan.get("current_expiry"),
                "next_expiry": self.scan.get("next_expiry"),
                "expiry_day": self.scan.get("expiry_day"),
                "pair": display_pair or pair,
                "scan_pair": pair,
                "candidates": self.scan.get("candidates") or [],
                "pairs": self.scan.get("pairs") or [],
                "tracked_legs": self.scan.get("tracked_legs"),
                "tracked_pairs": self.scan.get("tracked_pairs"),
                "switch_reason": self.scan.get("switch_reason"),
                "active_id": self.scan.get("active_id"),
                "locked_merge": self.scan.get("locked_merge"),
                "converged_once_ids": self.scan.get("converged_once_ids") or [],
                "decayed_ids": self.scan.get("decayed_ids") or [],
                "late_start": self.scan.get("late_start"),
                "late_report": self.scan.get("late_report"),
                "history_seeded_legs": self.scan.get("history_seeded_legs"),
            },
            "algo1": a1,
            "algo2": a2,
            "series": series,
            "order_ltp": order_ltp,
            "open_trades": opens,
            # Day book only — prior days stay in data/trades.json for Reports
            "all_trades": trade_store.today_closed_trades()[-100:],
            "day_trades": trade_store.today_entries_for_dashboard(),
            "history_total": len(trade_store.list_trades()),
            "pnl": trade_store.pnl_summary(),
            "notifications": notes,
            "auth": getattr(self, "_auth_status", None),
            "heartbeat": {
                "age_sec": hb.age_seconds(),
                "alive": hb.is_alive(float(cfg.get("heartbeat_max_age_sec", 90))),
                **(hb.read_heartbeat() or {}),
            },
            "unattended": {
                "enabled": bool(cfg.get("unattended_mode", True)),
                "auto_login": bool(cfg.get("unattended_auto_login", True)),
            },
            "position_integrity": {
                "open_count": len(opens),
                "block_new_while_any_open": bool(cfg.get("block_new_while_any_open", True)),
                "can_open_algo1": pos_integrity.can_open_new("algo1", cfg=cfg)[0],
                "open_symbols": [t.get("symbol") for t in opens],
            },
            "link_alert": link_alert.snapshot(),
            "stall_alert": stall_alert.snapshot(),
            "entry_window": {
                "start": cfg.get("entry_window_start") or cfg.get("session_start", "09:15"),
                "end": cfg.get("entry_window_end") or cfg.get("session_end", "15:15"),
                "in_window": _in_session(cfg),
                "no_entry_after_minutes": int(cfg.get("no_entry_after_minutes", 300)),
                "realtime_parity": bool(cfg.get("realtime_parity", True)),
                "signal_persist_minutes": int(cfg.get("signal_persist_minutes", 2)),
                "post_decay_confirm_minutes": int(
                    cfg.get("post_decay_confirm_minutes", 2)
                ),
            },
            "index": self.market.index_context(),
            "macro": self.macro,
            "fii_oi": self.fii_oi or {},
            "decay": {
                "observing": self.decay.state.observing,
                "is_decay_day": self.decay.state.is_decay_day,
                "score": round(self.decay.state.score, 3),
                "reasons": self.decay.state.reasons,
                "message": self.decay.state.last_message,
                "block_entries": self.decay.block_entries(),
            },
            "post_decay": getattr(self, "_post_decay", {}) or {},
            "decay_memory": self.decay_memory.snapshot(),
            "pair_regime": self._regime_payload(),
            "setup_owner": owner,
        }

    def _push_activity_log(
        self, *, level: str, message: str, pulse_sec: float = 0.0
    ) -> None:
        """Append a UI log line. Safe to call while self.lock is held (no re-lock)."""
        msg = (message or "").strip()
        if not msg:
            return
        now = time.time()
        # Deduplicate identical tick lines, but pulse every N sec so UI proves life
        if level == "tick" and msg == getattr(self, "_last_activity_line", ""):
            last_pulse = float(getattr(self, "_last_activity_pulse", 0) or 0)
            if pulse_sec <= 0 or now - last_pulse < pulse_sec:
                return
            msg = f"{msg} · alive"
        if level == "tick":
            self._last_activity_line = message.strip()
            self._last_activity_pulse = now
        row = {
            "ts": datetime.now(IST).isoformat(),
            "level": level,
            "message": msg[:240],
        }
        log = getattr(self, "_activity_log", None)
        if log is None:
            self._activity_log = []
            log = self._activity_log
        log.append(row)
        if len(log) > 120:
            del log[:-120]

    def _activity_snapshot(
        self,
        a1: dict[str, Any],
        a2: dict[str, Any],
        opens: list[dict[str, Any]],
        cfg: dict[str, Any],
    ) -> dict[str, Any]:
        """Compact live status for dashboard: scanning / waiting / buying / selling / holding."""
        in_win = _in_session(cfg)
        live = bool(cfg.get("live_trading"))
        mode_lbl = "LIVE" if live else "PAPER"

        def _leg(algo: str, snap: dict[str, Any]) -> dict[str, Any]:
            phase = str(snap.get("phase") or "—").upper()
            msg = str(snap.get("message") or snap.get("last_message") or "")
            open_for = [t for t in opens if t.get("algo") == algo]
            mtm = round(sum(float(t.get("live_pnl_rupees") or 0) for t in open_for), 2)
            invested = round(
                sum(
                    float(t["invested"])
                    if t.get("invested") is not None
                    else float(t.get("entry_price") or 0) * int(t.get("qty") or 0)
                    for t in open_for
                ),
                2,
            )
            if open_for:
                state = "HOLDING"
                if "exit" in msg.lower() or "square" in msg.lower() or "sell" in msg.lower():
                    state = "SELLING"
            elif phase in ("BUY", "ENTER", "ENTRY", "FIRE"):
                state = "BUYING"
            elif phase in ("WAIT", "ARMED", "CONVERGED", "WATCH"):
                state = "WAITING"
            elif not in_win:
                state = "OUT_OF_WINDOW"
            else:
                state = "SCANNING"
            return {
                "algo": algo,
                "phase": phase,
                "state": state,
                "message": msg[:160],
                "predicted_side": snap.get("predicted_side") or snap.get("side"),
                "confidence": snap.get("confidence"),
                "open_count": len(open_for),
                "open_mtm": mtm,
                "invested": invested,
                "symbols": [t.get("symbol") for t in open_for],
            }

        open_mtm = round(sum(float(t.get("live_pnl_rupees") or 0) for t in opens), 2)
        open_invested = round(
            sum(
                float(t["invested"])
                if t.get("invested") is not None
                else float(t.get("entry_price") or 0) * int(t.get("qty") or 0)
                for t in opens
            ),
            2,
        )
        headline_parts = []
        for leg in (_leg("algo1", a1), _leg("algo2", a2)):
            if not cfg.get(f"{leg['algo']}_enabled", True) and leg["algo"] == "algo2":
                continue
            if leg["algo"] == "algo1" and not cfg.get("algo1_enabled", True):
                continue
            headline_parts.append(f"{leg['algo'].upper()} {leg['state']}")
        return {
            "mode": mode_lbl,
            "in_window": in_win,
            "headline": " · ".join(headline_parts) or "IDLE",
            "open_count": len(opens),
            "open_mtm": open_mtm,
            "open_invested": open_invested,
            "algo1": _leg("algo1", a1),
            "algo2": _leg("algo2", a2),
            "recent": [
                {"ts": n.get("ts"), "message": n.get("message")}
                for n in (getattr(self, "notifications", None) or [])[-8:]
            ],
        }

    def _regime_payload(self) -> dict[str, Any] | None:
        # recompute light snapshot for UI
        pair = self.scan.get("pair")
        if not pair:
            return None
        ce_hist = [p["value"] for p in self.market.series_payload(pair["ce"]["tradingsymbol"])]
        pe_hist = [p["value"] for p in self.market.series_payload(pair["pe"]["tradingsymbol"])]
        if not ce_hist or not pe_hist:
            return None
        ce_ref = pair["ce"].get("ltp") or ce_hist[0]
        pe_ref = pair["pe"].get("ltp") or pe_hist[0]
        if self.algo2.state.ce_entry and self.algo2.state.pe_entry:
            ce_ref, pe_ref = self.algo2.state.ce_entry, self.algo2.state.pe_entry
        reg = evaluate_pair_regime(
            ce_ltp=ce_hist[-1],
            pe_ltp=pe_hist[-1],
            ce_ref=ce_ref,
            pe_ref=pe_ref,
            ce_hist=ce_hist,
            pe_hist=pe_hist,
            index=self.market.index_context(),
            decay_score=self.decay.state.score,
        )
        return {
            "regime": reg.regime,
            "winner": reg.winner,
            "loser": reg.loser,
            "ce_mult": round(reg.ce_mult or 0, 2),
            "pe_mult": round(reg.pe_mult or 0, 2),
            "crush_level": reg.crush_level,
            "expand_mult": round(reg.expand_mult or 0, 2),
            "range_bound": reg.range_bound,
            "decaying": reg.decaying,
            "near_support": reg.near_support,
            "near_resistance": reg.near_resistance,
            "advice": reg.advice,
            "reasons": reg.reasons,
        }


ENGINE = TradingEngine()

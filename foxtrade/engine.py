from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade import trade_store
from foxtrade.algo_ids import matches
from foxtrade.algo1_breakout import Algo1Breakout
from foxtrade.algo1_convergence import Algo1ConvergenceDual
from foxtrade.algo_cw_dual import AlgoCwDual
from foxtrade.atm_channel import AtmChannelBook, channel_enabled
from foxtrade.pe_range import PeRangeBook, pe_range_enabled
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
BUILD_ID = "algo_cw_nw_v2_20261123_stallfix"


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


def kite_idle_now(cfg: dict[str, Any] | None = None) -> bool:
    """
    True when the trading engine should stop talking to Zerodha.

    Default: weekdays after kite_idle_after (15:40 IST), and all weekend.
    Reports / local DB / dashboard stay available via Flask.
    """
    cfg = cfg or {}
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return True
    raw = str(cfg.get("kite_idle_after") or "15:40")
    hh, mm = _parse_hhmm(raw, 15, 40)
    return (now.hour * 60 + now.minute) >= (hh * 60 + mm)


class TradingEngine:
    def __init__(self) -> None:
        self.config = load_config()
        self.market = MarketHub(history_seconds=int(self.config.get("history_seconds", 23400)))
        self.market.dual_spot = bool(self.config.get("dual_mode", False))
        self.dual = DualBook()
        self.algo1 = self.dual.books["NIFTY"]  # dashboard / legacy alias (breakout)
        self.algo1_conv = Algo1ConvergenceDual()  # Algo1 dual+day-type (does not alter Algo-CW)
        self.algo_cw = AlgoCwDual()
        self.algo_nw = AlgoCwDual(
            algo_id="algo_nw",
            display_name="Algo-NW",
            week_mode="next",
        )
        self.scan_nw: dict[str, Any] = {}
        self._last_nw_scan_at: float = 0.0
        self.atm_channel = AtmChannelBook()
        self.pe_range = PeRangeBook()
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
        self._auth_status: dict[str, Any] | None = None
        # UI Logout: skip TOTP auto-login until Login / callback / process restart
        self._suppress_auto_login: bool = False
        # After 15:40 IST — no Zerodha; reports-only
        self._kite_idle: bool = False

    def apply_manual_logout(self, status: dict[str, Any] | None = None) -> None:
        from kite_client import set_manual_logout

        self._suppress_auto_login = True
        set_manual_logout(True)
        self._auth_status = status or {
            "ok": False,
            "needs_login": True,
            "reason": "logged_out",
            "user_name": None,
            "user_id": None,
            "saved_at": None,
            "logged_out": True,
            "auto_login": "suppressed_manual_logout",
        }
        # Drop live market poll so quotes aren't served from a dead session
        try:
            self.market.stop()
        except Exception:
            pass
        with self.lock:
            self.notifications.append(
                {
                    "ts": datetime.now(IST).isoformat(),
                    "message": "Kite session logged out from dashboard",
                }
            )
            self.notifications = self.notifications[-50:]

    def clear_manual_logout(self) -> None:
        from kite_client import set_manual_logout

        self._suppress_auto_login = False
        set_manual_logout(False)
        # Resume market hub after Login (logout had stopped it)
        try:
            if not self.is_kite_idle():
                self.market.start()
        except Exception:
            pass

    def is_kite_idle(self) -> bool:
        return bool(getattr(self, "_kite_idle", False))

    def _enter_kite_idle(self, *, reason: str = "after 15:40 IST") -> None:
        if getattr(self, "_kite_idle", False):
            return
        self._kite_idle = True
        try:
            self.market.stop()
        except Exception:
            pass
        try:
            from foxtrade.option_chain_db import CHAIN_RECORDER

            CHAIN_RECORDER.stop()
        except Exception:
            pass
        try:
            link_alert.mute()
        except Exception:
            pass
        msg = f"Kite idle — {reason}. Engine stopped; Reports still work."
        self.algo1.state.last_message = "AFTER MARKET · reports only"
        self.algo_cw.state.last_message = "AFTER MARKET · reports only"
        self.algo_nw.state.last_message = "AFTER MARKET · reports only"
        self._push_activity_log(level="info", message=msg, pulse_sec=0)
        with self.lock:
            self.notifications.append(
                {"ts": datetime.now(IST).isoformat(), "message": msg}
            )
            self.notifications = self.notifications[-50:]

    def _exit_kite_idle(self) -> None:
        if not getattr(self, "_kite_idle", False):
            return
        self._kite_idle = False
        try:
            self.market.start()
        except Exception:
            pass
        try:
            from foxtrade.option_chain_db import CHAIN_RECORDER

            CHAIN_RECORDER.start(self.config)
        except Exception:
            pass
        self._needs_bootstrap = True
        self._push_activity_log(
            level="info",
            message="Kite active — engine resumed (pre/market hours)",
            pulse_sec=0,
        )

    def try_acquire(self, algo: str) -> bool:
        if not self.config.get("exclusive_entries", True):
            return True
        # CW + NW convergence books may both hold independently
        if algo == "algo_nw":
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
        if self._thread and self._thread.is_alive():
            return
        self.config = load_config()
        opens = trade_store.open_trades()
        idle_boot = kite_idle_now(self.config) and not opens

        self._stop.clear()
        self._last_learn_try = 0.0
        self._last_runtime_save = 0.0
        self._eod_reset_day: str | None = None
        # After 15:40 / weekend with no OPEN legs: reports-only — do not touch Zerodha.
        self._needs_bootstrap = not idle_boot
        self._kite_idle = bool(idle_boot)

        logged_out = False
        try:
            from kite_client import is_manual_logout

            logged_out = bool(is_manual_logout())
        except Exception:
            logged_out = False
        # Logout / after-hours: do not spin market-hub (avoids auth hang + false data alerts)
        if not idle_boot and not logged_out:
            self.market.start()

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
            if self.algo_cw.restore_from_store(runtime):
                restored.append("algo_cw")
            if self.algo_nw.restore_from_store(runtime):
                restored.append("algo_nw")
            if not idle_boot:
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
        # Independent HB pulse — survives slow Kite calls so live never looks dead
        try:
            hb.start_pulse(interval_sec=5.0)
        except Exception:
            pass

        if idle_boot:
            self.algo1.state.last_message = "AFTER MARKET · reports only"
            self.algo_cw.state.last_message = "AFTER MARKET · reports only"
            self.algo_nw.state.last_message = "AFTER MARKET · reports only"
            self.notifications.append(
                {
                    "ts": datetime.now(IST).isoformat(),
                    "message": (
                        f"Kite idle after {self.config.get('kite_idle_after', '15:40')} — "
                        "engine stopped; Reports / local DB still available"
                    ),
                }
            )
        else:
            try:
                from foxtrade.kite_cache import prefetch_async

                prefetch_async("NFO")
                prefetch_async("BFO")
            except Exception:
                pass
            # Persist current+next week CE/PE (ATM±10) every 10s + OI every 1m
            try:
                from foxtrade.option_chain_db import CHAIN_RECORDER

                CHAIN_RECORDER.start(self.config)
            except Exception as exc:  # noqa: BLE001
                self.notifications.append(
                    {
                        "ts": datetime.now(IST).isoformat(),
                        "message": f"option_chain_db start failed: {exc}",
                    }
                )

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
            self.algo_cw._sync_flat_if_no_opens(self.release)
        except Exception:
            if not any(matches(t.get("algo"), "algo_cw") for t in opens):
                self.algo_cw._clear(self.release)
                self.algo_cw.state.last_message = "cleared ghost Algo-CW — no open trade"
        try:
            self.algo_nw._sync_flat_if_no_opens(self.release)
        except Exception:
            if not any(matches(t.get("algo"), "algo_nw") for t in opens):
                self.algo_nw._clear(self.release)
                self.algo_nw.state.last_message = "cleared ghost Algo-NW — no open trade"

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
        self.algo_cw._clear(self.release)
        self.algo_cw.state.last_message = "EOD reset — ready for next session"
        self.algo_nw._clear(self.release)
        self.algo_nw.state.last_message = "EOD reset — ready for next session"
        self.scan_nw = {}
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

        # Overnight / after 15:40 — don't hit Zerodha; reports use local data only.
        if kite_idle_now(self.config) and not trade_store.open_trades():
            from kite_client import load_token_meta

            prev = getattr(self, "_auth_status", None) or {}
            meta = load_token_meta() or {}
            status = {
                "ok": True,
                "needs_login": False,
                "reason": "kite_idle",
                "user_name": prev.get("user_name"),
                "user_id": prev.get("user_id"),
                # Always surface last login time from disk (idle stub used to drop it)
                "saved_at": prev.get("saved_at") or meta.get("saved_at"),
                "kite_idle": True,
                "auto_login": "idle_skip",
            }
            self._auth_status = status
            return status

        from kite_client import is_manual_logout

        # Survive process restart — flag file is source of truth for UI Logout
        if is_manual_logout():
            self._suppress_auto_login = True

        status = check_session(force_profile=force_profile)
        # Unattended: try TOTP auto-login when session missing/stale
        # (skipped after explicit UI Logout until Login again)
        suppressed = bool(
            getattr(self, "_suppress_auto_login", False) or is_manual_logout()
        )
        if (
            status.get("needs_login")
            and self.config.get("unattended_auto_login", True)
            and not suppressed
        ):
            try:
                from foxtrade.auto_login import ensure_kite_session

                status = ensure_kite_session(force_login=True)
            except Exception as exc:  # noqa: BLE001
                status = {**status, "auto_login": f"err:{exc}"}
        if suppressed and status.get("needs_login"):
            status = {
                **status,
                "ok": False,
                "needs_login": True,
                "logged_out": True,
                "auto_login": "suppressed_manual_logout",
                "reason": status.get("reason") or "logged_out",
            }
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
            status = self.refresh_auth(force_profile=True)
            # Ledger truth vs Zerodha truth: a phantom live OPEN row blocks all
            # new entries, so reconcile once per boot before managing anything.
            if not (status or {}).get("needs_login"):
                try:
                    from foxtrade import broker as _broker

                    fixed = _broker.reconcile_open_with_broker()
                    if fixed:
                        with self.lock:
                            self.notifications.append(
                                {
                                    "ts": datetime.now(IST).isoformat(),
                                    "message": (
                                        "startup reconcile: closed "
                                        f"{len(fixed)} phantom live leg(s) "
                                        "(broker flat)"
                                    ),
                                }
                            )
                            self.notifications = self.notifications[-50:]
                except Exception:
                    pass
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
            "algo_cw": self.algo_cw.runtime_snapshot(),
            "algo_nw": self.algo_nw.runtime_snapshot(),
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
        for book in (self.algo_cw, self.algo_nw):
            for tid in (
                book.state.ce_trade_id,
                book.state.pe_trade_id,
                book.state.breakout_trade_id,
                book.state.fallen_trade_id,
            ):
                if tid:
                    ids.add(tid)
        if getattr(self, "atm_channel", None) and self.atm_channel.state.open_trade_id:
            ids.add(self.atm_channel.state.open_trade_id)
        if getattr(self, "pe_range", None) and self.pe_range.state.open_trade_id:
            ids.add(self.pe_range.state.open_trade_id)
        # Disk truth: every OPEN strategy row is protected + managed
        for t in trade_store.open_trades():
            if t.get("algo") in (
                "algo1",
                "algo1_sx",
                "algo_cw",
                "algo_nw",
                "atm_channel",
                "pe_range",
            ) and t.get("id"):
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

        Loops capture their stop Event at start, so we can orphan a hung
        worker and start a fresh generation — critical during LIVE/OPEN
        when process kill is forbidden.
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

        # Always orphan+respawn when hung — NEVER ask for process kill here.
        # Full bootstrap on every restart was the death spiral (tick blocked >60s).
        self._stop = threading.Event()
        self.market._stop = threading.Event()
        self.market._thread = None
        self._thread = None
        # Clear inflight flags so a hung bg thread cannot block the new loop forever
        self._bootstrap_inflight = False
        self._rescan_inflight = False
        self._candles_inflight = False
        self._macro_inflight = False
        self._auth_inflight = False
        self._nw_scan_inflight = False
        self._fii_inflight = False
        self._dual_scan_inflight = False
        self._needs_bootstrap = False
        # Seed tick_ts BEFORE start — watchdog must not see the pre-hang age
        hb.seed_alive(reason=f"restart:{reason}")
        self.start()
        # Re-seed after start in case start() took a few seconds
        hb.seed_alive(reason=f"restart_ok:{reason}")
        hb.beat(phase="restarted", reason=str(reason)[:80])
        detail = "orphaned_hung_thread" if hung else "clean"
        if hung:
            self._push_activity_log(
                level="warn",
                message=f"orphaned hung engine thread — new generation ({detail})",
                pulse_sec=0,
            )
        return {
            "ok": True,
            "reason": reason,
            "need_relaunch": False,
            "detail": detail,
        }

    def reload_config(self) -> dict[str, Any]:
        self.config = load_config()
        self.market.resize_history(int(self.config.get("history_seconds", 23400)))
        try:
            from foxtrade.option_chain_db import CHAIN_RECORDER

            CHAIN_RECORDER.stop()
            CHAIN_RECORDER.start(self.config)
        except Exception:
            pass
        return self.config

    def _option_chain_db_snapshot(self) -> dict[str, Any]:
        try:
            from foxtrade.option_chain_db import CHAIN_RECORDER

            # Skip heavy DB stats after hours — keeps Reports UI snappy without Zerodha.
            include_stats = not (
                getattr(self, "_kite_idle", False) or kite_idle_now(self.config)
            )
            return CHAIN_RECORDER.snapshot(include_db_stats=include_stats)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _chart_pair_from_open(self, scan_pair: dict[str, Any] | None) -> dict[str, Any] | None:
        """Lock charts to the open trade's CE/PE pair (premium-matched), not a random same-strike sibling."""
        # Flat / no open legs → always show scanner next-week pair (don't stick on 0DTE)
        if self.algo1.is_flat() and self.algo_cw.is_flat():
            return scan_pair
        sym = self.algo1.state.open_symbol
        if not sym:
            sym = self.algo_cw.state.ce_symbol or self.algo_cw.state.pe_symbol
        # Prefer the live scan pair when it already contains the open leg
        # (e.g. PE 22600 @135 paired with CE 22950 @133 — NOT same-strike CE @325)
        if scan_pair and sym:
            ce_s = AlgoCwDual._pair_leg(scan_pair, "CE").get("tradingsymbol")
            pe_s = AlgoCwDual._pair_leg(scan_pair, "PE").get("tradingsymbol")
            if sym in (ce_s, pe_s):
                return {**scan_pair, "locked_to_open": True}
        if self.algo_cw.state.ce_symbol and self.algo_cw.state.pe_symbol:
            ce_sym, pe_sym = self.algo_cw.state.ce_symbol, self.algo_cw.state.pe_symbol
        elif scan_pair and (
            AlgoCwDual._pair_leg(scan_pair, "CE") and AlgoCwDual._pair_leg(scan_pair, "PE")
        ):
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
                    from foxtrade.kite_cache import get_instruments

                    tok_map = {
                        i["tradingsymbol"]: int(i["instrument_token"])
                        for i in get_instruments("NFO")
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
        a2 = self.algo_cw.state
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
            self.release("algo_cw")
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

    def _maybe_scan_next_week(self) -> None:
        """Refresh next-week CE/PE merge band for algo_nw (throttled, non-blocking)."""
        if not self.config.get("algo_nw_enabled", True):
            return
        now = time.time()
        # Keep scanning while flat; when in a trade, refresh slowly so LTP stays fresh
        interval = 45.0 if self.algo_nw.is_flat() else 120.0
        if now - float(getattr(self, "_last_nw_scan_at", 0) or 0) < interval:
            return
        if getattr(self, "_nw_scan_inflight", False):
            return
        self._nw_scan_inflight = True
        self._last_nw_scan_at = now
        cfg = dict(self.config)

        def _bg_nw_scan() -> None:
            try:
                from foxtrade.scanner import scan_next_week_premium_band

                low = float(cfg.get("algo_nw_premium_low", 46) or 46)
                high = float(cfg.get("algo_nw_premium_high", 54) or 54)
                result = scan_next_week_premium_band(
                    premium_low=low,
                    premium_high=high,
                    strike_window=float(cfg.get("strike_window", 800) or 800),
                )
                self.scan_nw = result
                watch: list[dict[str, Any]] = []
                for c in result.get("candidates") or []:
                    watch.append({**c, "exchange": c.get("exchange") or "NFO"})
                pair = result.get("pair") or {}
                for leg in (pair.get("ce"), pair.get("pe")):
                    if isinstance(leg, dict) and leg.get("tradingsymbol"):
                        watch.append({**leg, "exchange": leg.get("exchange") or "NFO"})
                for sym in (
                    self.algo_nw.state.ce_symbol,
                    self.algo_nw.state.pe_symbol,
                ):
                    if sym:
                        watch.append(
                            {"tradingsymbol": sym, "exchange": "NFO"}
                        )
                if watch:
                    self.market.set_watch(watch, replace=False)
            except Exception as exc:  # noqa: BLE001
                self.scan_nw = {**(self.scan_nw or {}), "error": str(exc)}
            finally:
                self._nw_scan_inflight = False

        threading.Thread(
            target=_bg_nw_scan, name="nw-scan", daemon=True
        ).start()

    def _loop(self) -> None:
        # Capture stop Event so soft-restart can orphan this thread safely
        stop = self._stop
        candle_refresh = 0.0
        macro_refresh = 0.0
        fii_refresh = 0.0
        loop_i = 0
        while not stop.is_set():
            t0 = time.time()
            loop_i += 1
            try:
                self.config = load_config()
                open_n = len(trade_store.open_trades())
                fii = self.fii_oi or {}
                acts = fii.get("actions") or {}

                # After 15:40 IST (or weekend): no Zerodha — reports-only.
                # Stay live past 15:40 only while OPEN positions still need management.
                want_idle = kite_idle_now(self.config) and open_n == 0
                if want_idle:
                    self._enter_kite_idle()
                    hb.beat(
                        loop=loop_i,
                        phase="kite_idle",
                        spot=self.market.last_spot,
                        auth_ok=True,
                        open_count=0,
                        kite_idle=True,
                    )
                    hb.set_meta(phase="kite_idle")
                    time.sleep(5.0)
                    continue
                self._exit_kite_idle()

                # Heartbeat first — supervisor restarts us if this goes stale
                hb.beat(
                    loop=loop_i,
                    phase="tick",
                    spot=self.market.last_spot,
                    auth_ok=not bool(
                        (getattr(self, "_auth_status", None) or {}).get("needs_login")
                    ),
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
                            interval_sec=float(
                                self.config.get("link_alert_beep_seconds", 5)
                            ),
                        )
                except Exception:
                    pass

                if getattr(self, "_needs_bootstrap", False):
                    # NEVER block the tick on bootstrap (rescan/candles/macro can
                    # exceed the stall watchdog and cause restart death spirals).
                    self._needs_bootstrap = False
                    if not getattr(self, "_bootstrap_inflight", False):
                        self._bootstrap_inflight = True
                        hb.beat(phase="bootstrap_kick", loop=loop_i)

                        def _bg_boot() -> None:
                            try:
                                self._bootstrap()
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
                            finally:
                                self._bootstrap_inflight = False

                        threading.Thread(
                            target=_bg_boot, name="engine-bootstrap", daemon=True
                        ).start()
                    hb.set_meta(phase="running")

                # Re-bind OPEN positions every loop (trades.json truth — no memory loss)
                try:
                    self.algo1.ensure_bound_from_store(reason="engine loop")
                except Exception:
                    pass
                try:
                    if pos_integrity.open_for_algo("algo_cw") and self.algo_cw.is_flat():
                        self.algo_cw.restore_from_store(load_runtime())
                except Exception:
                    pass
                try:
                    if pos_integrity.open_for_algo("algo_nw") and self.algo_nw.is_flat():
                        self.algo_nw.restore_from_store(load_runtime())
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

                # Weekday morning hard token refresh (default 09:00 IST) — logout + TOTP login once/day
                now_ist = datetime.now(IST)
                mins = now_ist.hour * 60 + now_ist.minute
                if (
                    self.config.get("morning_token_refresh_enabled", True)
                    and now_ist.weekday() < 5
                    and not kite_idle_now(self.config)
                ):
                    try:
                        rh, rm = str(
                            self.config.get("morning_token_refresh_hm") or "09:00"
                        ).split(":")[:2]
                        refresh_m = int(rh) * 60 + int(rm)
                    except Exception:
                        refresh_m = 9 * 60
                    day_key = now_ist.date().isoformat()
                    if (
                        refresh_m <= mins <= refresh_m + 2
                        and getattr(self, "_morning_refresh_day", None) != day_key
                    ):
                        self._morning_refresh_day = day_key
                        try:
                            from foxtrade.auto_login import morning_logout_relogin

                            # Always clear logout + TOTP login at scheduled 09:00
                            try:
                                self.clear_manual_logout()
                            except Exception:
                                pass
                            res = morning_logout_relogin()
                            self._auth_status = res
                            with self.lock:
                                self.notifications.append(
                                    {
                                        "ts": datetime.now(IST).isoformat(),
                                        "message": (
                                            f"Morning token refresh ok={res.get('ok')} "
                                            f"new={res.get('new_saved_at') or res.get('saved_at')}"
                                        ),
                                    }
                                )
                                self.notifications = self.notifications[-50:]
                        except Exception as exc:  # noqa: BLE001
                            with self.lock:
                                self.notifications.append(
                                    {
                                        "ts": datetime.now(IST).isoformat(),
                                        "message": f"Morning token refresh failed: {exc}",
                                    }
                                )
                                self.notifications = self.notifications[-50:]

                # Soft pre-market auth check (08:30–09:14 IST weekdays)
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
                hb.beat(phase="tick", loop=loop_i, step="quotes")
                if not quotes and self.market.last_spot is None:
                    # Bootstrap if market thread has not filled yet
                    try:
                        self.market.poll_once()
                        quotes = dict(self.market.last_quotes)
                    except Exception:
                        pass

                # Rescan is heavy (LTP batches) — never block tick; run async + beat first
                if time.time() - self.last_scan_at > 90:
                    if not getattr(self, "_rescan_inflight", False):
                        self._rescan_inflight = True
                        hb.beat(phase="rescan_kick", loop=loop_i)

                        def _bg_rescan() -> None:
                            try:
                                self._rescan(dig_history=False)
                            except Exception as exc:  # noqa: BLE001
                                self.scan = {**self.scan, "error": f"rescan: {exc}"}
                                self.last_scan_at = time.time()
                            finally:
                                self._rescan_inflight = False

                        threading.Thread(
                            target=_bg_rescan, name="engine-rescan", daemon=True
                        ).start()
                if time.time() - candle_refresh > 30 and not getattr(
                    self, "_candles_inflight", False
                ):
                    self._candles_inflight = True
                    hb.beat(phase="candles_kick", loop=loop_i)
                    candle_refresh = time.time()

                    def _bg_candles() -> None:
                        try:
                            self.market.refresh_nifty_candles()
                            if self.config.get("dual_mode"):
                                self.market.refresh_sensex_candles()
                        except Exception as exc:  # noqa: BLE001
                            self._push_activity_log(
                                level="warn", message=f"candles: {exc}"
                            )
                        finally:
                            self._candles_inflight = False

                    threading.Thread(
                        target=_bg_candles, name="engine-candles", daemon=True
                    ).start()
                if time.time() - getattr(self, "_last_auth_check", 0) > 180:
                    if not getattr(self, "_auth_inflight", False):
                        self._auth_inflight = True
                        self._last_auth_check = time.time()
                        need = bool(
                            (getattr(self, "_auth_status", None) or {}).get("needs_login")
                        )
                        force_prof = need or (mins < 9 * 60 + 20)

                        def _bg_auth() -> None:
                            try:
                                self.refresh_auth(force_profile=force_prof)
                            except Exception:
                                pass
                            finally:
                                self._auth_inflight = False

                        threading.Thread(
                            target=_bg_auth, name="engine-auth", daemon=True
                        ).start()
                if time.time() - macro_refresh > 120 and not getattr(
                    self, "_macro_inflight", False
                ):
                    self._macro_inflight = True
                    macro_refresh = time.time()

                    def _bg_macro() -> None:
                        try:
                            self.macro = get_macro(force=True)
                            if isinstance(
                                (self.macro or {}).get("factors", {}).get("fii_oi"), dict
                            ):
                                self.fii_oi = {
                                    **(self.fii_oi or {}),
                                    **(self.macro["factors"]["fii_oi"]),
                                    "actions": (self.macro or {}).get("fii_oi_actions")
                                    or (
                                        self.macro["factors"]["fii_oi"].get("actions") or {}
                                    ),
                                    "regime": (self.macro or {}).get("fii_oi_regime")
                                    or self.macro["factors"]["fii_oi"].get("regime"),
                                    "smart_money": (self.macro or {}).get("smart_money"),
                                }
                        except Exception as exc:  # noqa: BLE001
                            self.macro = {**(self.macro or {}), "error": str(exc)}
                        finally:
                            self._macro_inflight = False

                    threading.Thread(
                        target=_bg_macro, name="engine-macro", daemon=True
                    ).start()
                # Dedicated FII/OI scan every 60s — background (never block tick)
                if (
                    time.time() - fii_refresh
                    > float(self.config.get("fii_oi_scan_interval_sec", 60))
                    and not getattr(self, "_fii_inflight", False)
                ):
                    self._fii_inflight = True
                    fii_refresh = time.time()
                    prev_reg = (self.fii_oi or {}).get("regime")

                    def _bg_fii() -> None:
                        try:
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
                        finally:
                            self._fii_inflight = False

                    threading.Thread(
                        target=_bg_fii, name="engine-fii", daemon=True
                    ).start()

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
                trade_flat = self.algo1.is_flat() and self.algo_cw.is_flat()

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
                    if self.algo_cw.state.ce_entry and self.algo_cw.state.pe_entry:
                        ce_ref = self.algo_cw.state.ce_entry
                        pe_ref = self.algo_cw.state.pe_entry
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

                hb.beat(phase="tick", loop=loop_i, step="pre_dual")
                # Convergence-only: skip FastTrack / dual books unless explicitly re-enabled
                if self.config.get("algo1_enabled", False) or self.config.get(
                    "algo1_convergence_dual", False
                ):
                    if self.config.get("algo1_convergence_dual", False):
                        # Algo1 dual convergence + day-type (Algo-CW untouched)
                        self.algo1_conv.on_tick(ctx)
                    elif self.config.get("dual_mode"):
                        from foxtrade.indicators import ema as _ema

                        spot_closes = {
                            "NIFTY": self.market.session_closes_live("NIFTY"),
                            "SENSEX": self.market.session_closes_live("SENSEX"),
                        }
                        ema_by_id = {
                            uid: {
                                "ema9": _ema(closes, 9) if len(closes) >= 9 else None,
                                "ema21": _ema(closes, 21) if len(closes) >= 21 else None,
                            }
                            for uid, closes in spot_closes.items()
                        }
                        index_meta = {
                            "NIFTY": {
                                **index,
                                **ema_by_id["NIFTY"],
                                "spot": self.market.last_spot_by_id.get("NIFTY")
                                or index.get("spot"),
                                "day_open": self.market.day_open_by_id.get("NIFTY")
                                or index.get("day_open"),
                                "spot_closes": spot_closes["NIFTY"],
                            },
                            "SENSEX": {
                                **ema_by_id["SENSEX"],
                                "spot": self.market.last_spot_by_id.get("SENSEX"),
                                "day_open": self.market.day_open_by_id.get("SENSEX"),
                                "spot_closes": spot_closes["SENSEX"],
                            },
                        }
                        # Prefer dual_scans from last rescan. NEVER scan_all on the tick
                        # thread — kite.ltp batches hang with no timeout → ENGINE DIED spam.
                        if not self.dual.last_scans and not getattr(
                            self, "_dual_scan_inflight", False
                        ):
                            self._dual_scan_inflight = True

                            def _bg_dual_scan() -> None:
                                try:
                                    self.dual.scan_all(self.config)
                                except Exception:
                                    pass
                                finally:
                                    self._dual_scan_inflight = False

                            threading.Thread(
                                target=_bg_dual_scan, name="dual-scan", daemon=True
                            ).start()
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
                hb.beat(phase="tick", loop=loop_i, step="post_dual")
                if self.config.get("algo_cw_enabled", True):
                    self.algo_cw.on_tick(ctx)

                # Next-week convergence (46–54 @ 1.70×, max 1 setup) — separate book
                if self.config.get("algo_nw_enabled", True):
                    try:
                        self._maybe_scan_next_week()
                        raw_scan = self.scan if isinstance(self.scan, dict) else {}
                        raw_nw = self.scan_nw if isinstance(self.scan_nw, dict) else {}
                        pair_nw = AlgoCwDual._normalize_pair(raw_nw.get("pair"))
                        ctx_nw = {
                            **ctx,
                            "pair": pair_nw,
                            "scan": {
                                **raw_scan,
                                **raw_nw,
                                "pair": pair_nw,
                                "week": "next",
                                "merge_gate": (
                                    raw_scan.get("merge_gate")
                                    if isinstance(raw_scan.get("merge_gate"), dict)
                                    else {}
                                ),
                            },
                        }
                        self.algo_nw.on_tick(ctx_nw)
                        # Keep NW symbols on the quote watchlist
                        watch_nw = []
                        for side in ("CE", "PE"):
                            try:
                                sym = AlgoCwDual._pair_leg(pair_nw, side).get("tradingsymbol")
                                if sym:
                                    watch_nw.append(sym)
                            except Exception:
                                pass
                        for sym in (
                            self.algo_nw.state.ce_symbol,
                            self.algo_nw.state.pe_symbol,
                        ):
                            if sym:
                                watch_nw.append(sym)
                        if watch_nw:
                            self.market.set_watch(watch_nw, replace=False)
                    except Exception as exc:  # noqa: BLE001
                        self._push_activity_log(
                            level="warn", message=f"algo_nw: {exc}"
                        )

                # Parallel ATM CE/PE channel scan (alongside dual/fast-open).
                # Must stay non-blocking — heavy Kite work is backgrounded inside.
                if channel_enabled(self.config):
                    try:
                        hb.beat(phase="atm_channel", loop=loop_i)
                        self.atm_channel.on_tick(
                            cfg=self.config,
                            quotes=quotes,
                            candidates=list(self.scan.get("candidates") or []),
                            spot=self.market.last_spot or self.scan.get("spot"),
                            option_series_fn=self.market.series_payload,
                            mode="live" if self.config.get("live_trading") else "paper",
                            notifications=ctx["notifications"],
                            pair=self.scan.get("pair")
                            if isinstance(self.scan.get("pair"), dict)
                            else None,
                            atm_hint=self.scan.get("atm"),
                        )
                        watch_atm = self.atm_channel.instruments_to_watch()
                        if watch_atm:
                            self.market.set_watch(watch_atm, replace=False)
                        hb.beat(phase="tick", loop=loop_i)
                    except Exception as exc:  # noqa: BLE001
                        self._push_activity_log(
                            level="warn", message=f"atm_channel: {exc}"
                        )
                        hb.beat(phase="tick", loop=loop_i, atm_err=str(exc)[:80])

                if pe_range_enabled(self.config):
                    try:
                        hb.beat(phase="pe_range", loop=loop_i)
                        self.pe_range.on_tick(
                            cfg=self.config,
                            quotes=quotes,
                            candidates=list(self.scan.get("candidates") or []),
                            spot=self.market.last_spot or self.scan.get("spot"),
                            option_series_fn=self.market.series_payload,
                            mode="live" if self.config.get("live_trading") else "paper",
                            notifications=ctx["notifications"],
                            pair=self.scan.get("pair")
                            if isinstance(self.scan.get("pair"), dict)
                            else None,
                            atm_hint=self.scan.get("atm"),
                        )
                        watch_pe = self.pe_range.instruments_to_watch()
                        if watch_pe:
                            self.market.set_watch(watch_pe, replace=False)
                        hb.beat(phase="tick", loop=loop_i)
                    except Exception as exc:  # noqa: BLE001
                        self._push_activity_log(
                            level="warn", message=f"pe_range: {exc}"
                        )
                        hb.beat(phase="tick", loop=loop_i, pe_range_err=str(exc)[:80])

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
                                algo=str(n.get("algo") or "") or None,
                            )
                # Heartbeat lines — CW + NW Convergence (+ pulse so UI never freezes)
                try:
                    a2 = self.algo_cw.state
                    line = f"Algo-CW {a2.phase} · {a2.last_message or ''}"
                    self._push_activity_log(
                        level="tick", message=line, pulse_sec=10.0, algo="algo_cw"
                    )
                except Exception:
                    pass
                try:
                    a2nw = self.algo_nw.state
                    line = f"Algo-NW {a2nw.phase} · {a2nw.last_message or ''}"
                    self._push_activity_log(
                        level="tick", message=line, pulse_sec=10.0, algo="algo_nw"
                    )
                except Exception:
                    pass
                # Matching-criteria status every ~3s (dashboard ticker next to Capital Used)
                try:
                    self._push_criteria_pulse()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                # Non-blocking — a hung sibling thread must not deadlock us on self.lock
                if self.lock.acquire(blocking=False):
                    try:
                        self.scan["error"] = str(exc)
                    finally:
                        self.lock.release()
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
        a2 = self.algo_cw.snapshot(quotes)
        a2nw = self.algo_nw.snapshot(quotes)
        screen_band = {
            "low": cfg["premium_low"],
            "high": cfg["premium_high"],
            "source": "screen",
        }
        nw_band = {
            "low": float(cfg.get("algo_nw_premium_low", 46) or 46),
            "high": float(cfg.get("algo_nw_premium_high", 54) or 54),
            "source": "next_week",
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

        # Zone for chart — LIVE series only. Never call Kite historical on the
        # Flask request thread (that wedged /api/state and looked like ENGINE DIED).
        from foxtrade.convergence_zone import detect_convergence_zone

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
                    zone = detect_convergence_zone(
                        ce_v,
                        pe_v,
                        gap_tol=float(cfg.get("convergence_zone_gap", 12.0)),
                        min_samples=int(cfg.get("convergence_zone_min_samples", 15)),
                    )
                except Exception:
                    zone = None
            self._zone_cache = {"id": pid, "ts": now_ts, "zone": zone}

        a1["screen_band"] = screen_band
        a2["screen_band"] = screen_band
        a2nw["screen_band"] = nw_band
        a1["convergence_band"] = zone if zone else screen_band
        a2["convergence_band"] = zone if zone else screen_band
        a2nw["convergence_band"] = a2nw.get("convergence_zone") or nw_band
        # Prefer live_series from open NW book for its chart
        if not a2nw.get("live_series") and self.algo_nw.state.live_series:
            a2nw["live_series"] = self.algo_nw.state.live_series

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
        activity = self._activity_snapshot(a1, a2, opens, cfg, a2nw=a2nw)

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
            "atm_channel": {
                "enabled": channel_enabled(cfg),
                "phase": getattr(getattr(self, "atm_channel", None), "state", None)
                and self.atm_channel.state.phase,
                "message": (
                    self.atm_channel.status_line()
                    if getattr(self, "atm_channel", None)
                    else ""
                ),
                "legs": (
                    getattr(self.atm_channel.state, "legs", {})
                    if getattr(self, "atm_channel", None)
                    else {}
                ),
                "signals": (
                    getattr(self.atm_channel.state, "last_signals", {})
                    if getattr(self, "atm_channel", None)
                    else {}
                ),
                "open_trade_id": (
                    self.atm_channel.state.open_trade_id
                    if getattr(self, "atm_channel", None)
                    else None
                ),
            },
            "pe_range": {
                "enabled": pe_range_enabled(cfg),
                "phase": getattr(getattr(self, "pe_range", None), "state", None)
                and self.pe_range.state.phase,
                "message": (
                    self.pe_range.status_line()
                    if getattr(self, "pe_range", None)
                    else ""
                ),
                "leg": (
                    getattr(self.pe_range.state, "leg", {})
                    if getattr(self, "pe_range", None)
                    else {}
                ),
                "signal": (
                    getattr(self.pe_range.state, "last_signal", {})
                    if getattr(self, "pe_range", None)
                    else {}
                ),
                "open_trade_id": (
                    self.pe_range.state.open_trade_id
                    if getattr(self, "pe_range", None)
                    else None
                ),
            },
            "activity": activity,
            "activity_log": list(getattr(self, "_activity_log", [])[-80:]),
            "activity_log_cw": [
                r
                for r in getattr(self, "_activity_log", [])
                if (r.get("algo") or "algo_cw") != "algo_nw"
            ][-80:],
            "activity_log_nw": [
                r
                for r in getattr(self, "_activity_log", [])
                if r.get("algo") == "algo_nw"
            ][-80:],
            "engine_running": True,
            "scan": {
                "current_expiry": self.scan.get("current_expiry"),
                "next_expiry": self.scan.get("next_expiry"),
                "expiry_day": self.scan.get("expiry_day"),
                "spot": self.scan.get("spot"),
                "atm": self.scan.get("atm"),
                "band": self.scan.get("band"),
                "pair": display_pair or pair,
                "scan_pair": pair,
                "candidates": self.scan.get("candidates") or [],
                "pairs": self.scan.get("pairs") or [],
                "tracked_legs": self.scan.get("tracked_legs"),
                "tracked_pairs": self.scan.get("tracked_pairs"),
                "switch_reason": self.scan.get("switch_reason"),
                "active_id": self.scan.get("active_id"),
                "locked_merge": self.scan.get("locked_merge"),
                "merge_gate": self.scan.get("merge_gate") or {},
                "converged_once_ids": self.scan.get("converged_once_ids") or [],
                "decayed_ids": self.scan.get("decayed_ids") or [],
                "late_start": self.scan.get("late_start"),
                "late_report": self.scan.get("late_report"),
                "history_seeded_legs": self.scan.get("history_seeded_legs"),
            },
            "algo1": a1,
            "algo_cw": a2,
            "algo_nw": a2nw,
            # legacy aliases for older dashboard tabs
            "algo2": a2,
            "algo2nw": a2nw,
            "algo_names": {"cw": "Algo-CW", "nw": "Algo-NW"},
            "build_id": BUILD_ID,
            "scan_nw": {
                "current_expiry": (self.scan_nw or {}).get("current_expiry"),
                "next_expiry": (self.scan_nw or {}).get("next_expiry"),
                "expiry": (self.scan_nw or {}).get("expiry"),
                "week": "next",
                "spot": (self.scan_nw or {}).get("spot"),
                "atm": (self.scan_nw or {}).get("atm"),
                "band": (self.scan_nw or {}).get("band") or nw_band,
                "pair": (self.scan_nw or {}).get("pair"),
                "candidates": (self.scan_nw or {}).get("candidates") or [],
            },
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
                "tick_age": hb.tick_age_seconds(),
                "alive": hb.is_alive(float(cfg.get("heartbeat_max_age_sec", 180))),
                **(hb.read_heartbeat() or {}),
            },
            "unattended": {
                "enabled": bool(cfg.get("unattended_mode", True)),
                "auto_login": bool(cfg.get("unattended_auto_login", True)),
            },
            "kite_idle": {
                "active": bool(getattr(self, "_kite_idle", False)) or kite_idle_now(cfg),
                "after": str(cfg.get("kite_idle_after") or "15:40"),
                "reports_only": True,
                "message": (
                    "AFTER MARKET · engine idle · Reports OK"
                    if (getattr(self, "_kite_idle", False) or kite_idle_now(cfg))
                    else "engine active"
                ),
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
            "option_chain_db": self._option_chain_db_snapshot(),
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
        self,
        *,
        level: str,
        message: str,
        pulse_sec: float = 0.0,
        algo: str | None = None,
    ) -> None:
        """Append a UI log line. Safe to call while self.lock is held (no re-lock)."""
        msg = (message or "").strip()
        if not msg:
            return
        now = time.time()
        algo_key = str(algo or "")
        dedupe_key = f"{algo_key}|{level}|{msg}"
        # Deduplicate identical tick/criteria lines, but pulse every N sec so UI proves life
        if level in ("tick", "criteria") and dedupe_key == getattr(
            self, "_last_activity_dedupe", ""
        ):
            last_pulse = float(getattr(self, "_last_activity_pulse", 0) or 0)
            if pulse_sec <= 0 or now - last_pulse < pulse_sec:
                return
            if level == "tick":
                msg = f"{msg} · alive"
        if level in ("tick", "criteria"):
            self._last_activity_dedupe = dedupe_key
            self._last_activity_line = message.strip()
            self._last_activity_pulse = now
        row = {
            "ts": datetime.now(IST).isoformat(),
            "level": level,
            "message": msg[:280],
            "algo": algo_key or None,
        }
        log = getattr(self, "_activity_log", None)
        if log is None:
            self._activity_log = []
            log = self._activity_log
        log.append(row)
        if len(log) > 200:
            del log[:-200]

    def _build_criteria_line(self) -> str:
        """Human-readable matching-criteria status for the live ticker."""
        from foxtrade.late_start import minutes_since_open

        cfg = self.config or {}
        parts: list[str] = []
        mins = minutes_since_open()
        in_win = _in_session(cfg)
        parts.append(f"t+{mins}m")
        parts.append("WIN" if in_win else "OUT")

        running: list[str] = []
        if cfg.get("fast_open_mode"):
            running.append("fast_open")
        if str(cfg.get("entry_style", "orb")).lower() == "orb":
            if cfg.get("enable_orb_breakout", True):
                running.append("ORB↑")
            if cfg.get("enable_orb_breakdown", True):
                running.append("ORB↓")
        if cfg.get("enable_trend_follow", True):
            running.append("TREND")
        try:
            from foxtrade.micro_scalp import micro_enabled

            if micro_enabled(cfg):
                running.append("micro")
        except Exception:
            pass
        try:
            from foxtrade.daily_pa import daily_pa_enabled

            if daily_pa_enabled(cfg):
                running.append("daily_pa")
        except Exception:
            pass
        if cfg.get("post_decay_entry_enabled", True):
            running.append("post_decay")
        if cfg.get("dual_mode"):
            running.append("dual")
        if channel_enabled(cfg):
            running.append("ATM-CH")
        if pe_range_enabled(cfg):
            running.append("PE-RANGE")
        if running:
            parts.append("run[" + "+".join(running[:8]) + "]")

        # Channel status early so the 1-line banner always shows CE/PE pattern
        if channel_enabled(cfg) and getattr(self, "atm_channel", None):
            ch = self.atm_channel.status_line()
            if ch:
                parts.append(f"CH {ch}")
        if pe_range_enabled(cfg) and getattr(self, "pe_range", None):
            pr = self.pe_range.status_line()
            if pr:
                parts.append(f"PE-R {pr}")

        if self.decay.block_entries():
            parts.append(f"DECAY-BLOCK {self.decay.state.last_message or ''}".strip())
        elif self.decay.state.is_decay_day:
            parts.append(f"decay-day s={self.decay.state.score:.2f}")

        if cfg.get("dual_mode"):
            scores = getattr(self.dual, "last_scores", {}) or {}
            for uid in ("NIFTY", "SENSEX"):
                det = scores.get(uid) or {}
                allow = bool(det.get("allow"))
                side = det.get("side") or "-"
                conf = float(det.get("conf") or det.get("confidence") or det.get("score") or 0)
                event = str(det.get("event") or "").replace("_", " ")[:28]
                reason = str(det.get("reason") or "")[:40]
                tag = "MATCH" if allow else "WAIT"
                bit = f"{uid} {tag} {side} {conf:.0%}"
                if event:
                    bit += f" {event}"
                elif reason:
                    bit += f" · {reason}"
                parts.append(bit)
            dm = str(getattr(self.dual, "last_message", "") or "").strip()
            if dm:
                parts.append(dm[:70])
        else:
            a1 = self.algo1.state
            parts.append(
                f"A1 {a1.phase} {a1.predicted_side or '-'} "
                f"{float(a1.confidence or 0):.0%} · {(a1.last_message or a1.reason or '')[:70]}"
            )

        sx = None
        try:
            sx = self.dual.books.get("SENSEX")
        except Exception:
            sx = None
        if sx is not None and cfg.get("dual_mode"):
            st = sx.state
            if st.phase and st.phase not in ("SCAN",):
                parts.append(
                    f"SX {st.phase} {st.predicted_side or '-'} "
                    f"{float(st.confidence or 0):.0%} · {(st.last_message or '')[:50]}"
                )

        idx = {}
        try:
            idx = self.market.index_context() or {}
        except Exception:
            idx = {}
        orb = (idx.get("opening_range") or {}) if isinstance(idx, dict) else {}
        if orb.get("bias"):
            parts.append(
                f"OR {orb.get('bias')}"
                f"{('→' + str(orb.get('prefer_side'))) if orb.get('prefer_side') else ''}"
            )

        return " · ".join(p for p in parts if p)[:360]

    def _push_criteria_pulse(self) -> None:
        """Emit criteria status ~every 3 seconds for the Capital Used ticker."""
        now = time.time()
        last = float(getattr(self, "_last_criteria_pulse", 0) or 0)
        if now - last < 3.0:
            return
        line = self._build_criteria_line()
        if not line:
            return
        self._last_criteria_pulse = now
        row = {
            "ts": datetime.now(IST).isoformat(),
            "level": "criteria",
            "message": line,
        }
        log = getattr(self, "_activity_log", None)
        if log is None:
            self._activity_log = []
            log = self._activity_log
        last_crit = getattr(self, "_last_criteria_msg", "")
        # Same text → refresh stamp (UI stays alive); new text → append (scroll up)
        if line == last_crit and log and log[-1].get("level") == "criteria":
            log[-1] = row
        else:
            log.append(row)
            if len(log) > 120:
                del log[:-120]
        self._last_criteria_msg = line

    def _activity_snapshot(
        self,
        a1: dict[str, Any],
        a2: dict[str, Any],
        opens: list[dict[str, Any]],
        cfg: dict[str, Any],
        *,
        a2nw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compact live status for dashboard: scanning / waiting / buying / selling / holding."""
        in_win = _in_session(cfg)
        live = bool(cfg.get("live_trading"))
        mode_lbl = "LIVE" if live else "PAPER"
        a2nw = a2nw or {}

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
        leg_cw = _leg("algo_cw", a2)
        leg_nw = _leg("algo_nw", a2nw)
        headline_parts = []
        if cfg.get("algo_cw_enabled", True):
            headline_parts.append(f"Algo-CW {leg_cw['state']}")
        if cfg.get("algo_nw_enabled", True):
            headline_parts.append(f"Algo-NW {leg_nw['state']}")
        return {
            "mode": mode_lbl,
            "in_window": in_win,
            "headline": " · ".join(headline_parts) or "IDLE",
            "open_count": len(opens),
            "open_mtm": open_mtm,
            "open_invested": open_invested,
            "algo1": _leg("algo1", a1),
            "algo_cw": leg_cw,
            "algo_nw": leg_nw,
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
        if self.algo_cw.state.ce_entry and self.algo_cw.state.pe_entry:
            ce_ref, pe_ref = self.algo_cw.state.ce_entry, self.algo_cw.state.pe_entry
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

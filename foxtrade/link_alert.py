"""
Zerodha live-link alert: 1 beep / 5s while disconnected AND open positions exist.

Stops automatically when connection restores.
Manual mute via mute() — cleared automatically on restore (next outage can alert again).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # noqa: BLE001
    IST = timezone(timedelta(hours=5, minutes=30))

_lock = threading.Lock()
_state: dict[str, Any] = {
    "alerting": False,
    "muted": False,
    "muted_at": None,
    "link_ok": True,
    "reason": "init",
    "last_check_ts": 0.0,
    "last_beep_ts": 0.0,
    "beep_count": 0,
    "spot": None,
    "open_count": 0,
}


def mute() -> dict[str, Any]:
    """User stopped the alert manually."""
    with _lock:
        _state["muted"] = True
        _state["muted_at"] = datetime.now(IST).isoformat()
        return snapshot()


def unmute() -> dict[str, Any]:
    with _lock:
        _state["muted"] = False
        _state["muted_at"] = None
        return snapshot()


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _beep_once() -> None:
    try:
        import winsound

        winsound.Beep(980, 220)  # ~220ms tone
    except Exception:
        try:
            # Fallback console bell
            print("\a", end="", flush=True)
        except Exception:
            pass


def _probe_live() -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "reason": "unknown", "spot": None}
    try:
        from kite_client import get_authenticated_kite, session_stale_by_date

        if session_stale_by_date():
            return {"ok": False, "reason": "session_stale_need_login", "spot": None}
        kite = get_authenticated_kite()
        q = kite.ltp(["NSE:NIFTY 50"])
        spot = (q.get("NSE:NIFTY 50") or {}).get("last_price")
        if spot is None:
            return {"ok": False, "reason": "nifty_ltp_missing", "spot": None}
        try:
            kite.orders()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"orders_unreachable:{exc}", "spot": float(spot)}
        return {"ok": True, "reason": "ok", "spot": float(spot)}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "token" in msg or "session" in msg or "login" in msg:
            return {"ok": False, "reason": f"auth:{exc}", "spot": None}
        return {"ok": False, "reason": f"link:{exc}", "spot": None}


def check_zerodha_live(*, force: bool = False, timeout_sec: float = 6.0) -> dict[str, Any]:
    """
    Probe live index LTP + orders API. Cached ~4s unless force.
    Hard timeout so a hung Kite call never freezes the engine.
    """
    now = time.time()
    with _lock:
        if not force and now - float(_state["last_check_ts"]) < 4.0:
            return {
                "ok": bool(_state["link_ok"]),
                "reason": _state["reason"],
                "spot": _state["spot"],
            }

    box: dict[str, Any] = {"done": False, "out": None}

    def _run() -> None:
        box["out"] = _probe_live()
        box["done"] = True

    th = threading.Thread(target=_run, name="kite-link-probe", daemon=True)
    th.start()
    th.join(timeout=max(1.0, float(timeout_sec)))
    if not box["done"]:
        out = {"ok": False, "reason": "probe_timeout", "spot": None}
    else:
        out = box["out"] or {"ok": False, "reason": "probe_empty", "spot": None}
    _store_check(out)
    return out


def _store_check(out: dict[str, Any]) -> None:
    with _lock:
        _state["last_check_ts"] = time.time()
        _state["link_ok"] = bool(out.get("ok"))
        _state["reason"] = str(out.get("reason") or "")
        _state["spot"] = out.get("spot")


def tick(
    *,
    open_count: int,
    interval_sec: float = 5.0,
    trading_day_only: bool = True,
) -> dict[str, Any]:
    """
    Call from engine loop. Beeps at most once per interval_sec while:
      - open_count > 0
      - weekday (if trading_day_only)
      - Zerodha live link unhealthy
      - not muted
    """
    now = datetime.now(IST)
    with _lock:
        _state["open_count"] = int(open_count)

    if trading_day_only and now.weekday() >= 5:
        with _lock:
            _state["alerting"] = False
        return snapshot()

    # Probe whenever we have open risk OR live trading (catch WiFi cut before entry)
    live = False
    try:
        from foxtrade.config_store import load_config

        live = bool(load_config().get("live_trading"))
    except Exception:
        live = False

    if open_count <= 0 and not live:
        # Flat paper — stall_alert still watches; skip engine-loop probe spam
        with _lock:
            _state["alerting"] = False
        return snapshot()

    link = check_zerodha_live()
    link_ok = bool(link.get("ok"))

    with _lock:
        if link_ok:
            was = _state["alerting"]
            _state["alerting"] = False
            _state["link_ok"] = True
            _state["reason"] = "ok"
            if was or _state["muted"]:
                _state["muted"] = False
                _state["muted_at"] = None
            return dict(_state)

        _state["alerting"] = True
        _state["link_ok"] = False
        _state["reason"] = str(link.get("reason") or "link_down")
        if _state["muted"]:
            return dict(_state)

        if time.time() - float(_state["last_beep_ts"]) >= float(interval_sec):
            _state["last_beep_ts"] = time.time()
            _state["beep_count"] = int(_state["beep_count"]) + 1
            do_beep = True
        else:
            do_beep = False

    if do_beep:
        threading.Thread(target=_beep_once, name="link-beep", daemon=True).start()
        # One-shot voice kick (stall_alert owns the full 60s loop)
        try:
            from foxtrade import voice_alert

            if not voice_alert.is_speaking("zerodha_data"):
                from foxtrade.config_store import load_config as _lc

                secs = float((_lc() or {}).get("voice_alert_seconds", 30))
                voice_alert.speak_for(
                    "zerodha_data",
                    "Zerodha disconnected. No network.",
                    duration_sec=secs,
                )
        except Exception:
            pass
    return snapshot()

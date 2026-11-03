"""
Watchdog independent of the trading loop:

1. Engine heartbeat stale → voice "Engine died" for 60s (once per incident)
   then soft-restart engine; if still dead → full app relaunch.
2. No fresh Zerodha LTP → voice for 60s; repeat until data restores.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade import voice_alert

ROOT = Path(__file__).resolve().parents[1]
HB_PATH = ROOT / "data" / "heartbeat.json"
LOG_PATH = ROOT / "data" / "stall_alert.log"
IST = ZoneInfo("Asia/Kolkata")

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "alerting": False,
    "data_alerting": False,
    "muted": False,
    "last_beep_ts": 0.0,
    "beep_count": 0,
    "hb_age": None,
    "data_age": None,
    "link_down": False,
    "link_reason": "",
    "reason": "",
    "data_reason": "",
    "started_at": None,
    "last_engine_voice_ts": 0.0,
    "last_data_voice_ts": 0.0,
    "last_restart_ts": 0.0,
    "restart_count": 0,
    "relaunch_count": 0,
}
_thread: threading.Thread | None = None
_stop = threading.Event()


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def mute() -> dict[str, Any]:
    with _lock:
        _state["muted"] = True
        _state["alerting"] = False
        _state["data_alerting"] = False
    voice_alert.stop()
    return snapshot()


def unmute() -> dict[str, Any]:
    with _lock:
        _state["muted"] = False
    return snapshot()


def _log(msg: str) -> None:
    line = f"{datetime.now(IST).isoformat()}  {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(f"[stall_alert] {msg}", flush=True)


def _hb_age() -> float | None:
    try:
        data = json.loads(HB_PATH.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(data["ts"]))
    except Exception:
        return None


def _data_age() -> float | None:
    """Seconds since last successful Zerodha LTP (spot) update."""
    try:
        from foxtrade.engine import ENGINE

        ts = getattr(ENGINE.market, "last_data_ts", None)
        if ts is None:
            return None
        return max(0.0, time.time() - float(ts))
    except Exception:
        return None


def _probe_link_down() -> tuple[bool, str]:
    """True when Zerodha/network unreachable (WiFi cut, API dead)."""
    try:
        from foxtrade import link_alert

        link = link_alert.check_zerodha_live(force=True)
        ok = bool(link.get("ok"))
        return (not ok), str(link.get("reason") or "link_down")
    except Exception as exc:  # noqa: BLE001
        return True, f"probe_err:{exc}"


def _soft_restart_engine() -> dict[str, Any]:
    try:
        from foxtrade.engine import ENGINE

        return dict(ENGINE.restart(reason="stall_watchdog") or {})
    except Exception as exc:  # noqa: BLE001
        _log(f"soft restart failed: {exc}")
        return {"ok": False, "need_relaunch": True, "detail": str(exc)}


def _full_relaunch(reason: str) -> None:
    """Spawn detached relauncher then exit this process so ports free cleanly."""
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    if not Path(py).exists():
        py = sys.executable
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    _log(f"full relaunch scheduled: {reason}")
    subprocess.Popen(
        [py, str(ROOT / "tools_relaunch_app.py"), reason],
        cwd=str(ROOT),
        env=env,
        creationflags=flags,
        close_fds=True,
    )
    # Give relauncher a head start, then die so port is free
    time.sleep(0.5)
    os._exit(0)


def _loop(
    max_hb_age: float,
    max_data_age: float,
    voice_sec: float,
    restart_cooldown: float,
) -> None:
    _log(
        f"watchdog on hb>{max_hb_age}s data>{max_data_age}s "
        f"voice={voice_sec}s restart_cd={restart_cooldown}s"
    )
    engine_incident = False
    data_incident = False
    last_link_probe = 0.0
    link_down = False
    link_reason = ""

    while not _stop.wait(5.0):
        # Always armed (including nights) — WiFi cut must still voice-alert
        age = _hb_age()
        dage = _data_age()
        engine_dead = age is None or age > max_hb_age
        warm = (time.time() - float(_state.get("last_restart_ts") or time.time())) < 45

        # Active link probe every 12s (catches WiFi off even if last LTP looks "recent")
        if time.time() - last_link_probe >= 12.0:
            last_link_probe = time.time()
            link_down, link_reason = _probe_link_down()

        data_stale = False
        if not warm:
            if link_down:
                data_stale = True
                if dage is None:
                    dage = 9999.0
            elif dage is None:
                data_stale = True
                dage = 9999.0
            elif dage > max_data_age:
                data_stale = True

        with _lock:
            muted = bool(_state["muted"])
            _state["hb_age"] = age
            _state["data_age"] = dage
            _state["link_down"] = link_down
            _state["link_reason"] = link_reason

        # ---- Engine recovery ----
        if not engine_dead:
            if engine_incident:
                _log(f"engine recovered hb_age={age:.0f}s")
                voice_alert.stop("engine_died")
                voice_alert.clear_cooldown("engine_died")
            engine_incident = False
            with _lock:
                _state["alerting"] = False
                if not data_stale:
                    _state["reason"] = "ok"
        else:
            with _lock:
                _state["alerting"] = True
                _state["reason"] = (
                    "no_heartbeat" if age is None else f"stale_{age:.0f}s"
                )
            if not muted and not engine_incident:
                engine_incident = True
                _log(f"ENGINE DIED — voice {voice_sec:.0f}s once + restart ({_state['reason']})")
                with _lock:
                    _state["last_engine_voice_ts"] = time.time()
                    _state["beep_count"] = int(_state["beep_count"]) + 1
                voice_alert.speak_for(
                    "engine_died",
                    "Engine died. Restarting.",
                    duration_sec=voice_sec,
                )
                res = _soft_restart_engine()
                with _lock:
                    _state["last_restart_ts"] = time.time()
                    _state["restart_count"] = int(_state["restart_count"]) + 1
                _log(f"soft restart result={res}")
                if res.get("need_relaunch"):
                    with _lock:
                        _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                    _full_relaunch(f"thread_hung:{res.get('detail')}")
                # If still dead shortly after soft restart → full relaunch (no more voice)
                time.sleep(min(20.0, max(8.0, restart_cooldown * 0.35)))
                age2 = _hb_age()
                if age2 is None or age2 > max_hb_age:
                    with _lock:
                        last_rl = float(_state["last_restart_ts"] or 0)
                        can_rl = time.time() - last_rl >= restart_cooldown
                    if can_rl:
                        with _lock:
                            _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                        _full_relaunch(f"engine_frozen_hb_{age2}")
            elif engine_incident:
                # Already voiced once this incident — keep trying silent relaunch if needed
                with _lock:
                    last_rl = float(_state["last_restart_ts"] or 0)
                    can_rl = time.time() - last_rl >= restart_cooldown
                if can_rl:
                    age2 = _hb_age()
                    if age2 is None or age2 > max_hb_age:
                        with _lock:
                            _state["last_restart_ts"] = time.time()
                            _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                        _full_relaunch(f"engine_still_down_{age2}")

        # ---- Zerodha data freshness (only when engine itself is alive) ----
        if engine_dead:
            continue

        if not data_stale:
            if data_incident:
                _log(f"Zerodha data restored age={dage}")
                voice_alert.stop("zerodha_data")
                voice_alert.clear_cooldown("zerodha_data")
            data_incident = False
            with _lock:
                _state["data_alerting"] = False
                _state["data_reason"] = "ok"
            continue

        reason = (
            f"link_down:{link_reason}"
            if link_down
            else (f"stale_{dage:.0f}s" if dage is not None else "no_data")
        )
        with _lock:
            _state["data_alerting"] = True
            _state["data_reason"] = reason

        if muted:
            continue

        # Voice once for ~30s this outage; silent until restore then re-arm
        if not data_incident and not muted:
            data_incident = True
            with _lock:
                _state["last_data_voice_ts"] = time.time()
            phrase = (
                "Zerodha disconnected. No network."
                if link_down
                else "Zerodha data not updating."
            )
            _log(f"Zerodha/link alert — voice {voice_sec:.0f}s once ({reason})")
            voice_alert.speak_for(
                "zerodha_data",
                phrase,
                duration_sec=voice_sec,
            )
            try:
                from foxtrade.auto_login import ensure_kite_session

                ensure_kite_session(force_login=False)
            except Exception:
                pass


def start(
    max_age_sec: float = 90.0,
    beep_every_sec: float = 15.0,  # kept for API compat; voice uses duration
    *,
    max_data_age_sec: float = 45.0,
    voice_sec: float = 30.0,
    restart_cooldown_sec: float = 60.0,
) -> None:
    global _thread
    del beep_every_sec  # unused — voice replaces periodic beeps
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["started_at"] = datetime.now(IST).isoformat()
        _state["last_restart_ts"] = time.time()
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        args=(
            float(max_age_sec),
            float(max_data_age_sec),
            float(voice_sec),
            float(restart_cooldown_sec),
        ),
        name="stall-watchdog",
        daemon=True,
    )
    _thread.start()


def stop() -> None:
    _stop.set()
    voice_alert.stop()
    with _lock:
        _state["running"] = False

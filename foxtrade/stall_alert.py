"""
Watchdog independent of the trading loop:

1. Engine heartbeat stale → voice "Engine died" (throttled) then soft-restart.
2. No fresh Zerodha LTP → voice until data restores (skipped when logged out / kite idle).

Soft-restart MUST seed tick_ts (via ENGINE.restart → hb.seed_alive). Without that,
the watchdog sees the pre-hang age and death-spirals soft restarts forever.
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

# After soft-restart, ignore stale age until the new loop has had time to tick.
RESTART_GRACE_SEC = 90.0
# Need this many consecutive "dead" polls (~5s each) before acting.
DEAD_STRIKES_NEEDED = 3
# Cap soft-restarts per rolling window to stop death spirals even if seed fails.
MAX_RESTARTS_PER_WINDOW = 4
RESTART_WINDOW_SEC = 600.0

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "alerting": False,
    "data_alerting": False,
    "muted": False,
    "last_beep_ts": 0.0,
    "beep_count": 0,
    "hb_age": None,
    "pulse_age": None,
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
    "max_hb_age_sec": 180.0,
    "max_data_age_sec": 45.0,
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
    """Process pulse age (any write)."""
    try:
        data = json.loads(HB_PATH.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(data["ts"]))
    except Exception:
        return None


_last_good_tick: float | None = None


def _tick_age() -> float | None:
    """Engine-loop age — pulse must NOT mask a hung trading thread.

    A torn or missing heartbeat file must not look like death. Keep the last
    good tick time so one bad read cannot fire ENGINE DIED.
    """
    global _last_good_tick
    try:
        data = json.loads(HB_PATH.read_text(encoding="utf-8"))
        ts = data.get("tick_ts")
        if ts is None and not data.get("pulse"):
            ts = data.get("ts")
        if ts is not None:
            _last_good_tick = float(ts)
    except Exception:
        pass
    if _last_good_tick is None:
        return None
    return max(0.0, time.time() - _last_good_tick)


def _seed_watchdog_clock() -> None:
    """After soft-restart, treat the loop as freshly alive in-memory too."""
    global _last_good_tick
    _last_good_tick = time.time()


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


def _manual_logout() -> bool:
    try:
        from kite_client import is_manual_logout

        return bool(is_manual_logout())
    except Exception:
        return False


def _soft_restart_engine() -> dict[str, Any]:
    try:
        from foxtrade.engine import ENGINE

        res = dict(ENGINE.restart(reason="stall_watchdog") or {})
        _seed_watchdog_clock()
        return res
    except Exception as exc:  # noqa: BLE001
        _log(f"soft restart failed: {exc}")
        return {"ok": False, "need_relaunch": True, "detail": str(exc)}


def _live_or_open() -> tuple[bool, int]:
    """True if live trading ON or any OPEN trade — never hard-kill in that case."""
    try:
        from foxtrade.config_store import load_config
        from foxtrade import trade_store

        live = bool(load_config().get("live_trading"))
        n = len(trade_store.open_trades())
        return (live or n > 0), n
    except Exception:
        return False, 0


def _full_relaunch(reason: str) -> None:
    """Spawn detached relauncher then exit this process so ports free cleanly.

    HARD RULE: never process-kill during live trading or while OPEN positions exist.
    Soft-restart only — a mid-live os._exit orphans risk and loses the book.
    """
    protect, opens = _live_or_open()
    if protect:
        _log(
            f"RELAUNCH BLOCKED (live/open={opens}) reason={reason} — soft recover only"
        )
        try:
            res = _soft_restart_engine()
            _log(f"protected soft restart={res}")
        except Exception as exc:  # noqa: BLE001
            _log(f"protected soft restart failed: {exc}")
        with _lock:
            _state["last_restart_ts"] = time.time()
        return

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
        f"voice={voice_sec}s restart_cd={restart_cooldown}s "
        f"grace={RESTART_GRACE_SEC:.0f}s"
    )
    engine_incident = False
    data_incident = False
    last_link_probe = 0.0
    link_down = False
    link_reason = ""
    dead_strikes = 0
    boot_ts = time.time()
    restart_times: list[float] = []

    while not _stop.wait(5.0):
        # Always armed (including nights) — WiFi cut must still voice-alert
        pulse_age = _hb_age()
        age = _tick_age()  # hung tick while pulse alive = engine_dead
        dage = _data_age()
        logged_out = _manual_logout()

        with _lock:
            last_restart = float(_state.get("last_restart_ts") or 0)
        since_restart = time.time() - last_restart if last_restart else 1e9
        in_grace = since_restart < RESTART_GRACE_SEC

        # First minute / post-restart grace: missing tick is boot, not death.
        raw_dead = (age is not None and age > max_hb_age) or (
            age is None and (time.time() - boot_ts) >= 60 and not in_grace
        )
        if in_grace:
            raw_dead = False
        if time.time() - boot_ts < 60 and age is None:
            raw_dead = False

        if raw_dead:
            dead_strikes += 1
        else:
            dead_strikes = 0
        engine_dead = dead_strikes >= DEAD_STRIKES_NEEDED

        # After 15:40 / weekend: engine is reports-only — don't probe Zerodha or alert on stale LTP.
        kite_idle = False
        try:
            from foxtrade.engine import ENGINE, kite_idle_now

            kite_idle = bool(ENGINE.is_kite_idle()) or kite_idle_now(ENGINE.config)
        except Exception:
            kite_idle = False

        # Active link probe every 12s (catches WiFi off even if last LTP looks "recent")
        if logged_out or kite_idle:
            link_down, link_reason = False, ("manual_logout" if logged_out else "kite_idle")
        elif time.time() - last_link_probe >= 12.0:
            last_link_probe = time.time()
            link_down, link_reason = _probe_link_down()

        # Data freshness — skip while logged out / kite idle / post-restart warm-up
        data_stale = False
        warm = in_grace or since_restart < 45
        if logged_out or kite_idle or warm:
            data_stale = False
        else:
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
            _state["pulse_age"] = pulse_age
            _state["data_age"] = dage
            _state["link_down"] = link_down
            _state["link_reason"] = link_reason
            _state["max_hb_age_sec"] = max_hb_age
            _state["max_data_age_sec"] = max_data_age

        # ---- Engine recovery ----
        if not engine_dead:
            if engine_incident:
                _log(f"engine recovered hb_age={0 if age is None else age:.0f}s")
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
            # Mute silences voice only. A hung tick must still soft-restart,
            # or the dashboard stays on ENGINE DIED until someone reloads.
            if not engine_incident:
                engine_incident = True
                with _lock:
                    last_voice = float(_state.get("last_engine_voice_ts") or 0)
                    restarts = int(_state.get("restart_count") or 0)
                voice_gap = time.time() - last_voice
                should_voice = (not muted) and voice_gap >= 600.0

                # Prune restart window and enforce budget
                now = time.time()
                restart_times[:] = [t for t in restart_times if now - t < RESTART_WINDOW_SEC]
                budget_ok = len(restart_times) < MAX_RESTARTS_PER_WINDOW

                if not budget_ok:
                    _log(
                        f"ENGINE STALE — restart budget exhausted "
                        f"({len(restart_times)}/{MAX_RESTARTS_PER_WINDOW} in "
                        f"{RESTART_WINDOW_SEC:.0f}s) age={age} — cooling down"
                    )
                    with _lock:
                        _state["reason"] = "restart_budget_cooldown"
                    # Reset incident after cooldown so we can try again later
                    if restart_times and (now - restart_times[0]) >= RESTART_WINDOW_SEC:
                        engine_incident = False
                        dead_strikes = 0
                    continue

                if should_voice:
                    _log(
                        f"ENGINE DIED — voice {voice_sec:.0f}s + soft restart "
                        f"({_state['reason']}) restarts={restarts}"
                    )
                    with _lock:
                        _state["last_engine_voice_ts"] = time.time()
                        _state["beep_count"] = int(_state["beep_count"]) + 1
                    voice_alert.speak_for(
                        "engine_died",
                        "Engine died. Restarting.",
                        duration_sec=min(15.0, float(voice_sec)),
                    )
                else:
                    _log(
                        f"ENGINE STALE — silent soft restart ({_state['reason']}) "
                        f"muted={muted} restarts={restarts}"
                    )
                res = _soft_restart_engine()
                restart_times.append(time.time())
                with _lock:
                    _state["last_restart_ts"] = time.time()
                    _state["restart_count"] = int(_state["restart_count"]) + 1
                dead_strikes = 0
                _log(f"soft restart result={res}")
                # Soft restart always orphans+respawns now. Only process-relaunch
                # if soft path itself failed AND we are NOT live/OPEN.
                if res.get("need_relaunch"):
                    with _lock:
                        _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                    _full_relaunch(f"thread_hung:{res.get('detail')}")
                else:
                    # Wait through grace — do NOT re-check pre-hang tick_age
                    time.sleep(min(25.0, max(12.0, restart_cooldown * 0.4)))
                    age2 = _tick_age()
                    # Only retry if age grew past max AND we are past seed (age2 should be small)
                    if age2 is not None and age2 > max_hb_age:
                        _log(f"still stale after soft (tick_age={age2}) — soft retry")
                        res2 = _soft_restart_engine()
                        restart_times.append(time.time())
                        with _lock:
                            _state["last_restart_ts"] = time.time()
                            _state["restart_count"] = int(_state["restart_count"]) + 1
                        dead_strikes = 0
                        _log(f"soft retry result={res2}")
                        if res2.get("need_relaunch"):
                            with _lock:
                                _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                            _full_relaunch(f"engine_frozen_hb_{age2}")
                    else:
                        _log(
                            f"post-restart tick ok (age={age2}) — watching"
                        )
            elif engine_incident:
                # Already voiced — keep soft-recovering on cooldown; respect budget
                with _lock:
                    last_rl = float(_state["last_restart_ts"] or 0)
                    can_rl = time.time() - last_rl >= max(restart_cooldown, RESTART_GRACE_SEC)
                now = time.time()
                restart_times[:] = [t for t in restart_times if now - t < RESTART_WINDOW_SEC]
                if can_rl and len(restart_times) < MAX_RESTARTS_PER_WINDOW:
                    age2 = _tick_age()
                    if age2 is None or age2 > max_hb_age:
                        with _lock:
                            _state["last_restart_ts"] = time.time()
                            _state["restart_count"] = int(_state["restart_count"]) + 1
                        res2 = _soft_restart_engine()
                        restart_times.append(time.time())
                        dead_strikes = 0
                        _log(f"silent soft recover tick_age={age2} result={res2}")
                        if res2.get("need_relaunch"):
                            with _lock:
                                _state["relaunch_count"] = int(_state["relaunch_count"]) + 1
                            _full_relaunch(f"engine_still_down_{age2}")
                elif can_rl and len(restart_times) >= MAX_RESTARTS_PER_WINDOW:
                    _log("silent recover skipped — restart budget exhausted")
                    # Clear incident so we re-evaluate after window
                    if restart_times and (now - min(restart_times)) >= RESTART_WINDOW_SEC:
                        engine_incident = False
                        dead_strikes = 0
                        restart_times.clear()

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

        if muted or logged_out:
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
    max_age_sec: float = 180.0,
    beep_every_sec: float = 15.0,  # kept for API compat; voice uses duration
    *,
    max_data_age_sec: float = 45.0,
    voice_sec: float = 30.0,
    restart_cooldown_sec: float = 90.0,
) -> None:
    global _thread
    del beep_every_sec  # unused — voice replaces periodic beeps
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["started_at"] = datetime.now(IST).isoformat()
        _state["last_restart_ts"] = time.time()
        _state["max_hb_age_sec"] = float(max_age_sec)
        _state["max_data_age_sec"] = float(max_data_age_sec)
    _stop.clear()
    _seed_watchdog_clock()
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

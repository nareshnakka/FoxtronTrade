"""Process heartbeat so an outer supervisor can detect freezes and restart."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "heartbeat.json"
_lock = threading.Lock()
_meta: dict[str, Any] = {}
_pulse_stop = threading.Event()
_pulse_thread: threading.Thread | None = None
_last_tick_ts: float = 0.0


def beat(**extra: Any) -> None:
    """Write heartbeat — call from engine loop every tick (cheap)."""
    global _last_tick_ts
    with _lock:
        now = time.time()
        phase = str(extra.get("phase") or _meta.get("phase") or "")
        force_tick = bool(extra.pop("_force_tick", False))
        # Only real loop progress resets tick_ts.
        # Do NOT count soft-restart / kick phases — that caused false "recovered"
        # then ENGINE DIED voice every ~60s while the new thread was still hung.
        is_tick = force_tick or phase in (
            "tick",
            "atm_channel",
            "slow_tick",
            "error",
            "kite_idle",
        ) or (
            extra.get("loop") is not None and phase not in (
                "restarted",
                "pulse",
                "pulse_start",
                "bootstrap_kick",
                "bootstrap_start",
                "rescan_kick",
                "candles_kick",
            )
        )
        if is_tick:
            _last_tick_ts = now
            _meta["tick_ts"] = now
            _meta["tick_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        payload = {
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "tick_ts": _meta.get("tick_ts") or _last_tick_ts or None,
            "tick_iso": _meta.get("tick_iso"),
            **_meta,
            **extra,
        }
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace — a torn write was parsed as "no heartbeat"
            # and the watchdog announced ENGINE DIED on a live loop.
            tmp = PATH.with_suffix(".json.tmp")
            raw = json.dumps(payload)
            tmp.write_text(raw, encoding="utf-8")
            os.replace(tmp, PATH)
        except Exception:
            pass


def seed_alive(*, reason: str = "seed") -> None:
    """
    Mark the trading loop alive RIGHT NOW.

    Soft-restart MUST call this — otherwise tick_ts stays at the pre-hang
    timestamp and the stall watchdog immediately restarts again (death spiral).
    """
    beat(phase="tick", loop=0, step="seed", reason=str(reason)[:80], _force_tick=True)


def set_meta(**kwargs: Any) -> None:
    with _lock:
        _meta.update(kwargs)


def read_heartbeat() -> dict[str, Any]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def age_seconds() -> float | None:
    """Age of last write (pulse OR tick) — process liveness."""
    hb = read_heartbeat()
    ts = hb.get("ts")
    if ts is None:
        return None
    try:
        return max(0.0, time.time() - float(ts))
    except Exception:
        return None


def tick_age_seconds() -> float | None:
    """Age of last real engine tick — ignores pulse so hung loops are visible."""
    hb = read_heartbeat()
    ts = hb.get("tick_ts")
    if ts is None:
        # Fall back to ts only when phase looks like a real tick
        if hb.get("pulse"):
            return None
        ts = hb.get("ts")
    if ts is None:
        return None
    try:
        return max(0.0, time.time() - float(ts))
    except Exception:
        return None


def is_alive(max_age: float = 90.0) -> bool:
    age = age_seconds()
    return age is not None and age <= max_age


def start_pulse(interval_sec: float = 5.0) -> None:
    """
    Independent pulse so a slow Kite call cannot look like a dead *process*.
    Does NOT reset tick_ts — stall watchdog still sees a hung engine loop.
    """
    global _pulse_thread
    if _pulse_thread and _pulse_thread.is_alive():
        return
    _pulse_stop.clear()

    def _run() -> None:
        while not _pulse_stop.wait(max(3.0, float(interval_sec))):
            extra: dict[str, Any] = {"phase": "pulse", "pulse": True}
            try:
                from foxtrade import trade_store
                from foxtrade.config_store import load_config

                opens = trade_store.open_trades()
                cfg = load_config()
                extra["open_count"] = len(opens)
                extra["live"] = bool(cfg.get("live_trading"))
            except Exception:
                pass
            with _lock:
                ta = None
                if _last_tick_ts:
                    ta = max(0.0, time.time() - _last_tick_ts)
                elif _meta.get("tick_ts"):
                    ta = max(0.0, time.time() - float(_meta["tick_ts"]))
            if ta is not None:
                extra["tick_age"] = round(ta, 1)
            beat(**extra)

    _pulse_thread = threading.Thread(target=_run, name="hb-pulse", daemon=True)
    _pulse_thread.start()
    beat(phase="pulse_start")


def stop_pulse() -> None:
    _pulse_stop.set()

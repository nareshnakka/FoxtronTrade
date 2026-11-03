"""Windows voice alerts — speak a phrase ONE time (no looping)."""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

_lock = threading.Lock()
_active: dict[str, Any] = {}  # key -> {"stop": Event, "thread": Thread}
_last_spoke: dict[str, float] = {}  # key -> ts (cooldown so no re-alert spam)


def is_speaking(key: str) -> bool:
    with _lock:
        meta = _active.get(key)
        return bool(meta and meta["thread"].is_alive())


def stop(key: str | None = None) -> None:
    with _lock:
        keys = list(_active.keys()) if key is None else [key]
        for k in keys:
            meta = _active.get(k)
            if meta:
                meta["stop"].set()


def _sapi_speak(text: str) -> None:
    safe = text.replace("'", "").replace('"', "")[:120]
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate=0; $s.Volume=100; "
        f"$s.Speak('{safe}')"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=25,
            capture_output=True,
            check=False,
        )
        return
    except Exception:
        pass
    try:
        import winsound

        winsound.Beep(1000, 500)
    except Exception:
        print(f"\a VOICE: {text}", flush=True)


def speak_for(
    key: str,
    phrase: str,
    *,
    duration_sec: float = 30.0,
    gap_sec: float = 0.4,
) -> bool:
    """
    Speak `phrase` exactly once. `duration_sec` is a cooldown so the same
    key will not speak again until that many seconds pass (and incident resets).
    Returns False if already speaking or still in cooldown.
    """
    del gap_sec  # unused — no loop
    now = time.time()
    with _lock:
        meta = _active.get(key)
        if meta and meta["thread"].is_alive():
            return False
        last = float(_last_spoke.get(key) or 0)
        if now - last < float(duration_sec):
            return False
        stop_ev = threading.Event()

        def _run() -> None:
            if not stop_ev.is_set():
                _sapi_speak(phrase)
            with _lock:
                cur = _active.get(key)
                if cur and cur["stop"] is stop_ev:
                    _active.pop(key, None)

        th = threading.Thread(target=_run, name=f"voice-{key}", daemon=True)
        _active[key] = {"stop": stop_ev, "thread": th, "phrase": phrase}
        _last_spoke[key] = now
        th.start()
    return True


def clear_cooldown(key: str | None = None) -> None:
    """Allow the same alert key to speak again (e.g. after link restores)."""
    with _lock:
        if key is None:
            _last_spoke.clear()
        else:
            _last_spoke.pop(key, None)

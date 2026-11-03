"""Process heartbeat so an outer supervisor can detect freezes and restart."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "heartbeat.json"
_lock = threading.Lock()
_meta: dict[str, Any] = {}


def beat(**extra: Any) -> None:
    """Write heartbeat — call from engine loop every tick (cheap)."""
    with _lock:
        payload = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            **_meta,
            **extra,
        }
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass


def set_meta(**kwargs: Any) -> None:
    with _lock:
        _meta.update(kwargs)


def read_heartbeat() -> dict[str, Any]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def age_seconds() -> float | None:
    hb = read_heartbeat()
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

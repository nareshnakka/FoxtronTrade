"""Persist algo runtime so open positions survive process/code restarts."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PATH = DATA / "runtime_state.json"
_lock = threading.Lock()


def load_runtime() -> dict[str, Any]:
    with _lock:
        if not PATH.exists():
            return {}
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_runtime(payload: dict[str, Any]) -> None:
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

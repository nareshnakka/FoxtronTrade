"""
Shared Kite instruments cache.

kite.instruments(NFO/BFO) is multi‑MB and routinely takes 10–40s. Calling it on
the engine tick freezes the process (GIL / broker I/O) even with an HB pulse —
exits stall, miss fills, or soft-restart mid-book. Trading paths MUST use
non-blocking reads: return cache immediately and refresh in the background.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_cache: dict[str, list[dict[str, Any]]] = {}
_ts: dict[str, float] = {}
_inflight: dict[str, bool] = {}

# 30 min is plenty — option contract list only changes overnight / new listings
DEFAULT_TTL = 30 * 60


def _fetch_sync(exch: str) -> list[dict[str, Any]]:
    from kite_client import get_authenticated_kite

    kite = get_authenticated_kite()
    fresh = list(kite.instruments(exch) or [])
    with _lock:
        _cache[exch] = fresh
        _ts[exch] = time.time()
        _inflight[exch] = False
    return list(fresh)


def _refresh_bg(exch: str) -> None:
    try:
        _fetch_sync(exch)
    except Exception:
        with _lock:
            _inflight[exch] = False


def get_instruments(
    exchange: str,
    *,
    ttl: float = DEFAULT_TTL,
    force: bool = False,
    block: bool = False,
) -> list[dict[str, Any]]:
    """Return instruments for exchange (NFO/BFO/…).

    Default is non-blocking: serve cache (even stale) and refresh in background.
    Use block=True only from offline tools / boot warmers — never from the tick.
    """
    exch = str(exchange or "NFO").upper()
    now = time.time()
    with _lock:
        rows = _cache.get(exch)
        age = now - float(_ts.get(exch) or 0)
        fresh_enough = rows is not None and not force and age < ttl
        if fresh_enough:
            return list(rows)
        already = bool(_inflight.get(exch))
        if not already:
            _inflight[exch] = True
        stale = list(rows) if rows is not None else []

    if block and not already:
        try:
            return _fetch_sync(exch)
        except Exception:
            return stale

    if not already:
        threading.Thread(
            target=_refresh_bg,
            args=(exch,),
            name=f"kite-cache-{exch}",
            daemon=True,
        ).start()
    return stale


def prefetch_async(exchange: str = "NFO") -> None:
    """Warm cache on a daemon thread (safe at boot)."""
    get_instruments(str(exchange or "NFO").upper(), force=False, block=False)

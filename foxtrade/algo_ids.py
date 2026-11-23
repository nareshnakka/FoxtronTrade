"""Canonical algo ids: Algo-CW / Algo-NW (legacy algo2 / algo2nw aliases)."""
from __future__ import annotations

# New id ← legacy ids that still appear in trades / runtime / deep-links
_CANON: dict[str, str] = {
    "algo_cw": "algo_cw",
    "algo_nw": "algo_nw",
    "algo2": "algo_cw",
    "algo2nw": "algo_nw",
    "algo_cw_void": "algo_cw_void",
    "algo2_void": "algo_cw_void",
}

_ALIASES: dict[str, frozenset[str]] = {
    "algo_cw": frozenset({"algo_cw", "algo2"}),
    "algo_nw": frozenset({"algo_nw", "algo2nw"}),
    "algo_cw_void": frozenset({"algo_cw_void", "algo2_void"}),
}

DISPLAY: dict[str, str] = {
    "algo_cw": "Algo-CW",
    "algo_nw": "Algo-NW",
    "algo1": "Algo1",
    "algo_cw_void": "Algo-CW void",
}


def canon(algo: str | None) -> str:
    a = str(algo or "").strip().lower()
    return _CANON.get(a, a)


def aliases(algo: str | None) -> frozenset[str]:
    c = canon(algo)
    return _ALIASES.get(c, frozenset({c}) if c else frozenset())


def matches(trade_algo: str | None, want: str | None) -> bool:
    if not want:
        return True
    return canon(trade_algo) == canon(want) or str(trade_algo or "") in aliases(want)


def display(algo: str | None) -> str:
    c = canon(algo)
    return DISPLAY.get(c, c or "—")

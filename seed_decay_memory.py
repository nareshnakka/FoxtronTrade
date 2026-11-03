"""
Backfill data/decay_memory.json from last_3_months synth CE/PE day bars
so post-decay entry can fire on day-1 without waiting for live history.
"""
from __future__ import annotations

import json
from pathlib import Path

from analyze_crush_bounce import build_week_series, _sessions
from foxtrade.decay_memory import DecayMemory, PATH

ROOT = Path(__file__).resolve().parent


def main() -> None:
    sessions = _sessions()
    rows = build_week_series(sessions)
    mem = DecayMemory()
    days = mem.db.setdefault("days", {})
    seeded = 0
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        # Don't overwrite a live finalized/today row that already has real quotes
        existing = days.get(d) or {}
        if existing.get("source") == "live" or (
            existing.get("finalized") and existing.get("symbol_ce")
        ):
            continue
        days[d] = {
            "date": d,
            "finalized": True,
            "source": "synth_seed",
            "strike": r.get("strike"),
            "expiry": r.get("week"),
            "ce_open": r.get("ce_open"),
            "ce_high": r.get("ce_high"),
            "ce_low": r.get("ce_low"),
            "ce_close": r.get("ce_close"),
            "pe_open": r.get("pe_open"),
            "pe_high": r.get("pe_high"),
            "pe_low": r.get("pe_low"),
            "pe_close": r.get("pe_close"),
            "spot_open": r.get("spot_open"),
            "spot_close": r.get("spot_close"),
        }
        seeded += 1
    mem.db["days"] = days
    mem._dirty = True
    mem._maybe_save(force=True)
    sig = mem.analyze(
        {
            "post_decay_entry_enabled": True,
            "post_decay_ce_half_pct": 50.0,
            "post_decay_pe_crush_pct": 60.0,
            "post_decay_pe_low_max": 150.0,
            "post_decay_streak_days": 3,
            "post_decay_min_confidence": 0.62,
        }
    )
    print(json.dumps({
        "path": str(PATH),
        "days_stored": len(days),
        "seeded": seeded,
        "signal": sig.as_dict(),
    }, indent=2))


if __name__ == "__main__":
    main()

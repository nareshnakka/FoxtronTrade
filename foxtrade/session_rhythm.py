"""
Prior-session expansion rhythm from multi-day spot candles.

Purpose: answer "were the last 2-3 sessions quiet?" before an Algo2 entry.
Measured on Nifty index data, not option premiums, because expired option
premium history is unavailable. A session is classified by one-way excursion
from its open — the move a long straddle leg actually gets to harvest.

Empirical note (746 Nifty sessions, 2023-11 .. 2026-11): 2-3 quiet sessions do
NOT reliably predict a 3x-4x day; quiet regimes persist. Only a 4+ quiet streak
shows a measurable lift in extreme one-way moves, and even then it is ~8-16%,
not a certainty. So this module reports readiness; it must never force an entry.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(IST)
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(IST).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def session_stats(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group candles by IST session and return open/high/low/close per day."""
    days: dict[date, dict[str, Any]] = {}
    for row in candles or []:
        day = _as_date(row.get("date") or row.get("ts"))
        if day is None:
            continue
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        cur = days.get(day)
        if cur is None:
            days[day] = {
                "date": day.isoformat(),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            }
        else:
            cur["high"] = max(cur["high"], h)
            cur["low"] = min(cur["low"], l)
            cur["close"] = c

    out: list[dict[str, Any]] = []
    for day in sorted(days):
        row = days[day]
        o = row["open"]
        if o <= 0:
            continue
        up = max(0.0, row["high"] - o)
        down = max(0.0, o - row["low"])
        out.append(
            {
                **row,
                "exc_pct": round(max(up, down) / o * 100.0, 3),
                "range_pct": round((row["high"] - row["low"]) / o * 100.0, 3),
                "body_pct": round((row["close"] - o) / o * 100.0, 3),
            }
        )
    return out


def compute_rhythm(
    candles: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    *,
    today: date | str | None = None,
) -> dict[str, Any]:
    """Classify completed sessions strictly before `today` (no lookahead)."""
    cfg = cfg or {}
    quiet_max = float(cfg.get("rhythm_quiet_max_pct", 1.05))
    big_min = float(cfg.get("rhythm_big_min_pct", 1.05))
    huge_min = float(cfg.get("rhythm_huge_min_pct", 1.60))
    watch_min = int(cfg.get("rhythm_watch_quiet_sessions", 4))

    day = _as_date(today) if today is not None else datetime.now(IST).date()
    if day is None:
        return _empty(quiet_max, big_min, huge_min, watch_min)

    sessions = [s for s in session_stats(candles) if _as_date(s["date"]) < day]
    if not sessions:
        return _empty(quiet_max, big_min, huge_min, watch_min)

    for s in sessions:
        exc = float(s["exc_pct"])
        if exc >= huge_min:
            s["class"] = "huge"
        elif exc >= big_min:
            s["class"] = "big"
        elif exc < quiet_max:
            s["class"] = "quiet"
        else:
            s["class"] = "normal"

    quiet_streak = 0
    for s in reversed(sessions):
        if s["class"] != "quiet":
            break
        quiet_streak += 1

    last_big_ago = None
    last_huge_ago = None
    for ago, s in enumerate(reversed(sessions), start=1):
        if last_big_ago is None and s["class"] in ("big", "huge"):
            last_big_ago = ago
        if last_huge_ago is None and s["class"] == "huge":
            last_huge_ago = ago
        if last_big_ago is not None and last_huge_ago is not None:
            break

    expansion_watch = quiet_streak >= watch_min
    if expansion_watch:
        regime = "expansion-watch"
    elif quiet_streak >= 2:
        regime = "quiet-persist"
    elif last_big_ago == 1:
        regime = "post-big"
    else:
        regime = "normal"

    return {
        "as_of": day.isoformat(),
        "last_sessions": sessions[-5:],
        "quiet_streak": quiet_streak,
        "last_big_ago": last_big_ago,
        "last_huge_ago": last_huge_ago,
        "regime": regime,
        "expansion_watch": expansion_watch,
        "thresholds": {
            "quiet_below_pct": quiet_max,
            "big_at_pct": big_min,
            "huge_at_pct": huge_min,
            "watch_quiet_sessions": watch_min,
        },
        "basis": "nifty spot one-way excursion from session open",
    }


def _empty(quiet_max: float, big_min: float, huge_min: float, watch_min: int) -> dict[str, Any]:
    return {
        "as_of": None,
        "last_sessions": [],
        "quiet_streak": 0,
        "last_big_ago": None,
        "last_huge_ago": None,
        "regime": "unknown",
        "expansion_watch": False,
        "thresholds": {
            "quiet_below_pct": quiet_max,
            "big_at_pct": big_min,
            "huge_at_pct": huge_min,
            "watch_quiet_sessions": watch_min,
        },
        "basis": "nifty spot one-way excursion from session open",
    }


def rhythm_tag(rhythm: dict[str, Any] | None) -> str:
    """Short dashboard suffix, empty when nothing worth saying."""
    if not rhythm:
        return ""
    streak = int(rhythm.get("quiet_streak") or 0)
    if streak >= int((rhythm.get("thresholds") or {}).get("watch_quiet_sessions", 4)):
        return f" · {streak} quiet (watch)"
    if streak >= 2:
        return f" · {streak} quiet"
    if rhythm.get("regime") == "post-big":
        return " · post-big"
    return ""


def rhythm_payload(rhythm: dict[str, Any] | None) -> dict[str, Any]:
    """Compact copy stored on trade rows for later study."""
    if not rhythm:
        return {}
    return {
        "regime": rhythm.get("regime"),
        "quiet_streak": rhythm.get("quiet_streak"),
        "last_big_ago": rhythm.get("last_big_ago"),
        "expansion_watch": rhythm.get("expansion_watch"),
        "last_sessions": [
            {
                "date": s.get("date"),
                "exc_pct": s.get("exc_pct"),
                "class": s.get("class"),
            }
            for s in (rhythm.get("last_sessions") or [])
        ],
    }

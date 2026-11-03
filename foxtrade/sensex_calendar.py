"""
Sensex trading calendar for dual book.

Rules:
  • Always: Tuesday + Wednesday
  • Thursday: only if gamma blast detected (optional sleeve)
  • Weekly expiry = Tuesday → Monday (day-before) is hard cutoff
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.sensex_gamma import detect_sensex_gamma_blast

IST = ZoneInfo("Asia/Kolkata")

# Python weekday: Mon=0 … Sun=6
SENSEX_EXPIRY_WEEKDAY = 1  # Tuesday
SENSEX_TRADE_WEEKDAYS = (1, 2)  # Tuesday, Wednesday — always on
SENSEX_CONDITIONAL_WEEKDAYS = (3,)  # Thursday — gamma-only
SENSEX_PRE_EXPIRY_WEEKDAY = 0  # Monday — cutoff (day before expiry)


def _as_date(d: date | datetime | str | None) -> date | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.astimezone(IST).date() if d.tzinfo else d.date()
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except ValueError:
        return None


def sensex_expiry_weekday(cfg: dict[str, Any] | None = None) -> int:
    c = cfg or {}
    return int(c.get("sensex_expiry_weekday", SENSEX_EXPIRY_WEEKDAY))


def sensex_trade_weekdays(cfg: dict[str, Any] | None = None) -> tuple[int, ...]:
    """Unconditional Sensex days (default Tue/Wed)."""
    c = cfg or {}
    raw = c.get("sensex_trade_weekdays")
    if raw is None:
        return SENSEX_TRADE_WEEKDAYS
    return tuple(int(x) for x in raw)


def sensex_conditional_weekdays(cfg: dict[str, Any] | None = None) -> tuple[int, ...]:
    """Days that need a gamma blast to arm (default Thursday)."""
    c = cfg or {}
    if c.get("sensex_thursday_gamma_enabled", True) is False:
        return tuple()
    raw = c.get("sensex_conditional_weekdays")
    if raw is None:
        return SENSEX_CONDITIONAL_WEEKDAYS
    return tuple(int(x) for x in raw)


def is_sensex_expiry_day(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    dd = _as_date(d) or datetime.now(IST).date()
    return dd.weekday() == sensex_expiry_weekday(cfg)


def is_sensex_pre_expiry_cutoff(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Monday (day before Tuesday expiry) — do not plan Sensex."""
    dd = _as_date(d) or datetime.now(IST).date()
    c = cfg or {}
    if c.get("sensex_block_pre_expiry", True) is False:
        return False
    pre = int(c.get("sensex_pre_expiry_weekday", SENSEX_PRE_EXPIRY_WEEKDAY))
    return dd.weekday() == pre


def is_sensex_gamma_conditional_day(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    dd = _as_date(d) or datetime.now(IST).date()
    if is_sensex_pre_expiry_cutoff(dd, cfg):
        return False
    return dd.weekday() in sensex_conditional_weekdays(cfg)


def is_sensex_calendar_eligible(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    Session file may be considered for Sensex (Tue/Wed always, Thu conditional slot).
    Does not require gamma yet — use is_sensex_trade_day(..., closes=) to arm.
    """
    dd = _as_date(d) or datetime.now(IST).date()
    if is_sensex_pre_expiry_cutoff(dd, cfg):
        return False
    wd = dd.weekday()
    return wd in sensex_trade_weekdays(cfg) or wd in sensex_conditional_weekdays(cfg)


def is_sensex_trade_day(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
    closes: list[float] | None = None,
) -> bool:
    """
    True when Sensex may take new entries now.
      • Tue/Wed: always (subject to Mon cutoff)
      • Thu: only if gamma blast detected on `closes`
    """
    dd = _as_date(d) or datetime.now(IST).date()
    if is_sensex_pre_expiry_cutoff(dd, cfg):
        return False
    wd = dd.weekday()
    if wd in sensex_trade_weekdays(cfg):
        return True
    if wd in sensex_conditional_weekdays(cfg):
        c = cfg or {}
        # If flag says Thursday not gamma-gated, treat as normal trade day
        if c.get("sensex_thursday_gamma_only", True) is False:
            return True
        if not closes or len(closes) < 40:
            return False
        return bool(detect_sensex_gamma_blast(closes, cfg=c).get("allow"))
    return False


def sensex_day_role(
    d: date | datetime | str | None = None,
    cfg: dict[str, Any] | None = None,
    closes: list[float] | None = None,
) -> str:
    """Label for logs / reports."""
    dd = _as_date(d) or datetime.now(IST).date()
    if is_sensex_pre_expiry_cutoff(dd, cfg):
        return "PRE_EXPIRY_CUTOFF"
    if is_sensex_expiry_day(dd, cfg):
        return "EXPIRY_GAMMA"
    if is_sensex_gamma_conditional_day(dd, cfg):
        if closes and detect_sensex_gamma_blast(closes, cfg=cfg).get("allow"):
            return "THU_GAMMA_ARMED"
        return "THU_GAMMA_WATCH"
    if dd.weekday() in sensex_trade_weekdays(cfg):
        return "POST_EXPIRY_TRADE"
    return "OFF"

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")

_MONTHLY = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
# Weekly month code in Zerodha symbols: 1–9, O, N, D
_WEEKLY_MON = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "O": 10,
    "N": 11,
    "D": 12,
}

_RE_WEEKLY = re.compile(
    r"^NIFTY(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$",
    re.IGNORECASE,
)
_RE_MONTHLY = re.compile(
    r"^NIFTY(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+)(CE|PE)$",
    re.IGNORECASE,
)

# symbol → expiry date cache (from Kite instruments)
_SYMBOL_EXPIRY: dict[str, date] = {}
_TODAY_IS_EXPIRY: bool | None = None
_TODAY_IS_EXPIRY_ON: date | None = None


def _as_date(value: Any) -> date:
    """Normalize Kite expiry (date or datetime) to date."""
    if isinstance(value, datetime):
        return value.astimezone(IST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def parse_expiry_from_symbol(symbol: str | None) -> date | None:
    """
    Infer option expiry from Zerodha NIFTY symbol when possible.
    Weekly: NIFTY26N0322600PE → 2026-11-03
    Monthly: NIFTY26OCT22850PE → month only (day resolved via instrument cache)
    """
    if not symbol:
        return None
    sym = str(symbol).upper().strip()
    m = _RE_WEEKLY.match(sym)
    if m:
        yy, mc, dd = int(m.group(1)), m.group(2).upper(), int(m.group(3))
        month = _WEEKLY_MON.get(mc)
        if not month:
            return None
        try:
            return date(2000 + yy, month, dd)
        except ValueError:
            return None
    return None  # monthly needs instrument map for exact day


def _refresh_symbol_expiry_cache(nifty_opts: list[dict[str, Any]] | None = None) -> None:
    global _SYMBOL_EXPIRY, _TODAY_IS_EXPIRY, _TODAY_IS_EXPIRY_ON
    today = datetime.now(IST).date()
    if nifty_opts is None:
        try:
            kite = get_authenticated_kite()
            nifty_opts = [
                i
                for i in kite.instruments("NFO")
                if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")
            ]
        except Exception:
            return
    _SYMBOL_EXPIRY = {
        str(i["tradingsymbol"]).upper(): _as_date(i["expiry"]) for i in nifty_opts
    }
    _TODAY_IS_EXPIRY = today in set(_SYMBOL_EXPIRY.values())
    _TODAY_IS_EXPIRY_ON = today


def today_is_expiry_day(nifty_opts: list[dict[str, Any]] | None = None) -> bool:
    """True when today is a listed NIFTY options expiry (IST)."""
    today = datetime.now(IST).date()
    if _TODAY_IS_EXPIRY_ON == today and _TODAY_IS_EXPIRY is not None and nifty_opts is None:
        return _TODAY_IS_EXPIRY
    if nifty_opts is not None:
        return today in {_as_date(i["expiry"]) for i in nifty_opts}
    _refresh_symbol_expiry_cache(None)
    return bool(_TODAY_IS_EXPIRY)


def resolve_contract_expiry(
    *,
    expiry: Any = None,
    symbol: str | None = None,
) -> date | None:
    """Best-effort contract expiry: explicit field → weekly symbol → Kite map → monthly same-month."""
    if expiry is not None and str(expiry).strip():
        try:
            return _as_date(expiry)
        except Exception:
            pass
    parsed = parse_expiry_from_symbol(symbol)
    if parsed is not None:
        return parsed
    if symbol:
        sym = str(symbol).upper().strip()
        if not _SYMBOL_EXPIRY:
            _refresh_symbol_expiry_cache(None)
        if sym in _SYMBOL_EXPIRY:
            return _SYMBOL_EXPIRY[sym]
        # Monthly NIFTY26OCT… on an expiry day in that month → treat as that expiry day
        m = _RE_MONTHLY.match(sym)
        if m:
            yy, mon = int(m.group(1)), _MONTHLY[m.group(2).upper()]
            today = datetime.now(IST).date()
            if 2000 + yy == today.year and mon == today.month and today_is_expiry_day():
                return today
    return None


def is_expiry_day(today: date | None = None, expiries: list[Any] | None = None) -> bool:
    """True when today is a listed NIFTY option expiry."""
    d = today or datetime.now(IST).date()
    if expiries is None:
        return today_is_expiry_day()
    return any(_as_date(e) == d for e in expiries)


def _expiries(nifty_opts: list[dict[str, Any]], today) -> tuple[date, date]:
    """
    Label expiries: current_expiry (trade week) + next_expiry (label/rolls only).

    Rules for the TRADE week (current_expiry):
      - Normal day: current listed weekly (first expiry >= today)
      - Expiry day: immediate NEXT weekly (skip today's 0DTE — never skip a further week)
    """
    today_d = _as_date(today)
    future = sorted(
        {
            _as_date(i["expiry"])
            for i in nifty_opts
            if _as_date(i["expiry"]) >= today_d
        }
    )
    if not future:
        raise RuntimeError("No future NIFTY expiries found")
    # Expiry day: trade immediate next week; else trade current week
    if future[0] == today_d:
        current = future[1] if len(future) > 1 else future[0]
        nxt = future[2] if len(future) > 2 else current
    else:
        current = future[0]
        nxt = future[1] if len(future) > 1 else future[0]
    return current, nxt


def trade_expiries_for_scan(
    nifty_opts: list[dict[str, Any]],
    today: date | None = None,
) -> tuple[date, date, set[date], bool]:
    """
    Returns (current_expiry, next_expiry, trade_universe, expiry_day).

    Trade universe is ALWAYS a single week:
      - Expiry day → immediate next week only
      - All other days → current week only
    Never both weeks; never skip-week on expiry day.
    """
    today_d = today or datetime.now(IST).date()
    cur_exp, next_exp = _expiries(nifty_opts, today_d)
    expiry_day = today_d in {_as_date(i["expiry"]) for i in nifty_opts}
    # Always exactly one trade week (= current_expiry from _expiries)
    trade = {cur_exp}
    return cur_exp, next_exp, trade, expiry_day


def primary_trade_week(today: date | None = None) -> date | None:
    """
    The one weekly expiry allowed for new entries today.
      - Expiry day → first expiry AFTER today
      - Otherwise → first expiry ON/AFTER today (current week)
    """
    today_d = today or datetime.now(IST).date()
    if not _SYMBOL_EXPIRY:
        _refresh_symbol_expiry_cache(None)
    future_ge = sorted({e for e in _SYMBOL_EXPIRY.values() if e >= today_d})
    if not future_ge:
        _refresh_symbol_expiry_cache(None)
        future_ge = sorted({e for e in _SYMBOL_EXPIRY.values() if e >= today_d})
    if not future_ge:
        return None
    if future_ge[0] == today_d:
        # Expiry day → immediate next week
        return future_ge[1] if len(future_ge) > 1 else None
    return future_ge[0]


# Back-compat alias
def immediate_next_week_expiry(today: date | None = None) -> date | None:
    return primary_trade_week(today)


def expiry_allowed_for_entry(
    expiry: Any = None,
    today: date | None = None,
    *,
    symbol: str | None = None,
) -> bool:
    """
    New entries must match the single trade week for today:
      - Expiry day → next week only (not 0DTE, not week-after)
      - Other days → current week only (not next-next week)
    """
    today_d = today or datetime.now(IST).date()
    resolved = resolve_contract_expiry(expiry=expiry, symbol=symbol)
    if resolved is None:
        return False
    if resolved <= today_d:
        return False
    trade_week = primary_trade_week(today_d)
    if trade_week is None:
        return False
    return resolved == trade_week


def reject_same_day_expiry(
    *,
    symbol: str,
    expiry: Any = None,
) -> str | None:
    """Return a reason string if this contract must not be entered; else None."""
    if expiry_allowed_for_entry(expiry, symbol=symbol):
        return None
    resolved = resolve_contract_expiry(expiry=expiry, symbol=symbol)
    today_d = datetime.now(IST).date()
    trade_week = primary_trade_week(today_d)
    if resolved is not None and resolved <= today_d:
        return (
            f"blocked same-day/0DTE expiry {resolved} for {symbol} "
            f"— trade week is {trade_week}"
        )
    return (
        f"blocked wrong-week expiry {resolved or '?'} for {symbol} "
        f"— today trade week is {trade_week} only"
    )


def scan_premium_band(
    premium_low: float = 125,
    premium_high: float = 145,
    strike_window: float = 600,
    *,
    wide_low: float | None = None,
    wide_high: float | None = None,
) -> dict[str, Any]:
    """
    Collect NFO NIFTY CE/PE in premium band.
    Optional wide_low/wide_high also keep legs for late-start history dig
    (e.g. crushed to 50 or expanded to 200).
    """
    kite = get_authenticated_kite()
    spot = float(kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"])
    atm = round(spot / 50) * 50
    today = datetime.now(IST).date()
    w_low = wide_low if wide_low is not None else premium_low
    w_high = wide_high if wide_high is not None else premium_high

    inst = kite.instruments("NFO")
    nifty = [i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    _refresh_symbol_expiry_cache(nifty)
    cur_exp, next_exp, trade_exps, expiry_day = trade_expiries_for_scan(nifty, today)
    # HARD: never scan today's 0DTE. On expiry day: immediate next week ONLY (no skip-week).
    if not trade_exps:
        raise RuntimeError(f"No post-today trade expiries (today={today}, cur={cur_exp}, next={next_exp})")

    candidates: list[dict[str, Any]] = []
    for exp in trade_exps:
        if exp <= today:
            continue
        subset = [
            i
            for i in nifty
            if _as_date(i["expiry"]) == exp and abs(float(i["strike"]) - atm) <= strike_window
        ]
        keys = [f"NFO:{i['tradingsymbol']}" for i in subset]
        quotes: dict[str, Any] = {}
        for i in range(0, len(keys), 400):
            quotes.update(kite.ltp(keys[i : i + 400]))
        for i in subset:
            sym = str(i["tradingsymbol"])
            exp_d = _as_date(i["expiry"])
            # Belt + suspenders: field and symbol must both be after today
            if exp_d <= today:
                continue
            if not expiry_allowed_for_entry(exp_d, today, symbol=sym):
                continue
            lp = quotes.get(f"NFO:{sym}", {}).get("last_price")
            if lp is None:
                continue
            lp_f = float(lp)
            if w_low <= lp_f <= w_high:
                candidates.append(
                    {
                        "tradingsymbol": sym,
                        "instrument_token": int(i["instrument_token"]),
                        "instrument_type": i["instrument_type"],
                        "strike": float(i["strike"]),
                        "expiry": str(exp_d),
                        "ltp": lp_f,
                        "in_core_band": premium_low <= lp_f <= premium_high,
                    }
                )

    pair = _best_convergence_pair(candidates, premium_low, premium_high)
    return {
        "spot": spot,
        "atm": atm,
        "current_expiry": str(cur_exp),
        "next_expiry": str(next_exp),
        "expiry_day": expiry_day,
        "candidates": candidates,
        "pair": pair,
    }


def _best_convergence_pair(
    candidates: list[dict[str, Any]],
    low: float,
    high: float,
) -> dict[str, Any] | None:
    """Prefer same-strike CE/PE closest in premium inside band."""
    by_key: dict[tuple[str, float], dict[str, dict[str, Any]]] = {}
    for c in candidates:
        key = (c["expiry"], c["strike"])
        by_key.setdefault(key, {})[c["instrument_type"]] = c

    best = None
    best_key = None  # (-mid, gap) → prefer higher merge mid, then tighter gap
    for (_exp, _strike), sides in by_key.items():
        if "CE" not in sides or "PE" not in sides:
            continue
        ce, pe = sides["CE"], sides["PE"]
        gap = abs(ce["ltp"] - pe["ltp"])
        mid = (ce["ltp"] + pe["ltp"]) / 2
        if not (low <= mid <= high):
            continue
        # Among in-band merges: highest mid wins; equal mid → tighter gap
        key = (-mid, gap)
        if best_key is None or key < best_key:
            best_key = key
            best = {
                "expiry": ce["expiry"],
                "strike": ce["strike"],
                "ce": ce,
                "pe": pe,
                "gap": round(gap, 2),
                "mid": round(mid, 2),
            }

    # fallback: closest CE and PE by premium even if different strikes
    if best is None:
        ces = [c for c in candidates if c["instrument_type"] == "CE"]
        pes = [c for c in candidates if c["instrument_type"] == "PE"]
        for ce in ces:
            for pe in pes:
                if ce["expiry"] != pe["expiry"]:
                    continue
                gap = abs(ce["ltp"] - pe["ltp"])
                mid = (ce["ltp"] + pe["ltp"]) / 2
                if not (low <= mid <= high):
                    continue
                key = (-mid, gap)
                if best_key is None or key < best_key:
                    best_key = key
                    best = {
                        "expiry": ce["expiry"],
                        "strike": None,
                        "ce": ce,
                        "pe": pe,
                        "gap": round(gap, 2),
                        "mid": round(mid, 2),
                    }
    return best


def is_converged(ce_ltp: float, pe_ltp: float, tol: float, low: float, high: float) -> bool:
    mid = (ce_ltp + pe_ltp) / 2
    return abs(ce_ltp - pe_ltp) <= tol and low <= mid <= high


def find_next_week_option(
    *,
    side: str,
    strike: float,
    current_expiry: str | None = None,
) -> dict[str, Any] | None:
    """
    Same strike CE/PE on the next weekly expiry after current_expiry
    (or after today's current week if expiry omitted).
    """
    kite = get_authenticated_kite()
    today = datetime.now(IST).date()
    inst = kite.instruments("NFO")
    nifty = [i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] in ("CE", "PE")]
    cur_exp, next_exp = _expiries(nifty, today)
    # If we know the held contract's expiry and it is already "current", roll to next
    target_exp = next_exp
    if current_expiry:
        try:
            cur = _as_date(current_expiry)
            future = sorted({_as_date(i["expiry"]) for i in nifty if _as_date(i["expiry"]) > cur})
            if future:
                target_exp = future[0]
        except Exception:
            target_exp = next_exp
    # Never roll onto today's expiry
    if target_exp == today:
        target_exp = next_exp if next_exp != today else cur_exp

    side_u = side.upper()
    match = next(
        (
            i
            for i in nifty
            if _as_date(i["expiry"]) == target_exp
            and i["instrument_type"] == side_u
            and float(i["strike"]) == float(strike)
        ),
        None,
    )
    if not match:
        # nearest strike on next week
        pool = [
            i
            for i in nifty
            if _as_date(i["expiry"]) == target_exp and i["instrument_type"] == side_u
        ]
        if not pool:
            return None
        match = min(pool, key=lambda i: abs(float(i["strike"]) - float(strike)))

    key = f"NFO:{match['tradingsymbol']}"
    ltp = float(kite.ltp([key])[key]["last_price"])
    return {
        "tradingsymbol": match["tradingsymbol"],
        "instrument_token": int(match["instrument_token"]),
        "instrument_type": match["instrument_type"],
        "strike": float(match["strike"]),
        "expiry": str(match["expiry"]),
        "ltp": ltp,
        "current_expiry": str(cur_exp),
        "next_expiry": str(target_exp),
    }


def scan_premium_band_underlying(
    spec: dict[str, Any],
    *,
    premium_low: float | None = None,
    premium_high: float | None = None,
    strike_window: float | None = None,
    wide_low: float | None = None,
    wide_high: float | None = None,
) -> dict[str, Any]:
    """
    Generic Nifty/Sensex premium-band CE/PE scan.
    `spec` from foxtrade.underlyings (spot_symbol, opt_exchange, opt_name, …).
    """
    kite = get_authenticated_kite()
    spot_sym = str(spec["spot_symbol"])
    prefix = str(spec.get("ltp_prefix") or spec["opt_exchange"])
    name = str(spec["opt_name"])
    step = float(spec.get("strike_step") or 50)
    window = float(strike_window if strike_window is not None else spec.get("strike_window") or 600)
    p_lo = float(premium_low if premium_low is not None else spec.get("premium_low") or 125)
    p_hi = float(premium_high if premium_high is not None else spec.get("premium_high") or 145)
    w_lo = float(wide_low if wide_low is not None else spec.get("wide_low") or p_lo)
    w_hi = float(wide_high if wide_high is not None else spec.get("wide_high") or p_hi)

    spot = float(kite.ltp([spot_sym])[spot_sym]["last_price"])
    atm = round(spot / step) * step
    today = datetime.now(IST).date()

    inst = kite.instruments(str(spec["opt_exchange"]))
    opts = [
        i
        for i in inst
        if i.get("name") == name and i.get("instrument_type") in ("CE", "PE")
    ]
    if name == "NIFTY":
        _refresh_symbol_expiry_cache(opts)
    cur_exp, next_exp, trade_exps, expiry_day = trade_expiries_for_scan(opts, today)
    if not trade_exps:
        raise RuntimeError(f"No trade expiries for {name}")

    candidates: list[dict[str, Any]] = []
    for exp in trade_exps:
        if exp <= today:
            continue
        subset = [
            i
            for i in opts
            if _as_date(i["expiry"]) == exp and abs(float(i["strike"]) - atm) <= window
        ]
        keys = [f"{prefix}:{i['tradingsymbol']}" for i in subset]
        quotes: dict[str, Any] = {}
        for i in range(0, len(keys), 400):
            quotes.update(kite.ltp(keys[i : i + 400]))
        for i in subset:
            sym = str(i["tradingsymbol"])
            exp_d = _as_date(i["expiry"])
            if exp_d is None or exp_d <= today:
                continue
            # Underlying-local trade week (avoid Nifty-only symbol gate for Sensex)
            if exp_d not in trade_exps:
                continue
            lp = quotes.get(f"{prefix}:{sym}", {}).get("last_price")
            if lp is None:
                continue
            lp_f = float(lp)
            if w_lo <= lp_f <= w_hi:
                candidates.append(
                    {
                        "tradingsymbol": sym,
                        "instrument_token": int(i["instrument_token"]),
                        "instrument_type": i["instrument_type"],
                        "strike": float(i["strike"]),
                        "expiry": str(exp_d),
                        "ltp": lp_f,
                        "in_core_band": p_lo <= lp_f <= p_hi,
                        "underlying": name,
                        "exchange": prefix,
                    }
                )

    pair = _best_convergence_pair(candidates, p_lo, p_hi)
    return {
        "underlying": name,
        "spot": spot,
        "atm": atm,
        "current_expiry": str(cur_exp),
        "next_expiry": str(next_exp),
        "expiry_day": expiry_day,
        "candidates": candidates,
        "pair": pair,
        "premium_low": p_lo,
        "premium_high": p_hi,
    }

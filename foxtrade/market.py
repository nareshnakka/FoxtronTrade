from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kite_client import get_authenticated_kite
from foxtrade.indicators import atr, candle_reversal_flags, ema, rsi, support_resistance
from foxtrade.session_trend import compute_session_trend, day_open_from_minutes

IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"
SENSEX_TOKEN = 265
SENSEX_SYMBOL = "BSE:SENSEX"


class MarketHub:
    """1s poller with rolling history (default 500s) for index + selected options."""

    def __init__(self, history_seconds: int = 500) -> None:
        self.history_seconds = int(history_seconds)
        self._lock = threading.Lock()
        self.spot_series: deque[tuple[float, float]] = deque(maxlen=self.history_seconds)
        self.spot_series_by_id: dict[str, deque[tuple[float, float]]] = {
            "NIFTY": deque(maxlen=self.history_seconds),
            "SENSEX": deque(maxlen=self.history_seconds),
        }
        self.option_series: dict[str, deque[tuple[float, float]]] = defaultdict(
            self._new_option_deque
        )
        self.last_spot: float | None = None
        self.last_spot_by_id: dict[str, float | None] = {"NIFTY": None, "SENSEX": None}
        self.last_quotes: dict[str, float] = {}
        self.quote_ts: dict[str, float] = {}
        self.last_data_ts: float | None = None  # last successful Zerodha LTP
        self.watch_tokens: dict[str, int] = {}  # symbol -> instrument_token
        self.watch_meta: dict[str, dict[str, Any]] = {}
        self.nifty_candles_5m: list[dict[str, Any]] = []
        self.nifty_candles_1m: list[dict[str, Any]] = []
        self.sensex_candles_1m: list[dict[str, Any]] = []
        self.day_open: float | None = None
        self.day_open_by_id: dict[str, float | None] = {"NIFTY": None, "SENSEX": None}
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dual_spot: bool = False  # poll Sensex when dual_mode

    def _new_option_deque(self) -> deque[tuple[float, float]]:
        return deque(maxlen=self.history_seconds)

    def resize_history(self, history_seconds: int) -> None:
        """Rebuild deques when config history_seconds changes (keeps existing points)."""
        n = max(60, int(history_seconds))
        with self._lock:
            self.history_seconds = n
            self.spot_series = deque(self.spot_series, maxlen=n)
            self.option_series = defaultdict(
                self._new_option_deque,
                {sym: deque(rows, maxlen=n) for sym, rows in self.option_series.items()},
            )

    def set_watch(self, instruments: list[dict[str, Any]], *, replace: bool = False) -> None:
        """Watch list for LTP polling. Default merges so dynamic strikes keep history."""
        with self._lock:
            if replace:
                self.watch_tokens = {}
                self.watch_meta = {}
            for i in instruments:
                sym = i.get("tradingsymbol")
                if not sym:
                    continue
                tok = int(i.get("instrument_token") or 0)
                # Never overwrite a real token with 0 (open trades often lack token until resolved)
                prev = int(self.watch_tokens.get(sym) or 0)
                if tok > 0:
                    self.watch_tokens[sym] = tok
                elif sym not in self.watch_tokens:
                    self.watch_tokens[sym] = 0
                else:
                    tok = prev
                meta = dict(i)
                meta["instrument_token"] = tok if tok > 0 else prev
                self.watch_meta[sym] = {**(self.watch_meta.get(sym) or {}), **meta}
            # prune series only when replace=True
            if replace:
                for sym in list(self.option_series.keys()):
                    if sym not in self.watch_tokens:
                        del self.option_series[sym]

    def start(self) -> None:
        """Dedicated LTP thread — keeps charts live even when engine does heavy Kite calls."""
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="market-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        stop = self._stop  # capture — soft-restart may replace self._stop
        while not stop.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
            elapsed = time.time() - t0
            # Snappy LTP for instant algo fire (was ~1s)
            time.sleep(max(0.08, 0.35 - elapsed))

    def _append_point(self, series: deque[tuple[float, float]], ts: float, value: float) -> None:
        """Keep at most one sample per whole second (lightweight-charts needs unique times)."""
        t = float(int(ts))
        if series and int(series[-1][0]) == int(t):
            series[-1] = (t, value)
        else:
            series.append((t, value))

    def poll_once(self) -> None:
        try:
            kite = get_authenticated_kite()
        except Exception as exc:  # noqa: BLE001
            # Keep last quotes; never freeze the LTP thread on auth/network
            self.error = f"auth/poll:{exc}"
            return
        now = time.time()
        try:
            keys = [NIFTY_SYMBOL]
            if self.dual_spot:
                keys.append(SENSEX_SYMBOL)
            with self._lock:
                symbols = list(self.watch_tokens.keys())
                meta = {s: dict(self.watch_meta.get(s) or {}) for s in symbols}
            for sym in symbols:
                exch = str(meta.get(sym, {}).get("exchange") or "NFO")
                if exch not in ("NFO", "BFO", "NSE", "BSE"):
                    # Infer from symbol
                    exch = "BFO" if str(sym).upper().startswith("SENSEX") else "NFO"
                keys.append(f"{exch}:{sym}")
            # Hard-timeout LTP — a hung Kite socket must not freeze market/engine.
            quotes: dict[str, Any] = {}
            if keys:
                box: dict[str, Any] = {"q": {}, "err": None}

                def _ltp() -> None:
                    try:
                        box["q"] = kite.ltp(keys) or {}
                    except Exception as exc:  # noqa: BLE001
                        box["err"] = exc

                th = threading.Thread(target=_ltp, name="market-ltp", daemon=True)
                th.start()
                th.join(timeout=8.0)
                if th.is_alive():
                    self.error = "ltp:timeout"
                    return
                if box["err"] is not None:
                    raise box["err"]
                quotes = box["q"]

            spot = quotes.get(NIFTY_SYMBOL, {}).get("last_price")
            sx = quotes.get(SENSEX_SYMBOL, {}).get("last_price") if self.dual_spot else None
            got = False
            with self._lock:
                if spot is not None:
                    self.last_spot = float(spot)
                    self.last_spot_by_id["NIFTY"] = float(spot)
                    self._append_point(self.spot_series, now, float(spot))
                    self._append_point(self.spot_series_by_id["NIFTY"], now, float(spot))
                    got = True
                if sx is not None:
                    self.last_spot_by_id["SENSEX"] = float(sx)
                    self._append_point(self.spot_series_by_id["SENSEX"], now, float(sx))
                    got = True
                for sym in symbols:
                    exch = str(meta.get(sym, {}).get("exchange") or "NFO")
                    if exch not in ("NFO", "BFO", "NSE", "BSE"):
                        exch = "BFO" if str(sym).upper().startswith("SENSEX") else "NFO"
                    lp = quotes.get(f"{exch}:{sym}", {}).get("last_price")
                    if lp is None and exch == "NFO":
                        lp = quotes.get(f"BFO:{sym}", {}).get("last_price")
                    if lp is None:
                        continue
                    self.last_quotes[sym] = float(lp)
                    self.quote_ts[sym] = now
                    self._append_point(self.option_series[sym], now, float(lp))
                    got = True
                if got:
                    self.last_data_ts = now
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    def refresh_nifty_candles(self) -> None:
        """Fetch day candles with a hard timeout — never freeze the engine loop."""
        import threading

        box: dict[str, Any] = {"err": None, "done": False, "c5": None, "c1": None, "opened": None}

        def _work() -> None:
            try:
                kite = get_authenticated_kite()
                to_dt = datetime.now(IST)
                from_dt = to_dt - timedelta(days=10)
                box["c5"] = kite.historical_data(NIFTY_TOKEN, from_dt, to_dt, "5minute")
                box["c1"] = kite.historical_data(NIFTY_TOKEN, from_dt, to_dt, "minute")
                rows = box["c1"] or []
                box["opened"] = day_open_from_minutes(
                    [float(c["close"]) for c in rows if c.get("close") is not None],
                    rows,
                )
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc
            finally:
                box["done"] = True

        th = threading.Thread(target=_work, name="nifty-candles", daemon=True)
        th.start()
        th.join(timeout=12.0)
        if not box["done"]:
            self.error = "candles:timeout"
            return
        if box["err"] is not None:
            self.error = f"candles:{box['err']}"
            return
        if box["c5"] is not None:
            self.nifty_candles_5m = box["c5"]
        if box["c1"] is not None:
            self.nifty_candles_1m = box["c1"]
        if box["opened"] is not None:
            self.day_open = box["opened"]
        self.error = None

    def nifty_day_candles(self, interval: str = "5minute") -> list[dict[str, Any]]:
        kite = get_authenticated_kite()
        to_dt = datetime.now(IST)
        from_dt = to_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        if to_dt.weekday() >= 5 or to_dt < from_dt:
            # weekend / pre-open: last session day candles
            from_dt = to_dt - timedelta(days=5)
        rows = kite.historical_data(NIFTY_TOKEN, from_dt, to_dt, interval)
        out = []
        for row in rows:
            dt = row["date"]
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            out.append(
                {
                    "time": int(dt.timestamp()),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        return out

    def _first_5m_opening_range(self, spot: float | None) -> dict[str, Any] | None:
        """
        First cash-session 5m candle (09:15–09:20 IST) as opening range.

        Rules (user playbook, combined with other signals elsewhere):
        - Break above OR high → usually fades → bias PE
        - Break below OR low → often recover / range unless confirmed further breakdown → bias CE,
          or PE if breakdown extends
        """
        candles = self.nifty_candles_5m or []
        if not candles:
            return None
        today = datetime.now(IST).date()
        first = None
        for c in candles:
            dt = c.get("date")
            if dt is None:
                continue
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            else:
                dt = dt.replace(tzinfo=IST) if hasattr(dt, "replace") else dt
            try:
                if dt.date() != today:
                    continue
                if dt.hour == 9 and dt.minute == 15:
                    first = c
                    break
            except Exception:
                continue
        if first is None:
            return None
        or_open = float(first["open"])
        or_high = float(first["high"])
        or_low = float(first["low"])
        or_close = float(first["close"])
        px = float(spot) if spot is not None else float(self.last_spot or or_close)
        # Confirmed breakdown: meaningfully below OR low (~0.12% or 25 pts)
        brk_buf = max(25.0, px * 0.0012)
        bias = "INSIDE"
        prefer = None
        note = "inside first 5m range"
        if px > or_high:
            bias = "FADE_HIGH"
            prefer = "PE"
            note = "broke above first 5m → fade (PE bias)"
        elif px < or_low - brk_buf:
            bias = "BREAKDOWN"
            prefer = "PE"
            note = "confirmed break below first 5m → continuation (PE bias)"
        elif px < or_low:
            bias = "EXPECT_RECOVER"
            prefer = "CE"
            note = "dip under first 5m → recover/range (CE bias)"
        return {
            "open": or_open,
            "high": or_high,
            "low": or_low,
            "close": or_close,
            "spot": px,
            "bias": bias,
            "prefer_side": prefer,
            "breakdown_buffer": round(brk_buf, 1),
            "note": note,
        }

    def index_context(self) -> dict[str, Any]:
        candles = self.nifty_candles_5m or []
        closes = [c["close"] for c in candles] if candles else []
        if not closes and self.spot_series:
            closes = [v for _, v in self.spot_series]
        ohlc = [
            {
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
            for c in candles
        ]
        flags = candle_reversal_flags(ohlc[-5:] if ohlc else [])
        sr = support_resistance(closes)
        day_range = None
        if ohlc:
            # approximate today's range from last session candles
            day_range = max(c["high"] for c in ohlc[-75:]) - min(c["low"] for c in ohlc[-75:])
        opening_range = self._first_5m_opening_range(self.last_spot)
        day_open = self.day_open
        if day_open is None:
            day_open = day_open_from_minutes(
                [float(c["close"]) for c in (self.nifty_candles_1m or []) if c.get("close") is not None],
                self.nifty_candles_1m,
            )
            if day_open is not None:
                self.day_open = day_open
        # Minute closes for today only (for momentum vs open). Use the live
        # merged series so decisions never run on 2-3 minute old candles.
        today_closes = self.session_closes_live("NIFTY")
        if not today_closes and self.spot_series:
            today_closes = [v for _, v in self.spot_series]
        # EMA on the same 1m series the backtest uses (not 5m candles — that
        # mismatch made live entries far slower than the validated profile).
        ema9_v = ema(today_closes, 9) if len(today_closes) >= 9 else None
        ema21_v = ema(today_closes, 21) if len(today_closes) >= 21 else None
        session_trend = compute_session_trend(
            spot=self.last_spot,
            day_open=day_open,
            closes=today_closes,
            ema9=ema9_v,
            ema21=ema21_v,
        )
        return {
            "spot": self.last_spot,
            "day_open": day_open,
            "ema9": ema9_v,
            "ema21": ema21_v,
            "rsi14": rsi(closes, 14) if closes else None,
            "atr14": atr(ohlc, 14) if ohlc else None,
            "day_range": day_range,
            "support": sr["support"],
            "resistance": sr["resistance"],
            "patterns": flags,
            "opening_range": opening_range,
            "session_trend": session_trend,
            "spot_closes": today_closes,
        }

    def spot_values(self) -> list[float]:
        with self._lock:
            return [v for _, v in self.spot_series]

    def spot_values_for(self, underlying: str = "NIFTY") -> list[float]:
        uid = str(underlying or "NIFTY").upper()
        with self._lock:
            rows = self.spot_series_by_id.get(uid) or self.spot_series
            return [v for _, v in rows]

    def quote_fresh(self, symbol: str, max_age: float = 20.0) -> bool:
        """False when the poller has not refreshed this symbol recently."""
        ts = self.quote_ts.get(symbol)
        return ts is not None and (time.time() - ts) <= max_age

    def _minute_closes_from_ticks(self, uid: str) -> dict[int, float]:
        today_start = datetime.combine(
            datetime.now(IST).date(), dt_time.min, tzinfo=IST
        ).timestamp()
        with self._lock:
            rows = list(self.spot_series_by_id.get(uid) or [])
        out: dict[int, float] = {}
        for t, v in rows:
            if t < today_start:
                continue
            out[int(t // 60)] = float(v)
        return out

    def session_closes_live(self, underlying: str = "NIFTY") -> list[float]:
        """
        Today's 1-minute closes including the in-progress minute.

        Kite 1m candles are merged with the tick feed resampled to minute
        closes. The last value is the current LTP, so live analysis sees the
        same series the minute backtest uses without multi-minute lag.
        """
        uid = str(underlying or "NIFTY").upper()
        rows = self.sensex_candles_1m if uid == "SENSEX" else self.nifty_candles_1m
        today = datetime.now(IST).date()
        merged: dict[int, float] = {}
        for r in rows or []:
            dt = r.get("date")
            if dt is None or r.get("close") is None:
                continue
            try:
                if getattr(dt, "tzinfo", None):
                    dt = dt.astimezone(IST)
                if dt.date() != today:
                    continue
                merged[int(dt.timestamp()) // 60] = float(r["close"])
            except Exception:
                continue
        merged.update(self._minute_closes_from_ticks(uid))
        if not merged:
            # No candles yet: fall back to today's ticks only. Yesterday's
            # closes must never seed the EMA before the open.
            today_start = datetime.combine(today, dt_time.min, tzinfo=IST).timestamp()
            with self._lock:
                rows = list(self.spot_series_by_id.get(uid) or [])
            return [float(v) for t, v in rows if t >= today_start]
        return [merged[k] for k in sorted(merged)]

    def session_closes_from_candles(self, underlying: str = "NIFTY") -> list[float]:
        """Today's 1m closes for directional analysis (preferred over 1s LTP series)."""
        uid = str(underlying or "NIFTY").upper()
        rows = self.sensex_candles_1m if uid == "SENSEX" else self.nifty_candles_1m
        if not rows:
            return self.spot_values_for(uid)
        out: list[float] = []
        today = datetime.now(IST).date()
        for r in rows:
            dt = r.get("date")
            if dt is None:
                continue
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            if hasattr(dt, "date") and dt.date() != today:
                continue
            if r.get("close") is not None:
                out.append(float(r["close"]))
        return out or self.spot_values_for(uid)

    def refresh_sensex_candles(self) -> None:
        """Fetch Sensex day 1m bars (dual mode)."""
        import threading

        box: dict[str, Any] = {"err": None, "done": False, "c1": None, "opened": None}

        def _work() -> None:
            try:
                kite = get_authenticated_kite()
                to_dt = datetime.now(IST)
                from_dt = to_dt - timedelta(days=5)
                box["c1"] = kite.historical_data(SENSEX_TOKEN, from_dt, to_dt, "minute")
                rows = box["c1"] or []
                box["opened"] = day_open_from_minutes(
                    [float(c["close"]) for c in rows if c.get("close") is not None],
                    rows,
                )
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc
            finally:
                box["done"] = True

        th = threading.Thread(target=_work, name="sensex-candles", daemon=True)
        th.start()
        th.join(timeout=12.0)
        if not box["done"]:
            return
        if box["err"] is not None:
            self.error = f"sensex_candles:{box['err']}"
            return
        if box["c1"] is not None:
            self.sensex_candles_1m = box["c1"]
        if box["opened"] is not None:
            self.day_open_by_id["SENSEX"] = box["opened"]

    def series_payload(self, symbol: str) -> list[dict[str, float]]:
        with self._lock:
            rows = list(self.option_series.get(symbol, []))
        out: list[dict[str, float]] = []
        for t, v in rows:
            pt = {"time": int(t), "value": float(v)}
            if out and out[-1]["time"] == pt["time"]:
                out[-1] = pt
            else:
                out.append(pt)
        return out

    def spot_payload(self) -> list[dict[str, float]]:
        with self._lock:
            rows = list(self.spot_series)
        out: list[dict[str, float]] = []
        for t, v in rows:
            pt = {"time": int(t), "value": float(v)}
            if out and out[-1]["time"] == pt["time"]:
                out[-1] = pt
            else:
                out.append(pt)
        return out


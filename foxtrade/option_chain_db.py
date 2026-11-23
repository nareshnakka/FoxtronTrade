"""
Live Nifty option-chain recorder → SQLite (analysis).

Captures every ~10s:
  - Index spot + ATM
  - Current-week + next-week CE/PE, ATM ± wing (default 20: ITM/ATM/OTM)

Captures every ~60s:
  - Same legs' OI / volume via kite.quote

Expiry rollover:
  - During session on expiry day: week0 = 0DTE, week1 = next
  - After 15:30 IST on expiry day (and overnight): skip 0DTE → next becomes current
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "option_chain_live" / "chain.db"
ARM_FILE = ROOT / "data" / "option_chain_live" / "capture_arm.json"

_STEP = 50
_CHUNK = 400


def _load_arm() -> dict[str, Any]:
    try:
        if ARM_FILE.exists():
            import json

            return json.loads(ARM_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_arm(*, day: str, capturing: bool) -> None:
    import json

    ARM_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARM_FILE.write_text(
        json.dumps({"day": day, "capturing": bool(capturing)}, indent=2),
        encoding="utf-8",
    )


def _now() -> datetime:
    return datetime.now(IST)


def _as_date(v: Any) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v)[:10])


def recording_expiries(
    nifty_opts: list[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[date, date, bool]:
    """
    Return (current_week, next_week, rolled_after_expiry).

    Always two listed weeks for capture. After 15:30 on expiry day the
    expiring series is dropped so current auto-switches to next week.
    """
    now = now or _now()
    today = now.date()
    future = sorted(
        {
            _as_date(i["expiry"])
            for i in nifty_opts
            if _as_date(i["expiry"]) >= today
        }
    )
    if not future:
        raise RuntimeError("No future NIFTY expiries found")
    after_close = now.time() >= dtime(15, 30)
    rolled = False
    if future[0] == today and after_close:
        # Expiry session finished — promote next week
        cur = future[1] if len(future) > 1 else future[0]
        nxt = future[2] if len(future) > 2 else cur
        rolled = len(future) > 1
    else:
        cur = future[0]
        nxt = future[1] if len(future) > 1 else future[0]
    return cur, nxt, rolled


def _in_session(now: datetime | None = None) -> bool:
    now = now or _now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 10) <= t <= dtime(15, 35)


class OptionChainDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                      key TEXT PRIMARY KEY,
                      value TEXT
                    );
                    CREATE TABLE IF NOT EXISTS ltp_ticks (
                      ts TEXT PRIMARY KEY,
                      day TEXT NOT NULL,
                      spot REAL,
                      atm INTEGER,
                      current_expiry TEXT,
                      next_expiry TEXT,
                      rolled INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS ltp_legs (
                      ts TEXT NOT NULL,
                      week TEXT NOT NULL,
                      expiry TEXT NOT NULL,
                      strike INTEGER NOT NULL,
                      side TEXT NOT NULL,
                      symbol TEXT NOT NULL,
                      ltp REAL,
                      PRIMARY KEY (ts, symbol)
                    );
                    CREATE INDEX IF NOT EXISTS idx_ltp_legs_day
                      ON ltp_legs(ts);
                    CREATE INDEX IF NOT EXISTS idx_ltp_legs_sym
                      ON ltp_legs(symbol, ts);
                    CREATE TABLE IF NOT EXISTS oi_snaps (
                      ts_minute TEXT NOT NULL,
                      day TEXT NOT NULL,
                      spot REAL,
                      atm INTEGER,
                      current_expiry TEXT,
                      next_expiry TEXT,
                      week TEXT NOT NULL,
                      expiry TEXT NOT NULL,
                      strike INTEGER NOT NULL,
                      side TEXT NOT NULL,
                      symbol TEXT NOT NULL,
                      ltp REAL,
                      oi INTEGER,
                      volume INTEGER,
                      oi_day_high INTEGER,
                      oi_day_low INTEGER,
                      PRIMARY KEY (ts_minute, symbol)
                    );
                    CREATE INDEX IF NOT EXISTS idx_oi_day ON oi_snaps(day);
                    CREATE INDEX IF NOT EXISTS idx_oi_sym ON oi_snaps(symbol, ts_minute);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def insert_ltp(
        self,
        *,
        ts: str,
        day: str,
        spot: float | None,
        atm: int | None,
        current_expiry: str,
        next_expiry: str,
        rolled: bool,
        legs: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ltp_ticks
                    (ts, day, spot, atm, current_expiry, next_expiry, rolled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        day,
                        spot,
                        atm,
                        current_expiry,
                        next_expiry,
                        1 if rolled else 0,
                    ),
                )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO ltp_legs
                    (ts, week, expiry, strike, side, symbol, ltp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            ts,
                            L["week"],
                            L["expiry"],
                            int(L["strike"]),
                            L["side"],
                            L["symbol"],
                            L.get("ltp"),
                        )
                        for L in legs
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def insert_oi(
        self,
        *,
        ts_minute: str,
        day: str,
        spot: float | None,
        atm: int | None,
        current_expiry: str,
        next_expiry: str,
        legs: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO oi_snaps
                    (ts_minute, day, spot, atm, current_expiry, next_expiry,
                     week, expiry, strike, side, symbol, ltp, oi, volume,
                     oi_day_high, oi_day_low)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            ts_minute,
                            day,
                            spot,
                            atm,
                            current_expiry,
                            next_expiry,
                            L["week"],
                            L["expiry"],
                            int(L["strike"]),
                            L["side"],
                            L["symbol"],
                            L.get("ltp"),
                            L.get("oi"),
                            L.get("volume"),
                            L.get("oi_day_high"),
                            L.get("oi_day_low"),
                        )
                        for L in legs
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def stats(self, *, timeout: float = 0.25) -> dict[str, Any]:
        # Never block Flask /api/state — COUNTs under a held writer lock wedged the UI.
        got = self._lock.acquire(timeout=max(0.05, float(timeout)))
        if not got:
            return {"db": str(self.path), "busy": True}
        try:
            conn = self._connect()
            try:
                last = conn.execute(
                    "SELECT ts, spot, atm, current_expiry, next_expiry FROM ltp_ticks "
                    "ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                # Approximate sizes via sqlite_stat / page count — avoid full table COUNT(*)
                # on every dashboard poll (ltp_legs is ~1M rows).
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                return {
                    "db": str(self.path),
                    "approx_bytes": int(page_count or 0) * int(page_size or 0),
                    "last_ts": last[0] if last else None,
                    "last_spot": last[1] if last else None,
                    "last_atm": last[2] if last else None,
                    "last_current_expiry": last[3] if last else None,
                    "last_next_expiry": last[4] if last else None,
                }
            finally:
                conn.close()
        finally:
            self._lock.release()

    def list_days(self) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT day FROM ltp_ticks ORDER BY day DESC"
                ).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

    def list_timestamps(
        self,
        day: str,
        *,
        time_from: str | None = None,
        time_to: str | None = None,
        offset: int = 0,
        limit: int = 100,
        week: str = "both",
        side: str = "both",
        moneyness: str = "all",
        depth: int = 10,
        ltp_min: float | None = None,
        ltp_max: float | None = None,
    ) -> dict[str, Any]:
        """
        Paginated tick timestamps for a day.

        When moneyness/ltp/week/side filters are set (beyond all/both), only
        snapshots that have at least one matching leg are returned.
        """
        offset_i = max(0, int(offset))
        limit_i = max(1, min(2000, int(limit)))
        depth_i = max(0, int(depth))
        step_pts = _STEP * depth_i
        mode = (moneyness or "all").lower()
        need_leg = (
            mode not in ("", "all")
            or week in ("current", "next")
            or side in ("CE", "PE")
            or ltp_min is not None
            or ltp_max is not None
        )
        with self._lock:
            conn = self._connect()
            try:
                where = ["t.day=?"]
                args: list[Any] = [day]
                if time_from:
                    where.append("substr(t.ts, 12, 8) >= ?")
                    args.append(_norm_hms(time_from))
                if time_to:
                    where.append("substr(t.ts, 12, 8) <= ?")
                    args.append(_norm_hms(time_to))

                if need_leg:
                    exists = [
                        "EXISTS (SELECT 1 FROM ltp_legs L WHERE L.ts = t.ts"
                    ]
                    if week in ("current", "next"):
                        exists.append("AND L.week=?")
                        args.append(week)
                    if side in ("CE", "PE"):
                        exists.append("AND L.side=?")
                        args.append(side)
                    if ltp_min is not None:
                        exists.append("AND L.ltp IS NOT NULL AND L.ltp >= ?")
                        args.append(float(ltp_min))
                    if ltp_max is not None:
                        exists.append("AND L.ltp IS NOT NULL AND L.ltp <= ?")
                        args.append(float(ltp_max))
                    if mode == "wing":
                        exists.append(
                            "AND t.atm IS NOT NULL AND ABS(L.strike - t.atm) <= ?"
                        )
                        args.append(int(step_pts))
                    elif mode == "atm":
                        exists.append("AND t.atm IS NOT NULL AND L.strike = t.atm")
                    elif mode == "itm":
                        exists.append(
                            "AND t.atm IS NOT NULL AND ("
                            "(L.side='CE' AND L.strike < t.atm "
                            "AND ((t.atm - L.strike) / ?) BETWEEN 1 AND ?) OR "
                            "(L.side='PE' AND L.strike > t.atm "
                            "AND ((L.strike - t.atm) / ?) BETWEEN 1 AND ?)"
                            ")"
                        )
                        args.extend([_STEP, depth_i, _STEP, depth_i])
                    elif mode == "otm":
                        exists.append(
                            "AND t.atm IS NOT NULL AND ("
                            "(L.side='CE' AND L.strike > t.atm "
                            "AND ((L.strike - t.atm) / ?) BETWEEN 1 AND ?) OR "
                            "(L.side='PE' AND L.strike < t.atm "
                            "AND ((t.atm - L.strike) / ?) BETWEEN 1 AND ?)"
                            ")"
                        )
                        args.extend([_STEP, depth_i, _STEP, depth_i])
                    exists.append(")")
                    where.append(" ".join(exists))

                where_sql = " AND ".join(where)
                total = conn.execute(
                    f"SELECT COUNT(*) FROM ltp_ticks t WHERE {where_sql}",
                    args,
                ).fetchone()[0]

                rows = conn.execute(
                    f"SELECT t.ts, t.spot, t.atm, t.current_expiry, t.next_expiry "
                    f"FROM ltp_ticks t WHERE {where_sql} "
                    f"ORDER BY t.ts ASC LIMIT ? OFFSET ?",
                    [*args, limit_i, offset_i],
                ).fetchall()
                times = [
                    {
                        "ts": r[0],
                        "time": str(r[0])[11:19],
                        "spot": r[1],
                        "atm": r[2],
                        "current_expiry": r[3],
                        "next_expiry": r[4],
                        "index": offset_i + i,
                    }
                    for i, r in enumerate(rows)
                ]
                pages = max(1, (int(total) + limit_i - 1) // limit_i) if total else 1
                page = (offset_i // limit_i) + 1 if total else 1
                return {
                    "day": day,
                    "times": times,
                    "total": int(total),
                    "offset": offset_i,
                    "limit": limit_i,
                    "page": int(page),
                    "pages": int(pages),
                    "filtered": bool(need_leg),
                }
            finally:
                conn.close()

    def query_snapshot(
        self,
        *,
        day: str,
        ts: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        week: str = "both",
        side: str = "both",
        moneyness: str = "wing",
        depth: int = 10,
        ltp_min: float | None = None,
        ltp_max: float | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """
        Filter one (or latest-in-window) LTP snapshot + optional OI join.

        moneyness: all | atm | itm | otm | wing (ATM±depth)
        depth: strike steps from ATM (5 or 10 → ± 50)
        """
        with self._lock:
            conn = self._connect()
            try:
                tick = None
                if ts:
                    tick = conn.execute(
                        "SELECT ts, spot, atm, current_expiry, next_expiry, day "
                        "FROM ltp_ticks WHERE ts=?",
                        (ts,),
                    ).fetchone()
                if tick is None:
                    sql = (
                        "SELECT ts, spot, atm, current_expiry, next_expiry, day "
                        "FROM ltp_ticks WHERE day=?"
                    )
                    args: list[Any] = [day]
                    if time_from:
                        sql += " AND substr(ts, 12, 8) >= ?"
                        args.append(_norm_hms(time_from))
                    if time_to:
                        sql += " AND substr(ts, 12, 8) <= ?"
                        args.append(_norm_hms(time_to))
                    sql += " ORDER BY ts DESC LIMIT 1"
                    tick = conn.execute(sql, args).fetchone()
                if tick is None:
                    return {
                        "day": day,
                        "ts": None,
                        "legs": [],
                        "error": "no ticks for filters",
                    }
                ts_use, spot, atm, cur_exp, nxt_exp, day_use = tick
                atm_i = int(atm or 0)
                depth_i = max(0, int(depth))
                step_pts = _STEP * depth_i

                legs_sql = (
                    "SELECT week, expiry, strike, side, symbol, ltp "
                    "FROM ltp_legs WHERE ts=?"
                )
                legs_args: list[Any] = [ts_use]
                if week in ("current", "next"):
                    legs_sql += " AND week=?"
                    legs_args.append(week)
                if side in ("CE", "PE"):
                    legs_sql += " AND side=?"
                    legs_args.append(side)
                if ltp_min is not None:
                    legs_sql += " AND ltp IS NOT NULL AND ltp >= ?"
                    legs_args.append(float(ltp_min))
                if ltp_max is not None:
                    legs_sql += " AND ltp IS NOT NULL AND ltp <= ?"
                    legs_args.append(float(ltp_max))
                legs_sql += " ORDER BY week, strike, side LIMIT ?"
                legs_args.append(int(limit))
                raw = conn.execute(legs_sql, legs_args).fetchall()

                # OI at same minute; else nearest earlier minute for this day
                ts_min = str(ts_use)[:16] + ":00"
                oi_map: dict[str, dict[str, Any]] = {}
                oi_rows = conn.execute(
                    "SELECT symbol, oi, volume, ltp FROM oi_snaps WHERE ts_minute=?",
                    (ts_min,),
                ).fetchall()
                if not oi_rows:
                    near = conn.execute(
                        "SELECT ts_minute FROM oi_snaps WHERE day=? AND ts_minute<=? "
                        "ORDER BY ts_minute DESC LIMIT 1",
                        (day_use, ts_min),
                    ).fetchone()
                    if near:
                        oi_rows = conn.execute(
                            "SELECT symbol, oi, volume, ltp FROM oi_snaps WHERE ts_minute=?",
                            (near[0],),
                        ).fetchall()
                for r in oi_rows:
                    oi_map[r[0]] = {"oi": r[1], "volume": r[2], "oi_ltp": r[3]}

                out_legs: list[dict[str, Any]] = []
                for week_v, expiry, strike, side_v, symbol, ltp in raw:
                    k = int(strike)
                    dist = (k - atm_i) // _STEP if atm_i else 0
                    mny = _moneyness_label(side_v, k, atm_i)
                    mode = (moneyness or "wing").lower()
                    if mode == "wing" and abs(k - atm_i) > step_pts:
                        continue
                    if mode == "atm" and k != atm_i:
                        continue
                    if mode == "itm" and (
                        mny != "ITM" or abs(dist) < 1 or abs(dist) > depth_i
                    ):
                        continue
                    if mode == "otm" and (
                        mny != "OTM" or abs(dist) < 1 or abs(dist) > depth_i
                    ):
                        continue
                    # mode == "all" → no strike-band filter
                    oi = oi_map.get(symbol) or {}
                    out_legs.append(
                        {
                            "week": week_v,
                            "expiry": expiry,
                            "strike": k,
                            "side": side_v,
                            "symbol": symbol,
                            "ltp": ltp,
                            "oi": oi.get("oi"),
                            "volume": oi.get("volume"),
                            "moneyness": mny,
                            "atm_dist": dist,
                        }
                    )

                return {
                    "day": day_use,
                    "ts": ts_use,
                    "time": str(ts_use)[11:19],
                    "spot": spot,
                    "atm": atm_i,
                    "current_expiry": cur_exp,
                    "next_expiry": nxt_exp,
                    "filters": {
                        "week": week,
                        "side": side,
                        "moneyness": moneyness,
                        "depth": depth_i,
                        "ltp_min": ltp_min,
                        "ltp_max": ltp_max,
                        "time_from": time_from,
                        "time_to": time_to,
                    },
                    "count": len(out_legs),
                    "legs": out_legs,
                }
            finally:
                conn.close()

    def atm_premium_series(
        self,
        day: str,
        *,
        week: str = "current",
        time_from: str | None = None,
        time_to: str | None = None,
    ) -> dict[str, Any]:
        """
        Dynamic ATM CE/PE LTP path for a day from live capture.

        At each tick uses that tick's ATM strike (recenters as spot moves).
        """
        week = week if week in ("current", "next") else "current"
        with self._lock:
            conn = self._connect()
            try:
                sql = (
                    "SELECT ts, spot, atm, current_expiry, next_expiry "
                    "FROM ltp_ticks WHERE day=?"
                )
                args: list[Any] = [day]
                if time_from:
                    sql += " AND substr(ts, 12, 8) >= ?"
                    args.append(_norm_hms(time_from))
                if time_to:
                    sql += " AND substr(ts, 12, 8) <= ?"
                    args.append(_norm_hms(time_to))
                sql += " ORDER BY ts ASC"
                ticks = conn.execute(sql, args).fetchall()
                points: list[dict[str, Any]] = []
                atm_seen: set[int] = set()
                for ts, spot, atm, cur_exp, nxt_exp in ticks:
                    if atm is None:
                        continue
                    atm_i = int(atm)
                    atm_seen.add(atm_i)
                    legs = conn.execute(
                        "SELECT side, ltp, symbol FROM ltp_legs "
                        "WHERE ts=? AND week=? AND strike=?",
                        (ts, week, atm_i),
                    ).fetchall()
                    ce_ltp = pe_ltp = None
                    ce_sym = pe_sym = None
                    for side, ltp, sym in legs:
                        if side == "CE":
                            ce_ltp, ce_sym = ltp, sym
                        elif side == "PE":
                            pe_ltp, pe_sym = ltp, sym
                    points.append(
                        {
                            "ts": ts,
                            "time": str(ts)[11:19],
                            "spot": spot,
                            "atm": atm_i,
                            "ce": ce_ltp,
                            "pe": pe_ltp,
                            "ce_symbol": ce_sym,
                            "pe_symbol": pe_sym,
                            "expiry": nxt_exp if week == "next" else cur_exp,
                        }
                    )
                return {
                    "day": day,
                    "week": week,
                    "source": "option_chain_live",
                    "count": len(points),
                    "atm_seen": sorted(atm_seen),
                    "points": points,
                }
            finally:
                conn.close()

    def strike_premium_series(
        self,
        *,
        week: str,
        expiry: str,
        legs: list[dict[str, Any]],
        day: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        max_legs: int = 6,
        resample: str = "1m",
    ) -> dict[str, Any]:
        """
        LTP history for selected CE/PE strikes, scoped to one expiry week only.

        Filters ltp_legs by week + expiry so the same strike number from another
        expiry never mixes in. Optional day/time window; default = all days for
        that expiry in the DB. resample=1m aggregates to last LTP per minute.
        """
        week = week if week in ("current", "next") else "current"
        expiry = str(expiry or "").strip()[:10]
        if not expiry:
            return {"ok": False, "error": "expiry required", "series": []}
        cleaned: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for raw in legs or []:
            side = str(raw.get("side") or "").upper()
            if side not in ("CE", "PE"):
                continue
            try:
                strike = int(raw.get("strike"))
            except Exception:
                continue
            key = (side, strike)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(key)
            if len(cleaned) >= max(1, int(max_legs)):
                break
        if not cleaned:
            return {
                "ok": False,
                "error": "no valid legs (need side+strike, max 6)",
                "series": [],
                "week": week,
                "expiry": expiry,
            }

        with self._lock:
            conn = self._connect()
            try:
                clauses = " OR ".join(
                    ["(side=? AND strike=?)"] * len(cleaned)
                )
                args: list[Any] = [week, expiry]
                for side, strike in cleaned:
                    args.extend([side, strike])
                sql = (
                    "SELECT ts, strike, side, symbol, ltp FROM ltp_legs "
                    f"WHERE week=? AND expiry=? AND ({clauses})"
                )
                if day:
                    sql += " AND substr(ts,1,10)=?"
                    args.append(str(day)[:10])
                if time_from:
                    sql += " AND substr(ts, 12, 8) >= ?"
                    args.append(_norm_hms(time_from))
                if time_to:
                    sql += " AND substr(ts, 12, 8) <= ?"
                    args.append(_norm_hms(time_to))
                sql += " ORDER BY ts ASC"
                rows = conn.execute(sql, args).fetchall()

                # bucket: (side, strike) -> minute -> last ltp + symbol
                buckets: dict[tuple[str, int], dict[str, tuple[float, str | None]]] = {
                    k: {} for k in cleaned
                }
                symbols: dict[tuple[str, int], str | None] = {k: None for k in cleaned}
                days_seen: set[str] = set()
                for ts, strike, side, symbol, ltp in rows:
                    if ltp is None:
                        continue
                    key = (str(side), int(strike))
                    if key not in buckets:
                        continue
                    ts_s = str(ts)
                    days_seen.add(ts_s[:10])
                    if resample == "1m":
                        bucket_t = ts_s[:16]  # YYYY-MM-DDTHH:MM
                    else:
                        bucket_t = ts_s[:19]
                    buckets[key][bucket_t] = (float(ltp), str(symbol) if symbol else None)
                    if symbol:
                        symbols[key] = str(symbol)

                series: list[dict[str, Any]] = []
                for side, strike in cleaned:
                    pts_map = buckets[(side, strike)]
                    points = [
                        {
                            "t": t,
                            "time": t[11:16] if "T" in t else t,
                            "v": round(v, 2),
                        }
                        for t, (v, _sym) in sorted(pts_map.items())
                    ]
                    series.append(
                        {
                            "id": f"{side}:{strike}",
                            "side": side,
                            "strike": strike,
                            "symbol": symbols[(side, strike)],
                            "count": len(points),
                            "points": points,
                            "channel": _fit_strike_channel(points),
                        }
                    )

                return {
                    "ok": True,
                    "week": week,
                    "expiry": expiry,
                    "day": day,
                    "resample": resample,
                    "source": "option_chain_live",
                    "days": sorted(days_seen),
                    "series": series,
                }
            finally:
                conn.close()


def _norm_hms(v: str) -> str:
    s = str(v or "").strip()
    if len(s) == 5:
        return s + ":00"
    if len(s) >= 8:
        return s[:8]
    return s


def _fit_strike_channel(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Parallel premium channel (hi/lo rails) for a strike LTP series."""
    if len(points) < 10:
        return None
    try:
        from foxtrade.breakout_trendline import fit_premium_channel, trendline_value

        px = [float(p["v"]) for p in points]
        break_i = len(px) - 1
        tol = max(0.5, abs(px[-1]) * 0.015)
        ch = fit_premium_channel(
            px,
            break_i=break_i,
            lookback=min(80, max(12, break_i)),
            from_start=True,
            tol=tol,
        )
        up = ch.get("up")  # support / lower
        down = ch.get("down")  # resistance / upper
        if not up and not down:
            return None
        hi_pts: list[dict[str, Any]] = []
        lo_pts: list[dict[str, Any]] = []
        for i, p in enumerate(points):
            hi = trendline_value(down, i)
            lo = trendline_value(up, i)
            if hi is not None:
                hi_pts.append({"t": p["t"], "v": round(float(hi), 2)})
            if lo is not None:
                lo_pts.append({"t": p["t"], "v": round(float(lo), 2)})
        if len(hi_pts) < 2 and len(lo_pts) < 2:
            return None
        return {
            "hi": hi_pts,
            "lo": lo_pts,
            "parallel": bool(ch.get("parallel")),
            "width": round(float(ch["width"]), 2) if ch.get("width") is not None else None,
            "slope": round(float(ch["slope"]), 5) if ch.get("slope") is not None else None,
        }
    except Exception:
        return None


def _moneyness_label(side: str, strike: int, atm: int) -> str:
    if not atm or strike == atm:
        return "ATM"
    if side == "CE":
        return "ITM" if strike < atm else "OTM"
    return "ITM" if strike > atm else "OTM"


def db_from_config(cfg: dict[str, Any] | None = None) -> OptionChainDB:
    cfg = cfg or {}
    raw = cfg.get("option_chain_db_path") or str(DEFAULT_DB)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return OptionChainDB(path)


class OptionChainRecorder:
    """Daemon thread: 10s LTP + 60s OI for current+next week wings."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._db: OptionChainDB | None = None
        self._cfg: dict[str, Any] = {}
        self._capturing = True  # day-default ON; user can pause via toggle
        self._arm_day: str | None = None
        self.status: dict[str, Any] = {
            "running": False,
            "enabled": False,
            "capturing": True,
            "arm_day": None,
            "last_ltp_ts": None,
            "last_oi_ts": None,
            "last_error": None,
            "current_expiry": None,
            "next_expiry": None,
            "symbols": 0,
            "samples_ltp": 0,
            "samples_oi": 0,
        }
        self._universe: list[dict[str, Any]] = []
        self._universe_key: str | None = None
        self._last_oi_minute: str | None = None
        self._latest_live: dict[str, Any] | None = None
        self._latest_oi_by_sym: dict[str, dict[str, Any]] = {}
        self._restore_arm()

    def _restore_arm(self) -> None:
        """Load pause state; new IST day always defaults capturing=ON."""
        today = _now().date().isoformat()
        arm = _load_arm()
        if arm.get("day") == today:
            self._capturing = bool(arm.get("capturing", True))
        else:
            self._capturing = True
            _save_arm(day=today, capturing=True)
        self._arm_day = today
        self.status["capturing"] = self._capturing
        self.status["arm_day"] = self._arm_day

    def _ensure_day_default(self) -> None:
        """Every new calendar day: capture arms ON automatically."""
        today = _now().date().isoformat()
        if self._arm_day == today:
            return
        self._arm_day = today
        self._capturing = True
        _save_arm(day=today, capturing=True)
        self.status["capturing"] = True
        self.status["arm_day"] = today

    def set_capturing(self, on: bool) -> dict[str, Any]:
        """UI toggle — pause/resume writes (thread keeps running)."""
        self._ensure_day_default()
        self._capturing = bool(on)
        day = self._arm_day or _now().date().isoformat()
        _save_arm(day=day, capturing=self._capturing)
        self.status["capturing"] = self._capturing
        self.status["arm_day"] = day
        if not self._capturing:
            self.status["idle"] = "capture_off"
        elif self.status.get("idle") == "capture_off":
            self.status["idle"] = None
        return self.snapshot()

    def start(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        self._cfg = dict(cfg)
        self._restore_arm()
        if not cfg.get("option_chain_db_enabled", True):
            self.status["enabled"] = False
            self.status["capturing"] = False
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        raw_path = cfg.get("option_chain_db_path") or str(DEFAULT_DB)
        path = Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        self._db = OptionChainDB(path)
        self.status["enabled"] = True
        self.status["db"] = str(self._db.path)
        self._thread = threading.Thread(
            target=self._loop, name="option-chain-db", daemon=True, args=(cfg,)
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self.status["running"] = False

    def _loop(self, cfg: dict[str, Any]) -> None:
        self.status["running"] = True
        # Sync (memory / UI) every 1s; persist to SQLite on save interval (default 10s)
        sync_iv = float(cfg.get("option_chain_sync_interval_sec", 1) or 1)
        save_iv = float(cfg.get("option_chain_ltp_interval_sec", 10) or 10)
        oi_iv = float(cfg.get("option_chain_oi_interval_sec", 60) or 60)
        wing = int(cfg.get("option_chain_wing_depth", 20) or 20)
        session_only = bool(cfg.get("option_chain_session_only", True))
        last_sync = 0.0
        last_save = 0.0
        last_oi = 0.0
        while not self._stop.is_set():
            try:
                self._ensure_day_default()
                now = _now()
                if not self._capturing:
                    self.status["idle"] = "capture_off"
                    self.status["capturing"] = False
                    self._stop.wait(2.0)
                    continue
                if session_only and not _in_session(now):
                    self.status["last_error"] = None
                    self.status["idle"] = "outside_session"
                    self._stop.wait(5.0)
                    continue
                self.status["idle"] = None
                self.status["capturing"] = True
                t0 = time.monotonic()
                if t0 - last_sync >= sync_iv:
                    persist = t0 - last_save >= save_iv
                    self._sample_ltp(wing=wing, persist=persist)
                    last_sync = t0
                    if persist:
                        last_save = t0
                if t0 - last_oi >= oi_iv:
                    self._sample_oi(wing=wing)
                    last_oi = t0
            except Exception as exc:  # noqa: BLE001
                self.status["last_error"] = str(exc)[:240]
            # Wake often enough for 1s sync
            self._stop.wait(min(0.25, max(0.1, sync_iv / 4)))
        self.status["running"] = False

    def _kite(self) -> Any:
        from kite_client import get_authenticated_kite

        return get_authenticated_kite()

    def _nifty_opts(self) -> list[dict[str, Any]]:
        from foxtrade.kite_cache import get_instruments

        inst = get_instruments("NFO", block=True)
        return [
            i
            for i in inst
            if i.get("name") == "NIFTY" and i.get("instrument_type") in ("CE", "PE")
        ]

    def _spot_atm(self, kite: Any) -> tuple[float | None, int | None]:
        try:
            q = kite.ltp(["NSE:NIFTY 50"]) or {}
            row = q.get("NSE:NIFTY 50") or {}
            spot = float(row["last_price"]) if row.get("last_price") is not None else None
        except Exception:
            spot = None
        atm = None
        if spot is not None:
            atm = int(round(spot / _STEP) * _STEP)
        return spot, atm

    def _build_universe(
        self, *, atm: int, cur: date, nxt: date, wing: int
    ) -> list[dict[str, Any]]:
        key = f"{atm}:{cur}:{nxt}:{wing}"
        if self._universe and self._universe_key == key:
            return self._universe
        from foxtrade.scanner import resolve_atm_wing_instruments

        out: list[dict[str, Any]] = []
        for week, exp in (("current", cur), ("next", nxt)):
            if week == "next" and exp == cur:
                continue
            legs = resolve_atm_wing_instruments(atm, expiry=exp, depth=wing, step=_STEP)
            for L in legs:
                out.append(
                    {
                        "week": week,
                        "expiry": str(exp),
                        "strike": int(float(L["strike"])),
                        "side": L["instrument_type"],
                        "symbol": L["tradingsymbol"],
                        "key": f"NFO:{L['tradingsymbol']}",
                    }
                )
        self._universe = out
        self._universe_key = key
        self.status["symbols"] = len(out)
        return out

    def _ltp_batch(self, kite: Any, keys: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(keys), _CHUNK):
            chunk = keys[i : i + _CHUNK]
            try:
                raw = kite.ltp(chunk) or {}
            except Exception:
                continue
            for k, v in raw.items():
                try:
                    if v and v.get("last_price") is not None:
                        out[k] = float(v["last_price"])
                except (TypeError, ValueError):
                    continue
        return out

    def _quote_batch(self, kite: Any, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), _CHUNK):
            chunk = keys[i : i + _CHUNK]
            try:
                raw = kite.quote(chunk) or {}
            except Exception:
                continue
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                out[k] = v
        return out

    def _sample_ltp(self, *, wing: int, persist: bool = True) -> None:
        if not self._db:
            return
        kite = self._kite()
        nifty = self._nifty_opts()
        now = _now()
        cur, nxt, rolled = recording_expiries(nifty, now)
        spot, atm = self._spot_atm(kite)
        if atm is None:
            self.status["last_error"] = "no_spot"
            return
        uni = self._build_universe(atm=atm, cur=cur, nxt=nxt, wing=wing)
        keys = [u["key"] for u in uni]
        ltps = self._ltp_batch(kite, keys)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        day = now.date().isoformat()
        legs = []
        for u in uni:
            legs.append(
                {
                    **u,
                    "ltp": ltps.get(u["key"]),
                }
            )
        # Always refresh in-memory live book (1s sync)
        self._latest_live = {
            "day": day,
            "ts": ts,
            "time": ts[11:19],
            "spot": spot,
            "atm": atm,
            "current_expiry": cur.isoformat(),
            "next_expiry": nxt.isoformat(),
            "rolled": rolled,
            "legs": legs,
            "persisted": bool(persist),
        }
        if persist:
            self._db.insert_ltp(
                ts=ts,
                day=day,
                spot=spot,
                atm=atm,
                current_expiry=cur.isoformat(),
                next_expiry=nxt.isoformat(),
                rolled=rolled,
                legs=legs,
            )
            self.status["samples_ltp"] = int(self.status.get("samples_ltp") or 0) + 1
            self.status["last_save_ts"] = ts
        self.status.update(
            {
                "last_ltp_ts": ts,
                "last_sync_ts": ts,
                "last_error": None,
                "current_expiry": cur.isoformat(),
                "next_expiry": nxt.isoformat(),
                "rolled": rolled,
                "spot": spot,
                "atm": atm,
                "sync_hz": 1.0,
            }
        )

    def live_snapshot(
        self,
        *,
        week: str = "both",
        side: str = "both",
        moneyness: str = "wing",
        depth: int = 10,
        ltp_min: float | None = None,
        ltp_max: float | None = None,
    ) -> dict[str, Any]:
        """Filtered view of the latest 1s-synced in-memory chain."""
        live = self._latest_live
        if not live:
            return {"error": "no live sync yet", "legs": [], "ts": None}
        atm_i = int(live.get("atm") or 0)
        depth_i = max(0, int(depth))
        step_pts = _STEP * depth_i
        mode = (moneyness or "wing").lower()
        out_legs: list[dict[str, Any]] = []
        oi_by_sym = self._latest_oi_by_sym
        for L in live.get("legs") or []:
            week_v = L.get("week")
            side_v = L.get("side")
            if week in ("current", "next") and week_v != week:
                continue
            if side in ("CE", "PE") and side_v != side:
                continue
            ltp = L.get("ltp")
            if ltp_min is not None and (ltp is None or float(ltp) < float(ltp_min)):
                continue
            if ltp_max is not None and (ltp is None or float(ltp) > float(ltp_max)):
                continue
            k = int(L["strike"])
            dist = (k - atm_i) // _STEP if atm_i else 0
            mny = _moneyness_label(str(side_v), k, atm_i)
            if mode == "wing" and abs(k - atm_i) > step_pts:
                continue
            if mode == "atm" and k != atm_i:
                continue
            if mode == "itm" and (mny != "ITM" or abs(dist) < 1 or abs(dist) > depth_i):
                continue
            if mode == "otm" and (mny != "OTM" or abs(dist) < 1 or abs(dist) > depth_i):
                continue
            out_legs.append(
                {
                    "week": week_v,
                    "expiry": L.get("expiry"),
                    "strike": k,
                    "side": side_v,
                    "symbol": L.get("symbol"),
                    "ltp": ltp,
                    "oi": oi_by_sym.get(L.get("symbol") or "", {}).get("oi"),
                    "volume": oi_by_sym.get(L.get("symbol") or "", {}).get("volume"),
                    "moneyness": mny,
                    "atm_dist": dist,
                }
            )
        return {
            "day": live.get("day"),
            "ts": live.get("ts"),
            "time": live.get("time"),
            "spot": live.get("spot"),
            "atm": atm_i,
            "current_expiry": live.get("current_expiry"),
            "next_expiry": live.get("next_expiry"),
            "source": "live_sync",
            "persisted": live.get("persisted"),
            "filters": {
                "week": week,
                "side": side,
                "moneyness": moneyness,
                "depth": depth_i,
                "ltp_min": ltp_min,
                "ltp_max": ltp_max,
            },
            "count": len(out_legs),
            "legs": out_legs,
        }

    def _sample_oi(self, *, wing: int) -> None:
        if not self._db:
            return
        kite = self._kite()
        nifty = self._nifty_opts()
        now = _now()
        # Align to minute bucket (one OI snap per minute)
        ts_minute = now.strftime("%Y-%m-%dT%H:%M:00")
        if ts_minute == self._last_oi_minute:
            return
        cur, nxt, _rolled = recording_expiries(nifty, now)
        spot, atm = self._spot_atm(kite)
        if atm is None:
            return
        uni = self._build_universe(atm=atm, cur=cur, nxt=nxt, wing=wing)
        keys = [u["key"] for u in uni]
        quotes = self._quote_batch(kite, keys)
        day = now.date().isoformat()
        legs = []
        for u in uni:
            q = quotes.get(u["key"]) or {}
            legs.append(
                {
                    **u,
                    "ltp": float(q["last_price"])
                    if q.get("last_price") is not None
                    else None,
                    "oi": int(q["oi"]) if q.get("oi") is not None else None,
                    "volume": int(q["volume"]) if q.get("volume") is not None else None,
                    "oi_day_high": int(q["oi_day_high"])
                    if q.get("oi_day_high") is not None
                    else None,
                    "oi_day_low": int(q["oi_day_low"])
                    if q.get("oi_day_low") is not None
                    else None,
                }
            )
        self._db.insert_oi(
            ts_minute=ts_minute,
            day=day,
            spot=spot,
            atm=atm,
            current_expiry=cur.isoformat(),
            next_expiry=nxt.isoformat(),
            legs=legs,
        )
        self._last_oi_minute = ts_minute
        self._latest_oi_by_sym = {
            str(L["symbol"]): {"oi": L.get("oi"), "volume": L.get("volume")}
            for L in legs
            if L.get("symbol")
        }
        self.status.update(
            {
                "last_oi_ts": ts_minute,
                "samples_oi": int(self.status.get("samples_oi") or 0) + 1,
                "last_error": None,
            }
        )

    def snapshot(self, *, include_db_stats: bool = True) -> dict[str, Any]:
        out = dict(self.status)
        out["has_live"] = self._latest_live is not None
        if self._latest_live:
            out["live_ts"] = self._latest_live.get("ts")
            out["live_persisted"] = self._latest_live.get("persisted")
        if include_db_stats and self._db:
            try:
                out["db_stats"] = self._db.stats(timeout=0.2)
            except Exception as exc:  # noqa: BLE001
                out["db_stats_error"] = str(exc)
        return out


# Process singleton
CHAIN_RECORDER = OptionChainRecorder()

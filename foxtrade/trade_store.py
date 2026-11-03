from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.charges import estimate_option_charges, trade_charges

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TRADES_PATH = DATA_DIR / "trades.json"

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(IST)


def _ensure() -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRADES_PATH.exists():
        TRADES_PATH.write_text("[]", encoding="utf-8")
    return json.loads(TRADES_PATH.read_text(encoding="utf-8"))


def _write(rows: list[dict[str, Any]]) -> None:
    TRADES_PATH.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def list_trades() -> list[dict[str, Any]]:
    with _lock:
        return _ensure()


def open_trade(payload: dict[str, Any], *, allow_add: bool = False) -> dict[str, Any]:
    """
    Persist a new OPEN row. Unless allow_add=True, refuses if this algo already
    has an OPEN row (hard anti-cheat / anti-amnesia duplicate).
    """
    with _lock:
        rows = _ensure()
        algo = str(payload.get("algo") or "")
        if algo and not allow_add:
            existing = [
                r
                for r in rows
                if r.get("status") == "OPEN"
                and str(r.get("algo") or "") == algo
                and not r.get("ledger_kind")  # ignore live-attempt audit rows
            ]
            if existing:
                return {
                    "id": None,
                    "status": "REJECTED",
                    "algo": algo,
                    "broker_status": "blocked_open_exists",
                    "note": (
                        f"refuse duplicate OPEN for {algo} — "
                        f"already {existing[0].get('symbol')} "
                        f"id={existing[0].get('id')} (square first)"
                    ),
                    "message": "OPEN already exists — no fresh trade until squared",
                    "existing_id": existing[0].get("id"),
                }
        # Track capital invested at entry (premium × qty)
        try:
            if payload.get("invested") is None and payload.get("entry_price") is not None:
                qty = int(payload.get("qty") or 0)
                payload = {
                    **payload,
                    "invested": round(float(payload["entry_price"]) * qty, 2),
                }
        except Exception:
            pass
        mode = str(payload.get("mode") or "paper")
        trade = {
            "id": str(uuid.uuid4())[:8],
            "opened_at": _now().isoformat(),
            "closed_at": None,
            "status": "OPEN",
            "pnl_points": 0.0,
            "pnl_rupees": 0.0,
            "ledger": "live" if mode == "live" else "paper",
            **payload,
        }
        rows.append(trade)
        _write(rows)
        return trade


def record_live_attempt(
    *,
    paper_trade: dict[str, Any] | None,
    reason: str,
    diagnosis: dict[str, Any] | None = None,
    broker_status: str = "live_buy_failed",
    exchange: str | None = None,
) -> dict[str, Any]:
    """
    Audit row for a real-money attempt that did not get a confirmed Zerodha fill.
    Stored as mode=live + CLOSED so Reports → Live only still shows it
    (excluded from win-rate / booked P&L via ledger_kind=live_attempt).
    """
    now = _now().isoformat()
    src = paper_trade or {}
    row: dict[str, Any] = {
        "id": str(uuid.uuid4())[:8],
        "opened_at": now,
        "closed_at": now,
        "status": "CLOSED",
        "mode": "live",
        "ledger": "live",
        "ledger_kind": "live_attempt",
        "broker_confirmed": False,
        "live_attempted": True,
        "broker_status": broker_status,
        "live_buy_diagnosis": diagnosis or {},
        "algo": src.get("algo"),
        "strategy": src.get("strategy"),
        "symbol": src.get("symbol"),
        "side": src.get("side"),
        "underlying": src.get("underlying"),
        "entry_price": src.get("entry_price"),
        "exit_price": src.get("entry_price"),
        "lots": src.get("lots"),
        "qty": src.get("qty"),
        "invested": src.get("invested") or 0.0,
        "pnl_points": 0.0,
        "pnl_rupees": 0.0,
        "net_pnl_rupees": 0.0,
        "charges": 0.0,
        "order_id": None,
        "exchange": exchange,
        "paper_fill_id": src.get("id"),
        "exit_reason": reason,
        "note": reason,
        "label": "LIVE_FAIL",
    }
    with _lock:
        rows = _ensure()
        rows.append(row)
        _write(rows)
    return row


def is_pnl_trade(t: dict[str, Any]) -> bool:
    """True for normal paper/live fills (excludes live-attempt audit rows)."""
    return str(t.get("ledger_kind") or "") != "live_attempt"


def update_trade(trade_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        rows = _ensure()
        for row in rows:
            if row["id"] == trade_id:
                row.update(updates)
                _write(rows)
                return row
        return None


def close_trade(
    trade_id: str,
    exit_price: float,
    reason: str,
    lot_size: int,
) -> dict[str, Any] | None:
    with _lock:
        rows = _ensure()
        for row in rows:
            if row["id"] == trade_id and row["status"] == "OPEN":
                entry = float(row["entry_price"])
                qty = int(row["qty"])
                side_mult = 1  # long options only
                points = (exit_price - entry) * side_mult
                gross = round(points * qty, 2)
                chg = estimate_option_charges(entry, float(exit_price), qty)
                charges = float(chg["charges"])
                row["exit_price"] = exit_price
                row["closed_at"] = _now().isoformat()
                row["status"] = "CLOSED"
                row["exit_reason"] = reason
                row["pnl_points"] = round(points, 2)
                row["pnl_rupees"] = gross  # gross (before charges)
                row["charges"] = charges
                row["charges_detail"] = chg
                row["net_pnl_rupees"] = round(gross - charges, 2)
                _write(rows)
                return row
        return None


def open_trades() -> list[dict[str, Any]]:
    return [
        t
        for t in list_trades()
        if t.get("status") == "OPEN" and str(t.get("ledger_kind") or "") != "live_attempt"
    ]


def _date_prefix(iso: str | None) -> str:
    return str(iso or "")[:10]


def today_closed_trades() -> list[dict[str, Any]]:
    """CLOSED trades whose closed_at (fallback opened_at) is today IST — for live Entries table."""
    today = _now().date().isoformat()
    out: list[dict[str, Any]] = []
    for t in list_trades():
        if t.get("status") != "CLOSED":
            continue
        day = _date_prefix(t.get("closed_at")) or _date_prefix(t.get("opened_at"))
        if day == today:
            out.append(t)
    return out


def today_entries_for_dashboard() -> list[dict[str, Any]]:
    """
    Live Entries / P&L day book: all OPEN + today's CLOSED only.
    Full history stays in trades.json for Reports.
    """
    return open_trades() + today_closed_trades()


def count_day_entries(algo: str, mode: str) -> int:
    """Legacy: counts every trade row (Algo2 dual = 2). Prefer count_day_setups."""
    today = _now().date().isoformat()
    n = 0
    for t in list_trades():
        if t.get("algo") != algo or t.get("mode") != mode:
            continue
        if str(t.get("opened_at", "")).startswith(today):
            n += 1
    return n


def count_day_setups(algo: str | None, mode: str) -> int:
    """Count unique setups today (dual legs share setup_id → 1). algo=None → all algos."""
    today = _now().date().isoformat()
    setups: set[str] = set()
    orphan = 0
    for t in list_trades():
        if t.get("mode") != mode:
            continue
        if algo is not None and t.get("algo") != algo:
            continue
        if not str(t.get("opened_at", "")).startswith(today):
            continue
        sid = t.get("setup_id")
        if sid:
            setups.add(str(sid))
        else:
            orphan += 1
    return len(setups) + orphan


def day_pnl_stats(algo: str, mode: str) -> dict[str, Any]:
    """Today closed PnL + loss count for expert risk gates."""
    today = _now().date().isoformat()
    pnl = 0.0
    losses = 0
    wins = 0
    primary_wins = 0
    primary_losses = 0
    last_loss_side: str | None = None
    last_loss_at = ""
    for t in list_trades():
        if t.get("algo") != algo or t.get("mode") != mode:
            continue
        if t.get("status") != "CLOSED":
            continue
        if not str(t.get("closed_at") or t.get("opened_at") or "").startswith(today):
            continue
        p = float(t.get("pnl_rupees") or t.get("pnl") or 0)
        pnl += p
        feats = t.get("entry_features") or {}
        is_scalp = bool(
            feats.get("micro_scalp")
            or feats.get("daily_soft")
            or feats.get("daily_pa")
        )
        if p < 0:
            losses += 1
            if not is_scalp:
                primary_losses += 1
            ca = str(t.get("closed_at") or "")
            if ca >= last_loss_at:
                last_loss_at = ca
                last_loss_side = str(t.get("side") or "") or None
        elif p > 0:
            wins += 1
            if not is_scalp:
                primary_wins += 1
    return {
        "pnl": round(pnl, 2),
        "realized_pnl": round(pnl, 2),
        "losses": losses,
        "wins": wins,
        "primary_wins": primary_wins,
        "primary_losses": primary_losses,
        "last_loss_side": last_loss_side,
    }


def period_pnl(algo: str, mode: str, *, since_date: str) -> float:
    """Sum closed PnL for algo/mode with closed_at/opened_at date ≥ since_date (YYYY-MM-DD)."""
    total = 0.0
    for t in list_trades():
        if t.get("algo") != algo or t.get("mode") != mode:
            continue
        if t.get("status") != "CLOSED":
            continue
        if not is_pnl_trade(t):
            continue
        stamp = str(t.get("closed_at") or t.get("opened_at") or "")[:10]
        if stamp < since_date:
            continue
        total += float(t.get("pnl_rupees") or t.get("pnl") or 0)
    return round(total, 2)


def seconds_since_last_close(algo: str, mode: str | None = None) -> float | None:
    """
    Seconds since the most recent CLOSED trade for this algo (same mode if given).
    None if nothing has closed yet → no cool-down.
    """
    latest: datetime | None = None
    for t in list_trades():
        if t.get("algo") != algo or t.get("status") != "CLOSED":
            continue
        if not is_pnl_trade(t):
            continue
        if mode is not None and t.get("mode") != mode:
            continue
        raw = t.get("closed_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if latest is None or ts > latest:
                latest = ts
        except Exception:
            continue
    if latest is None:
        return None
    return max(0.0, (_now() - latest.astimezone(IST)).total_seconds())


def _bucket() -> dict[str, float]:
    return {
        "paper": 0.0,
        "live": 0.0,
        "total": 0.0,  # net = gross - charges
        "gross": 0.0,
        "gross_paper": 0.0,
        "gross_live": 0.0,
        "charges": 0.0,
        "charges_paper": 0.0,
        "charges_live": 0.0,
    }


def pnl_summary() -> dict[str, Any]:
    now = _now()
    week_start = (now - timedelta(days=now.weekday())).date()
    month_start = now.replace(day=1).date()
    today = now.date()

    out = {
        "daily": _bucket(),
        "weekly": _bucket(),
        "monthly": _bucket(),
        "by_algo": {
            "algo1": _bucket(),
            "algo1_sx": _bucket(),
            "algo2": _bucket(),
        },
        "today_charges": 0.0,
        "today_gross": 0.0,
        "today_net": 0.0,
    }

    def _trade_day(t: dict[str, Any], *, open_leg: bool) -> Any:
        raw = t.get("opened_at") if open_leg else t.get("closed_at")
        raw = str(raw or "")
        try:
            return datetime.fromisoformat(raw).astimezone(IST).date()
        except Exception:
            try:
                return datetime.fromisoformat(raw[:10]).date()
            except Exception:
                return None

    def add(target: dict[str, float], *, key: str, net: float, gross: float, charges: float) -> None:
        target[key] += net
        target["total"] += net
        target["gross"] += gross
        target["gross_" + key] += gross
        target["charges"] += charges
        target["charges_" + key] += charges

    for t in list_trades():
        if not is_pnl_trade(t):
            continue
        mode = t.get("mode") or "paper"
        key = "live" if mode == "live" else "paper"
        algo = t.get("algo") or "algo1"
        if algo not in out["by_algo"]:
            out["by_algo"][algo] = _bucket()

        if t.get("status") == "CLOSED":
            d = _trade_day(t, open_leg=False)
            if d is None:
                continue
            gross = float(t.get("pnl_rupees") or 0)
            charges = trade_charges(t)
            if t.get("net_pnl_rupees") is not None:
                try:
                    net = float(t["net_pnl_rupees"])
                except (TypeError, ValueError):
                    net = round(gross - charges, 2)
            else:
                net = round(gross - charges, 2)
        elif t.get("status") == "OPEN":
            d = _trade_day(t, open_leg=True)
            if d is None:
                continue
            # Live MTM — keep Day/Week/Month ticking with open positions
            gross = float(t.get("live_pnl_rupees") or 0)
            if t.get("live_pnl_rupees") is None and t.get("live_price") and t.get("entry_price"):
                qty = int(t.get("qty") or 0)
                gross = round((float(t["live_price"]) - float(t["entry_price"])) * qty, 2)
            charges = trade_charges(t)  # est. round-trip using live as exit
            net = round(gross - charges, 2)
        else:
            continue

        if d == today:
            add(out["daily"], key=key, net=net, gross=gross, charges=charges)
        if d >= week_start:
            add(out["weekly"], key=key, net=net, gross=gross, charges=charges)
        if d >= month_start:
            add(out["monthly"], key=key, net=net, gross=gross, charges=charges)
        if algo in out["by_algo"]:
            add(out["by_algo"][algo], key=key, net=net, gross=gross, charges=charges)

    for section in ("daily", "weekly", "monthly"):
        for k in out[section]:
            out[section][k] = round(out[section][k], 2)
    for algo in out["by_algo"]:
        for k in out["by_algo"][algo]:
            out["by_algo"][algo][k] = round(out["by_algo"][algo][k], 2)

    out["today_charges"] = out["daily"]["charges"]
    out["today_gross"] = out["daily"]["gross"]
    out["today_net"] = out["daily"]["total"]
    out["open_count"] = len([t for t in list_trades() if t.get("status") == "OPEN"])

    # All-time closed net (paper + live) + open MTM
    all_time = _bucket()
    for t in list_trades():
        mode = t.get("mode") or "paper"
        key = "live" if mode == "live" else "paper"
        if t.get("status") == "CLOSED":
            gross = float(t.get("pnl_rupees") or 0)
            charges = trade_charges(t)
            net = (
                float(t["net_pnl_rupees"])
                if t.get("net_pnl_rupees") is not None
                else round(gross - charges, 2)
            )
            add(all_time, key=key, net=net, gross=gross, charges=charges)
        elif t.get("status") == "OPEN":
            gross = float(t.get("live_pnl_rupees") or 0)
            charges = trade_charges(t)
            net = round(gross - charges, 2)
            add(all_time, key=key, net=net, gross=gross, charges=charges)
    for k in all_time:
        all_time[k] = round(all_time[k], 2)
    out["all_time"] = all_time
    out["lifetime_net"] = all_time["total"]
    return out


def _parse_closed_date(t: dict[str, Any]):
    closed = str(t.get("closed_at") or "")
    try:
        return datetime.fromisoformat(closed).astimezone(IST).date()
    except Exception:
        try:
            return datetime.fromisoformat(closed[:10]).date()
        except Exception:
            return None


def report_details() -> dict[str, Any]:
    """Full report payload for the Reports page."""
    now = _now()
    week_start = (now - timedelta(days=now.weekday())).date()
    month_start = now.replace(day=1).date()
    today = now.date()
    summary = pnl_summary()
    trades = list_trades()
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_rows = [t for t in trades if t.get("status") == "OPEN"]

    def in_period(t: dict[str, Any], period: str) -> bool:
        d = _parse_closed_date(t)
        if d is None:
            return False
        if period == "daily":
            return d == today
        if period == "weekly":
            return d >= week_start
        if period == "monthly":
            return d >= month_start
        return True

    def _invested(t: dict[str, Any]) -> float:
        if t.get("invested") is not None:
            try:
                return float(t["invested"])
            except (TypeError, ValueError):
                pass
        try:
            return float(t.get("entry_price") or 0) * int(t.get("qty") or 0)
        except Exception:
            return 0.0

    def _algo_keys(rows: list[dict[str, Any]]) -> list[str]:
        keys = sorted({str(t.get("algo") or "unknown") for t in rows if t.get("algo")})
        # Prefer dual-book order
        prefer = ["algo1", "algo1_sx", "algo2"]
        ordered = [k for k in prefer if k in keys]
        ordered += [k for k in keys if k not in ordered]
        return ordered or prefer[:2]

    def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        # P&L / win-rate ignore live-attempt audit rows
        pnl_rows = [t for t in rows if is_pnl_trade(t)]
        attempts = [t for t in rows if not is_pnl_trade(t)]
        wins = [t for t in pnl_rows if float(t.get("pnl_rupees") or 0) > 0]
        losses = [t for t in pnl_rows if float(t.get("pnl_rupees") or 0) < 0]
        flat = [t for t in pnl_rows if float(t.get("pnl_rupees") or 0) == 0]
        total = sum(float(t.get("pnl_rupees") or 0) for t in pnl_rows)
        net_total = sum(
            float(t["net_pnl_rupees"])
            if t.get("net_pnl_rupees") is not None
            else float(t.get("pnl_rupees") or 0) - trade_charges(t)
            for t in pnl_rows
        )
        paper = sum(
            float(t.get("pnl_rupees") or 0)
            for t in pnl_rows
            if t.get("mode") != "live"
        )
        live = sum(
            float(t.get("pnl_rupees") or 0)
            for t in pnl_rows
            if t.get("mode") == "live"
        )
        points = sum(float(t.get("pnl_points") or 0) for t in pnl_rows)
        lots = sum(int(t.get("lots") or 0) for t in pnl_rows)
        invested = sum(_invested(t) for t in pnl_rows)
        charges = sum(trade_charges(t) for t in pnl_rows)
        confirmed = sum(
            1
            for t in pnl_rows
            if t.get("mode") == "live" and t.get("broker_confirmed")
        )
        return {
            "trades": len(pnl_rows),
            "wins": len(wins),
            "losses": len(losses),
            "flat": len(flat),
            "win_rate": round(len(wins) / len(pnl_rows) * 100, 1) if pnl_rows else 0.0,
            "pnl_rupees": round(total, 2),
            "net_pnl_rupees": round(net_total, 2),
            "pnl_points": round(points, 2),
            "paper": round(paper, 2),
            "live": round(live, 2),
            "lots": lots,
            "invested": round(invested, 2),
            "charges": round(charges, 2),
            "best": round(max((float(t.get("pnl_rupees") or 0) for t in pnl_rows), default=0), 2),
            "worst": round(min((float(t.get("pnl_rupees") or 0) for t in pnl_rows), default=0), 2),
            "live_confirmed": confirmed,
            "live_attempts_failed": len(attempts),
        }

    def by_algo_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for a in _algo_keys(rows):
            out[a] = stats([t for t in rows if str(t.get("algo") or "") == a])
        # Always expose Nifty / Sensex slots for dual UI
        for a in ("algo1", "algo1_sx"):
            out.setdefault(a, stats([]))
        return out

    # Full calendar history for Reports bars (never drop prior months)
    by_day: dict[str, float] = {}
    for t in closed:
        if not is_pnl_trade(t):
            continue
        d = _parse_closed_date(t)
        if d is None:
            continue
        key = d.isoformat()
        by_day[key] = round(by_day.get(key, 0) + float(t.get("pnl_rupees") or 0), 2)

    periods = {}
    for name in ("daily", "weekly", "monthly", "all"):
        rows = [t for t in closed if in_period(t, name)] if name != "all" else list(closed)
        periods[name] = {
            "stats": stats(rows),
            "by_algo": by_algo_block(rows),
            "trades": sorted(rows, key=lambda x: str(x.get("closed_at") or ""), reverse=True),
        }

    # unrealized on open (real + paper positions)
    open_active = [t for t in open_rows if is_pnl_trade(t)]
    unrealized = round(sum(float(t.get("live_pnl_rupees") or 0) for t in open_active), 2)

    # Separate paper vs live period slices
    def mode_periods(mode: str) -> dict[str, Any]:
        out_m = {}
        for name in ("daily", "weekly", "monthly", "all"):
            base_rows = periods[name]["trades"]
            rows = [t for t in base_rows if (t.get("mode") == "live") == (mode == "live")]
            out_m[name] = {
                "stats": stats(rows),
                "by_algo": by_algo_block(rows),
                "trades": rows,
            }
        return out_m

    paper_day: dict[str, float] = {}
    live_day: dict[str, float] = {}
    for t in closed:
        if not is_pnl_trade(t):
            continue
        d = _parse_closed_date(t)
        if d is None:
            continue
        key = d.isoformat()
        bucket = live_day if t.get("mode") == "live" else paper_day
        bucket[key] = round(bucket.get(key, 0) + float(t.get("pnl_rupees") or 0), 2)

    live_fills = [t for t in trades if t.get("mode") == "live" and is_pnl_trade(t)]
    live_attempts = [t for t in trades if t.get("mode") == "live" and not is_pnl_trade(t)]

    return {
        "generated_at": now.isoformat(),
        "summary": summary,
        "periods": periods,
        "periods_paper": mode_periods("paper"),
        "periods_live": mode_periods("live"),
        "daily_series": [{"date": k, "pnl": by_day[k]} for k in sorted(by_day.keys())],
        "daily_series_paper": [{"date": k, "pnl": paper_day[k]} for k in sorted(paper_day.keys())],
        "daily_series_live": [{"date": k, "pnl": live_day[k]} for k in sorted(live_day.keys())],
        "open_trades": open_active,
        "open_paper": [t for t in open_active if t.get("mode") != "live"],
        "open_live": [t for t in open_active if t.get("mode") == "live"],
        "unrealized_pnl": unrealized,
        "unrealized_paper": round(
            sum(float(t.get("live_pnl_rupees") or 0) for t in open_active if t.get("mode") != "live"), 2
        ),
        "unrealized_live": round(
            sum(float(t.get("live_pnl_rupees") or 0) for t in open_active if t.get("mode") == "live"), 2
        ),
        "week_start": week_start.isoformat(),
        "month_start": month_start.isoformat(),
        "today": today.isoformat(),
        # Retention: every paper + live fill stays in data/trades.json forever
        "stored_total": len(trades),
        "stored_closed": len(closed),
        "stored_open": len(open_active),
        "stored_paper": len([t for t in trades if t.get("mode") != "live"]),
        "stored_live": len(live_fills),
        "stored_live_attempts_failed": len(live_attempts),
        "live_attempts": sorted(
            live_attempts,
            key=lambda x: str(x.get("closed_at") or x.get("opened_at") or ""),
            reverse=True,
        )[:200],
    }


def report_filtered(
    *,
    mode: str = "combined",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """
    Custom duration report for paper / live / combined.
    date_from / date_to: YYYY-MM-DD inclusive (closed_at for closed, opened_at for open).
    """
    base = report_details()
    mode = (mode or "combined").lower()
    if mode not in ("combined", "paper", "live"):
        mode = "combined"

    def _in_range(t: dict[str, Any], *, open_leg: bool) -> bool:
        raw = t.get("opened_at") if open_leg else t.get("closed_at")
        d = str(raw or "")[:10]
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    def _mode_ok(t: dict[str, Any]) -> bool:
        m = t.get("mode") or "paper"
        if mode == "paper":
            return m != "live"
        if mode == "live":
            return m == "live"
        return True

    closed = [
        t
        for t in (base.get("periods") or {}).get("all", {}).get("trades") or []
        if _mode_ok(t) and _in_range(t, open_leg=False)
    ]
    # Recompute stats via report_details helpers by building a mini block
    # Use same invested/lots fields as stats() — call report_details periods path lightly
    opens = [
        t
        for t in (base.get("open_trades") or [])
        if _mode_ok(t) and _in_range(t, open_leg=True)
    ]

    # Inline stats (mirror report_details.stats)
    def _inv(t: dict[str, Any]) -> float:
        try:
            if t.get("invested") is not None:
                return float(t["invested"])
            return float(t.get("entry_price") or 0) * int(t.get("qty") or 0)
        except Exception:
            return 0.0

    pnl_closed = [t for t in closed if is_pnl_trade(t)]
    wins = [t for t in pnl_closed if float(t.get("pnl_rupees") or 0) > 0]
    losses = [t for t in pnl_closed if float(t.get("pnl_rupees") or 0) < 0]
    gross = sum(float(t.get("pnl_rupees") or 0) for t in pnl_closed)
    net = sum(
        float(t["net_pnl_rupees"])
        if t.get("net_pnl_rupees") is not None
        else float(t.get("pnl_rupees") or 0) - trade_charges(t)
        for t in pnl_closed
    )
    unreal = round(sum(float(t.get("live_pnl_rupees") or 0) for t in opens), 2)
    by_day: dict[str, float] = {}
    for t in pnl_closed:
        k = str(t.get("closed_at") or "")[:10]
        if not k:
            continue
        by_day[k] = round(by_day.get(k, 0) + float(t.get("pnl_rupees") or 0), 2)

    def _algo_slice(algo: str) -> dict[str, Any]:
        rows = [t for t in pnl_closed if t.get("algo") == algo]
        return {
            "trades": len(rows),
            "pnl_rupees": round(sum(float(t.get("pnl_rupees") or 0) for t in rows), 2),
            "wins": len([t for t in rows if float(t.get("pnl_rupees") or 0) > 0]),
            "losses": len([t for t in rows if float(t.get("pnl_rupees") or 0) < 0]),
        }

    return {
        "generated_at": _now().isoformat(),
        "mode": mode,
        "date_from": date_from,
        "date_to": date_to,
        "stats": {
            "trades": len(pnl_closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(pnl_closed) * 100, 1) if pnl_closed else 0.0,
            "pnl_rupees": round(gross, 2),
            "net_pnl_rupees": round(net, 2),
            "lots": sum(int(t.get("lots") or 0) for t in pnl_closed),
            "invested": round(sum(_inv(t) for t in pnl_closed), 2),
            "charges": round(sum(trade_charges(t) for t in pnl_closed), 2),
            "open_count": len(opens),
            "unrealized_pnl": unreal,
            "live_attempts_failed": len([t for t in closed if not is_pnl_trade(t)]),
        },
        "by_algo": {
            "algo1": _algo_slice("algo1"),
            "algo1_sx": _algo_slice("algo1_sx"),
            "algo2": _algo_slice("algo2"),
        },
        "daily_series": [{"date": k, "pnl": by_day[k]} for k in sorted(by_day.keys())],
        "trades": sorted(closed, key=lambda x: str(x.get("closed_at") or ""), reverse=True),
        "open_trades": opens,
        "summary": base.get("summary"),
        "stored_total": base.get("stored_total"),
    }

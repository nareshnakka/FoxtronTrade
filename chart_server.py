"""Local Nifty 50 candle chart (history + live LTP via Kite)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

from kite_client import get_authenticated_kite

NIFTY_TOKEN = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"
IST = ZoneInfo("Asia/Kolkata")
ALLOWED_INTERVALS = {
    "minute",
    "3minute",
    "5minute",
    "10minute",
    "15minute",
    "30minute",
    "60minute",
    "day",
}

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")


def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "nifty_chart.html")


@app.get("/api/candles")
def candles():
    interval = request.args.get("interval", "5minute")
    if interval not in ALLOWED_INTERVALS:
        return jsonify({"error": f"invalid interval: {interval}"}), 400
    try:
        days = max(1, min(int(request.args.get("days", "15")), 400))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400

    to_dt = datetime.now(IST)
    from_dt = to_dt - timedelta(days=days)
    # Intraday history depth is limited by Kite; keep request sane
    if interval != "day" and days > 60:
        from_dt = to_dt - timedelta(days=60)

    try:
        kite = get_authenticated_kite()
        rows = kite.historical_data(
            NIFTY_TOKEN,
            from_dt,
            to_dt,
            interval,
            continuous=False,
            oi=False,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    candles_out = []
    for row in rows:
        dt = row["date"]
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.astimezone(IST)
        # lightweight-charts expects unix seconds for intraday
        ts = int(dt.timestamp())
        candles_out.append(
            {
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )

    return jsonify(
        {
            "symbol": "NIFTY 50",
            "interval": interval,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "candles": candles_out,
        }
    )


@app.get("/api/quote")
def quote():
    try:
        kite = get_authenticated_kite()
        data = kite.quote([NIFTY_SYMBOL]).get(NIFTY_SYMBOL, {})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    ohlc = data.get("ohlc") or {}
    return jsonify(
        {
            "symbol": "NIFTY 50",
            "last_price": data.get("last_price"),
            "ohlc": ohlc,
            "timestamp": str(data.get("timestamp") or ""),
            "market_open": _is_market_open(),
        }
    )


if __name__ == "__main__":
    print("Nifty 50 chart → http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)

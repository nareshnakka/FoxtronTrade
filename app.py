"""FoxtronTrade dashboard + dual algo engine."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, request, send_from_directory

from foxtrade.config_store import load_config, save_config
from foxtrade.engine import ENGINE
from foxtrade import trade_store
from foxtrade.learner import learning_report, run_daily_learn
from kite_client import (
    check_session,
    exchange_request_token,
    get_authenticated_kite,
    login_url,
)

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
NIFTY_TOKEN = 256265
NIFTY_SYMBOL = "NSE:NIFTY 50"
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "dashboard.html")


@app.get("/api/auth")
def auth_status():
    try:
        status = ENGINE.refresh_auth(force_profile=True)
    except Exception:
        status = check_session(force_profile=True)
    return jsonify(status)


@app.get("/api/auth/login_url")
def auth_login_url():
    try:
        return jsonify({"ok": True, "login_url": login_url()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/callback")
def kite_callback():
    """Kite redirect target on :5050 (also forwarded from :5000 if Zerodha app uses 5000)."""
    request_token = request.args.get("request_token")
    status = request.args.get("status", "")
    if not request_token or status == "error":
        return (
            "<h3>Login failed</h3><p>Close this tab and click Login again on the dashboard.</p>",
            400,
        )
    try:
        info = exchange_request_token(request_token)
        try:
            ENGINE.refresh_auth(force_profile=True)
        except Exception:
            pass
        name = info.get("user_name") or info.get("user_id") or "OK"
        return (
            f"<h3>Login OK — {name}</h3>"
            "<p>access_token saved. You can close this tab and return to the dashboard.</p>"
            "<script>setTimeout(()=>window.close(),1500)</script>",
            200,
        )
    except Exception as exc:  # noqa: BLE001
        return f"<h3>Login error</h3><pre>{exc}</pre>", 500


def start_port5000_callback_forwarder(dashboard_port: int = 5050) -> None:
    """
    Zerodha app Redirect URL is often stuck on :5000.
    Listen on 5000 and 302-forward /callback (and other paths) to the real dashboard on 5050.
    """
    fwd = Flask("kite_port5000_forward")

    @fwd.route("/callback")
    def _fwd_callback():  # noqa: ANN202
        qs = request.query_string.decode("utf-8", errors="ignore")
        target = f"http://127.0.0.1:{dashboard_port}/callback"
        if qs:
            target = f"{target}?{qs}"
        return redirect(target, code=302)

    @fwd.route("/", defaults={"path": ""})
    @fwd.route("/<path:path>")
    def _fwd_any(path: str):  # noqa: ANN202
        qs = request.query_string.decode("utf-8", errors="ignore")
        target = f"http://127.0.0.1:{dashboard_port}/{path}"
        if qs:
            target = f"{target}?{qs}"
        return redirect(target, code=302)

    def _run() -> None:
        try:
            fwd.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
        except OSError as exc:
            print(f"Port 5000 forwarder not started ({exc}). Set Kite Redirect URL to :5050 or free port 5000.")

    threading.Thread(target=_run, name="kite-5000-forward", daemon=True).start()
    print("Kite callback forwarder → http://127.0.0.1:5000/callback  ⇒  :5050")


@app.get("/nifty")
def nifty_only():
    return send_from_directory(app.static_folder, "nifty_chart.html")


@app.get("/reports")
def reports_page():
    return send_from_directory(app.static_folder, "reports.html")


@app.get("/api/reports")
def reports_api():
    # refresh open MTM from engine state when available
    try:
        st = ENGINE.dashboard_state()
        for t in st.get("open_trades") or []:
            trade_store.update_trade(
                t["id"],
                {
                    "live_price": t.get("live_price"),
                    "live_pnl_rupees": t.get("live_pnl_rupees"),
                },
            )
    except Exception:
        pass
    mode = (request.args.get("mode") or "combined").lower()
    date_from = request.args.get("from") or request.args.get("date_from")
    date_to = request.args.get("to") or request.args.get("date_to")
    payload = trade_store.report_details()
    filtered = trade_store.report_filtered(
        mode=mode if mode in ("combined", "paper", "live") else "combined",
        date_from=date_from,
        date_to=date_to,
    )
    payload["filter"] = filtered
    if date_from or date_to:
        payload["custom_range"] = {
            "mode": filtered.get("mode"),
            "date_from": date_from,
            "date_to": date_to,
            "stats": filtered.get("stats"),
            "by_algo": filtered.get("by_algo"),
            "trades": filtered.get("trades"),
            "open_trades": filtered.get("open_trades"),
            "daily_series": filtered.get("daily_series"),
        }
    try:
        payload["learning"] = learning_report()
    except Exception as exc:  # noqa: BLE001
        payload["learning"] = {"error": str(exc)}
    return jsonify(payload)


@app.post("/api/learn")
def learn_now():
    return jsonify(run_daily_learn(force=True))


@app.get("/api/state")
def state():
    return jsonify(ENGINE.dashboard_state())


@app.post("/api/settings")
def settings():
    body = request.get_json(force=True, silent=True) or {}
    cfg = save_config(body)
    ENGINE.reload_config()
    return jsonify(cfg)


@app.post("/api/alert/mute")
def alert_mute():
    """Stop disconnect + stall beeps manually."""
    from foxtrade import link_alert, stall_alert

    return jsonify(
        {
            "ok": True,
            "link": link_alert.mute(),
            "stall": stall_alert.mute(),
        }
    )


@app.post("/api/alert/unmute")
def alert_unmute():
    from foxtrade import link_alert, stall_alert

    return jsonify(
        {
            "ok": True,
            "link": link_alert.unmute(),
            "stall": stall_alert.unmute(),
        }
    )


@app.get("/api/alert")
def alert_status():
    from foxtrade import link_alert, stall_alert

    return jsonify(
        {
            "link": link_alert.snapshot(),
            "stall": stall_alert.snapshot(),
        }
    )


@app.post("/api/square_off")
def square_off():
    """Manual market square-off for OPEN trades only (paper or live Kite SELL)."""
    body = request.get_json(force=True, silent=True) or {}
    all_open = bool(body.get("all_open"))
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "ids must be a list"}), 400
    if not all_open and not ids:
        return jsonify({"ok": False, "error": "select open trades or pass all_open"}), 400
    result = ENGINE.square_off_trades(ids, all_open=all_open)
    return jsonify(result)


@app.get("/api/nifty_candles")
def nifty_candles():
    interval = request.args.get("interval", "5minute")
    try:
        days = request.args.get("days")
        if days:
            # multi-day history for scrollable Nifty chart
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            from kite_client import get_authenticated_kite

            IST = ZoneInfo("Asia/Kolkata")
            to_dt = datetime.now(IST)
            from_dt = to_dt - timedelta(days=max(1, min(int(days), 30)))
            kite = get_authenticated_kite()
            rows = kite.historical_data(256265, from_dt, to_dt, interval)
            candles = []
            for row in rows:
                dt = row["date"]
                if getattr(dt, "tzinfo", None):
                    dt = dt.astimezone(IST)
                candles.append(
                    {
                        "time": int(dt.timestamp()),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
                )
            return jsonify({"candles": candles})
        candles = ENGINE.market.nifty_day_candles(interval=interval)
        return jsonify({"candles": candles})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), "candles": []}), 502


@app.get("/api/config")
def get_config():
    return jsonify(load_config())


@app.get("/api/quote")
def legacy_quote():
    """Compat for /nifty standalone chart page."""
    try:
        kite = get_authenticated_kite()
        data = kite.quote([NIFTY_SYMBOL]).get(NIFTY_SYMBOL, {})
        now = datetime.now(IST)
        market_open = (
            now.weekday() < 5
            and now.replace(hour=9, minute=15, second=0, microsecond=0)
            <= now
            <= now.replace(hour=15, minute=30, second=0, microsecond=0)
        )
        return jsonify(
            {
                "symbol": "NIFTY 50",
                "last_price": data.get("last_price"),
                "ohlc": data.get("ohlc") or {},
                "timestamp": str(data.get("timestamp") or ""),
                "market_open": market_open,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.get("/api/candles")
def legacy_candles():
    """Compat for /nifty standalone chart page."""
    interval = request.args.get("interval", "5minute")
    try:
        days = max(1, min(int(request.args.get("days", "15")), 400))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400
    to_dt = datetime.now(IST)
    from_dt = to_dt - timedelta(days=days if interval == "day" else min(days, 60))
    try:
        kite = get_authenticated_kite()
        rows = kite.historical_data(NIFTY_TOKEN, from_dt, to_dt, interval)
        candles = []
        for row in rows:
            dt = row["date"]
            if getattr(dt, "tzinfo", None):
                dt = dt.astimezone(IST)
            candles.append(
                {
                    "time": int(dt.timestamp()),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        return jsonify({"symbol": "NIFTY 50", "interval": interval, "candles": candles})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    print("Starting FoxtronTrade engine…")
    ENGINE.start()
    # Independent of engine loop — beeps if heartbeat freezes mid-session
    from foxtrade import stall_alert
    from foxtrade.config_store import load_config

    _cfg = load_config()
    stall_alert.start(
        max_age_sec=float(_cfg.get("heartbeat_max_age_sec", 90)),
        max_data_age_sec=float(_cfg.get("zerodha_data_max_age_sec", 45)),
        voice_sec=float(_cfg.get("voice_alert_seconds", 30)),
        restart_cooldown_sec=float(_cfg.get("engine_restart_cooldown_sec", 60)),
    )
    start_port5000_callback_forwarder(5050)
    print("Dashboard → http://127.0.0.1:5050")
    print("Live trading default OFF (paper).")
    print("Stall watchdog ON — voice 60s + auto-restart if engine dies / Zerodha data stalls.")
    print("If Kite Redirect URL is :5000, login callback is auto-forwarded to :5050.")
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)

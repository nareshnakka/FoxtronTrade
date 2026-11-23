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
    clear_access_token,
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
    # Never force Kite profile on dashboard poll — that wedged Flask under load.
    try:
        status = ENGINE.refresh_auth(force_profile=False)
    except Exception:
        status = check_session(force_profile=False)
    return jsonify(status)


@app.get("/api/auth/login_url")
def auth_login_url():
    try:
        # User clicked Login — allow auto-login / callback to take effect again.
        try:
            ENGINE.clear_manual_logout()
        except Exception:
            pass
        return jsonify({"ok": True, "login_url": login_url()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/auth/login", methods=["GET", "POST"])
def auth_login():
    """
    Dashboard Login click: clear logout flag and TOTP auto-login when .env
    creds exist. Only returns a browser login_url as fallback.
    """
    try:
        from foxtrade.auto_login import auto_login_configured, ensure_kite_session

        try:
            ENGINE.clear_manual_logout()
        except Exception:
            pass

        if auto_login_configured():
            status = ensure_kite_session(force_login=True)
            try:
                status2 = ENGINE.refresh_auth(force_profile=True)
                if isinstance(status2, dict):
                    status = {**status, **status2}
                ENGINE._auth_status = status
            except Exception:
                pass
            if status.get("ok"):
                return jsonify(
                    {
                        **status,
                        "ok": True,
                        "needs_login": False,
                        "mode": "auto",
                        "message": "Auto-login OK",
                    }
                )
            # Auto path failed — still offer manual URL so user isn't stuck
            out = {
                **status,
                "ok": False,
                "mode": "auto_failed",
                "needs_login": True,
                "message": status.get("reason")
                or (status.get("auto_login_detail") or {}).get("reason")
                or "auto_login_failed",
            }
            try:
                out["login_url"] = login_url()
            except Exception:
                pass
            return jsonify(out), 200

        # No TOTP creds — fall back to browser Kite login
        return jsonify(
            {
                "ok": True,
                "mode": "browser",
                "needs_login": True,
                "login_url": login_url(),
                "message": "Open Kite login in browser (add KITE_USER_ID/PASSWORD/TOTP to .env for one-click)",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc), "mode": "error"}), 500


@app.post("/api/auth/logout")
def auth_logout():
    """Revoke Kite token, delete local session, block auto-login until Login again."""
    try:
        from kite_client import invalidate_and_clear_session

        detail = invalidate_and_clear_session()
        status = {
            "ok": False,
            "needs_login": True,
            "reason": "logged_out",
            "user_name": None,
            "user_id": None,
            "saved_at": None,
            "login_url": None,
            "logged_out": True,
            "cleared": bool(detail.get("cleared")),
            "invalidated": bool(detail.get("invalidated")),
            "auto_login": "suppressed_manual_logout",
        }
        if detail.get("invalidate_error"):
            status["invalidate_error"] = detail["invalidate_error"]
        try:
            status["login_url"] = login_url()
        except Exception:
            pass
        try:
            ENGINE.apply_manual_logout(status)
        except Exception:
            pass
        return jsonify(status)
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
            ENGINE.clear_manual_logout()
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
    print("Kite callback forwarder -> http://127.0.0.1:5000/callback   ->   :5050")


@app.get("/nifty")
def nifty_only():
    return send_from_directory(app.static_folder, "nifty_chart.html")


@app.get("/reports")
def reports_page():
    return send_from_directory(app.static_folder, "reports.html")


@app.get("/option-chain-filter")
def option_chain_filter_page():
    """Live SQLite chain capture — filter by date/time, ATM/ITM/OTM, LTP."""
    return send_from_directory(app.static_folder, "option_chain_filter.html")


@app.get("/option-chain")
def option_chain_page_redirect():
    """Old static pager removed — send users to live chain filter."""
    return redirect("/option-chain-filter")


@app.get("/atm-premiums")
def atm_premiums_page():
    return send_from_directory(app.static_folder, "atm_premium_chart.html")


@app.get("/oi-convergence")
def oi_convergence_page():
    """Old static OI chart removed."""
    return redirect("/option-chain-filter")


@app.get("/merge-lock")
def merge_lock_page():
    """Old static merge-lock chart removed."""
    return redirect("/option-chain-filter")


@app.get("/lab")
def lab_page():
    return send_from_directory(app.static_folder, "lab.html")


@app.get("/api/lab/weeks")
def lab_weeks():
    from foxtrade.lab_fallen_channel import load_last_fallen
    from foxtrade.lab_train import list_weeks, load_last_run

    underlying = str(request.args.get("underlying") or "NIFTY")
    algo = str(request.args.get("algo") or "algo_cw")
    out = list_weeks(underlying=underlying, algo=algo)
    out["last_run"] = load_last_run()
    out["last_fallen"] = load_last_fallen()
    return jsonify(out)


@app.get("/api/lab/last")
def lab_last():
    from foxtrade.lab_train import load_last_run

    return jsonify(load_last_run() or {"ok": False, "error": "no run yet"})


@app.post("/api/lab/analyze")
def lab_analyze():
    from foxtrade.lab_train import analyze

    body = request.get_json(force=True, silent=True) or {}

    def _opt_float(key, default=None):
        if key not in body or body.get(key) is None or body.get(key) == "":
            return default
        return float(body.get(key))

    def _opt_int(key, default=None):
        if key not in body or body.get(key) is None or body.get(key) == "":
            return default
        return int(body.get(key))

    return jsonify(
        analyze(
            algo=str(body.get("algo") or "algo_cw"),
            underlying=str(body.get("underlying") or "NIFTY"),
            preset=str(body.get("preset") or "current"),
            days=body.get("days"),
            band_lo=_opt_float("band_lo"),
            band_hi=_opt_float("band_hi"),
            win_mult=_opt_float("win_mult"),
            max_setups=_opt_int("max_setups"),
            skip_2nd_if_net_ge=_opt_float("skip_2nd_if_net_ge"),
            mode=str(body.get("mode") or "hit"),
            retrace_pct=float(body.get("retrace_pct") or 0),
        )
    )


@app.post("/api/lab/train")
def lab_train_api():
    from foxtrade.lab_train import train

    body = request.get_json(force=True, silent=True) or {}
    exit_mults = body.get("exit_mults")
    if exit_mults is not None:
        try:
            exit_mults = [float(x) for x in exit_mults]
        except Exception:
            exit_mults = None

    def _opt_float(key, default=None):
        if key not in body or body.get(key) is None or body.get(key) == "":
            return default
        return float(body.get(key))

    return jsonify(
        train(
            algo=str(body.get("algo") or "algo_cw"),
            underlying=str(body.get("underlying") or "NIFTY"),
            preset=str(body.get("preset") or "current"),
            days=body.get("days"),
            band_lo=_opt_float("band_lo"),
            band_hi=_opt_float("band_hi"),
            band_sweep=bool(body.get("band_sweep")),
            exit_mults=exit_mults,
        )
    )


@app.post("/api/lab/promote")
def lab_promote():
    from foxtrade.lab_train import promote

    body = request.get_json(force=True, silent=True) or {}
    return jsonify(
        promote(
            algo=body.get("algo"),
            winner=body.get("winner"),
            use_last_best=bool(body.get("use_last_best", True)),
            reload_engine=bool(body.get("reload_engine", True)),
        )
    )


@app.get("/api/lab/fallen-channel")
def lab_fallen_channel_last():
    from foxtrade.lab_fallen_channel import load_last_fallen

    return jsonify(load_last_fallen() or {"ok": False, "error": "no fallen-channel run yet"})


@app.post("/api/lab/fallen-channel")
def lab_fallen_channel_scan():
    """Scan multi-day falling-channel breakouts (SL-12 / TP+30..50) for Lab."""
    from foxtrade.lab_fallen_channel import run_scan
    from foxtrade.lab_train import list_weeks

    body = request.get_json(force=True, silent=True) or {}
    underlying = str(body.get("underlying") or "NIFTY")
    algo = str(body.get("algo") or "algo_cw")
    if algo == "all":
        algo = "algo_cw"
    preset = str(body.get("preset") or "all")
    days = body.get("days")
    if not days:
        weeks = list_weeks(underlying=underlying, algo=algo)
        if preset == "custom":
            days = weeks.get("presets", {}).get("all") or []
        else:
            days = weeks.get("presets", {}).get(preset) or weeks.get("presets", {}).get("all") or []
    try:
        return jsonify(
            run_scan(
                days=list(days) if days else None,
                preset=preset,
                include_db=bool(body.get("include_db", True)),
                persist=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/playback")
def playback_page():
    return send_from_directory(app.static_folder, "playback_dashboard.html")


@app.get("/playback/week")
def playback_week_page():
    """Fresh week index from live auto-replay (current algo)."""
    from foxtrade.playback_auto import week_summary_live

    try:
        week = week_summary_live()
    except Exception as exc:
        return f"<p>Playback week error: {exc}</p>", 500
    days = week.get("available_days") or []
    mode = week.get("mode") or "algo"
    rows = []
    for d in days:
        net = next((x.get("net") for x in (week.get("days") or []) if x.get("day") == d), 0)
        rows.append(
            f"<li><strong>{d}</strong> · net ₹{net} &nbsp; "
            f"<a class='play' href='/playback?day={d}'>Play</a></li>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Week playback</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0b1017;color:#e8eef7;margin:24px}}
a{{color:#3d9cf0;text-decoration:none}}
a.play{{display:inline-block;padding:8px 14px;background:#163328;border:1px solid #2a6;
border-radius:8px;color:#e8eef7;font-weight:650}}
li{{margin:14px 0;font-size:1.05rem}}
</style></head><body>
<h1>Week playback — {mode}</h1>
<p>Auto-replay with current algo · fp {week.get('algo_fp','')}</p>
<p>NET ₹{week.get('net')} · gross ₹{week.get('gross')} · charges ₹{week.get('charges')}</p>
<ul>
{chr(10).join(rows) or '<li>no chain days</li>'}
</ul>
<p><a href="/">Live dashboard</a></p>
</body></html>"""


@app.get("/playback/report/<day>")
def playback_day_report(day: str):
    path = ROOT / "data" / "backtests" / "playback" / day / "report.html"
    if not path.exists():
        return jsonify({"error": f"no report for {day}"}), 404
    return path.read_text(encoding="utf-8")


def _playback_labels_dir() -> Path:
    d = ROOT / "data" / "backtests" / "labels"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_day(day: str) -> str | None:
    """YYYY-MM-DD only."""
    if not day or len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


@app.get("/api/playback/labels")
def playback_labels_list():
    import json

    out = []
    for p in sorted(_playback_labels_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return jsonify({"labels": out, "count": len(out)})


@app.get("/api/playback/labels/<day>")
def playback_labels_get(day: str):
    import json

    day = _safe_day(day)
    if not day:
        return jsonify({"error": "bad day"}), 400
    path = _playback_labels_dir() / f"{day}.json"
    if not path.exists():
        return jsonify(
            {
                "day": day,
                "merge_start": None,
                "merge_end": None,
                "break_t": None,
                "break_side": None,
                "notes": "",
            }
        )
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/playback/labels/<day>")
def playback_labels_post(day: str):
    import json

    day = _safe_day(day)
    if not day:
        return jsonify({"error": "bad day"}), 400
    body = request.get_json(silent=True) or {}
    payload = {
        "day": day,
        "merge_start": body.get("merge_start") or None,
        "merge_end": body.get("merge_end") or None,
        "break_t": body.get("break_t") or None,
        "break_side": body.get("break_side") or None,
        "notes": str(body.get("notes") or ""),
    }
    if payload["break_side"] not in (None, "CE", "PE"):
        return jsonify({"error": "break_side must be CE|PE"}), 400
    path = _playback_labels_dir() / f"{day}.json"
    if body.get("clear"):
        if path.exists():
            path.unlink()
        return jsonify({"ok": True, "cleared": True, "day": day})
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "label": payload})


@app.get("/api/playback/week")
def playback_week_api():
    """Always resim available chain days with the *current* algo config."""
    from foxtrade.playback_auto import week_summary_live

    try:
        return jsonify(week_summary_live())
    except Exception as exc:
        return jsonify({"available_days": [], "days": [], "net": None, "error": str(exc)})


@app.get("/api/playback/<day>")
def playback_day_api(day: str):
    """Auto-replay this day with the latest algo config."""
    day_ok = _safe_day(day)
    if not day_ok:
        return jsonify({"error": "bad day"}), 400
    from foxtrade.playback_auto import available_chain_days, simulate_day_live

    if day_ok not in available_chain_days():
        return jsonify(
            {
                "error": f"no option_chain_1m viewer for {day_ok}",
                "hint": "capture/export viewer.json under data/option_chain_1m/<day>/",
            }
        ), 404

    sim = simulate_day_live(day_ok)
    if sim.get("error") and not sim.get("frames"):
        return jsonify(
            {
                "error": sim.get("error"),
                "hint": "download option_chain_1m for this day",
            }
        ), 404
    return jsonify(sim)


@app.get("/api/merge_lock/<day>")
def merge_lock_api(day: str):
    """Deprecated static merge-lock viewer — use live chain filter."""
    return jsonify(
        {
            "error": "static merge_lock removed",
            "hint": "use /option-chain-filter (live capture DB)",
            "day": day,
        }
    ), 410


@app.get("/api/option_oi_1m/<day>")
def option_oi_1m_api(day: str):
    """Deprecated static OI CSV viewer — use live capture DB."""
    return jsonify(
        {
            "error": "static option_oi_1m removed",
            "hint": "use /option-chain-filter or /api/option_chain_db/live",
            "day": day,
        }
    ), 410


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
    algo = (request.args.get("algo") or "all").lower()
    payload = trade_store.report_details()
    filtered = trade_store.report_filtered(
        mode=mode if mode in ("combined", "paper", "live") else "combined",
        date_from=date_from,
        date_to=date_to,
        algo=None if algo in ("", "all", "combined") else algo,
    )
    payload["filter"] = filtered
    payload["algo_filter"] = algo if algo not in ("", "combined") else "all"
    if date_from or date_to or (algo and algo not in ("", "all", "combined")):
        payload["custom_range"] = {
            "mode": filtered.get("mode"),
            "algo": filtered.get("algo"),
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
    st = ENGINE.dashboard_state()
    # Always expose last Kite login time from access_token.json (idle path used to drop it)
    try:
        auth = dict(st.get("auth") or {})
        if not auth.get("saved_at"):
            from kite_client import load_token_meta

            meta = load_token_meta() or {}
            if meta.get("saved_at"):
                auth["saved_at"] = meta.get("saved_at")
                st["auth"] = auth
    except Exception:
        pass
    return jsonify(st)


@app.get("/api/option_chain_db")
def option_chain_db_status():
    """Live current+next week chain recorder status + SQLite stats."""
    try:
        from foxtrade.option_chain_db import CHAIN_RECORDER

        return jsonify(CHAIN_RECORDER.snapshot())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/option_chain_db/capture")
def option_chain_db_capture():
    """Toggle chain capture for today. New IST day always defaults ON."""
    try:
        from foxtrade.option_chain_db import CHAIN_RECORDER

        body = request.get_json(force=True, silent=True) or {}
        if "capturing" not in body:
            return jsonify({"error": "capturing bool required"}), 400
        return jsonify(CHAIN_RECORDER.set_capturing(bool(body.get("capturing"))))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/option_chain_db/days")
def option_chain_db_days():
    try:
        from foxtrade.config_store import load_config
        from foxtrade.option_chain_db import db_from_config

        return jsonify({"days": db_from_config(load_config()).list_days()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"days": [], "error": str(exc)}), 500


@app.get("/api/option_chain_db/times")
def option_chain_db_times():
    """
    Paginated timestamps for a day.
    Query: day, time_from?, time_to?, offset?, limit?,
           week?, side?, moneyness?, depth?, ltp_min?, ltp_max?
    """
    try:
        from foxtrade.config_store import load_config
        from foxtrade.option_chain_db import db_from_config

        day = str(request.args.get("day") or "").strip()
        if not day:
            return jsonify({"error": "day required"}), 400

        def _f(name: str) -> float | None:
            v = request.args.get(name)
            if v is None or str(v).strip() == "":
                return None
            return float(v)

        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            offset = 0
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            limit = 100
        try:
            depth = int(request.args.get("depth", "10"))
        except ValueError:
            depth = 10

        payload = db_from_config(load_config()).list_timestamps(
            day,
            time_from=request.args.get("time_from") or None,
            time_to=request.args.get("time_to") or None,
            offset=offset,
            limit=limit,
            week=str(request.args.get("week") or "both"),
            side=str(request.args.get("side") or "both"),
            moneyness=str(request.args.get("moneyness") or "all"),
            depth=depth,
            ltp_min=_f("ltp_min"),
            ltp_max=_f("ltp_max"),
        )
        payload["count"] = len(payload.get("times") or [])
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/option_chain_db/live")
def option_chain_db_live():
    """Latest 1s-synced in-memory chain (DB still saves every 10s)."""
    try:
        from foxtrade.option_chain_db import CHAIN_RECORDER

        def _f(name: str) -> float | None:
            v = request.args.get(name)
            if v is None or str(v).strip() == "":
                return None
            return float(v)

        try:
            depth = int(request.args.get("depth", "10"))
        except ValueError:
            depth = 10
        return jsonify(
            CHAIN_RECORDER.live_snapshot(
                week=str(request.args.get("week") or "both"),
                side=str(request.args.get("side") or "both"),
                moneyness=str(request.args.get("moneyness") or "wing"),
                depth=depth,
                ltp_min=_f("ltp_min"),
                ltp_max=_f("ltp_max"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/option_chain_db/atm_premiums")
def option_chain_db_atm_premiums():
    """Live ATM CE/PE premium path for a day (from capture DB)."""
    try:
        from foxtrade.config_store import load_config
        from foxtrade.option_chain_db import db_from_config

        day = str(request.args.get("day") or "").strip()
        if not day:
            return jsonify({"error": "day required"}), 400
        week = str(request.args.get("week") or "current")
        snap = db_from_config(load_config()).atm_premium_series(
            day,
            week=week,
            time_from=request.args.get("time_from") or None,
            time_to=request.args.get("time_to") or None,
        )
        return jsonify(snap)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/option_chain_db/strike_series")
def option_chain_db_strike_series():
    """
    History for up to 6 CE/PE strikes, locked to one expiry week.
    Query: week, expiry, legs=CE:25500,PE:25500 (max 6),
           scope=expiry|day (default expiry = all days for that expiry),
           day? (required if scope=day), time_from?, time_to?
    """
    try:
        from foxtrade.config_store import load_config
        from foxtrade.option_chain_db import db_from_config

        week = str(request.args.get("week") or "current")
        expiry = str(request.args.get("expiry") or "").strip()
        scope = str(request.args.get("scope") or "expiry").strip().lower()
        day = str(request.args.get("day") or "").strip() or None
        if scope == "day" and not day:
            return jsonify({"ok": False, "error": "day required when scope=day"}), 400
        if scope != "day":
            day = None  # full expiry-week history from available capture
        legs_raw = str(request.args.get("legs") or "").strip()
        legs: list[dict] = []
        for part in legs_raw.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            side, strike_s = part.split(":", 1)
            try:
                legs.append({"side": side.strip().upper(), "strike": int(strike_s)})
            except Exception:
                continue
        if not expiry and day:
            # Infer expiry from a snapshot tick for that day/week
            snap = db_from_config(load_config()).query_snapshot(
                day=day, week=week, moneyness="atm", depth=0
            )
            expiry = str(
                (snap.get("next_expiry") if week == "next" else snap.get("current_expiry"))
                or ""
            )[:10]
        if not expiry:
            return jsonify({"ok": False, "error": "expiry required"}), 400
        out = db_from_config(load_config()).strike_premium_series(
            week=week,
            expiry=expiry,
            legs=legs,
            day=day,
            time_from=request.args.get("time_from") or None,
            time_to=request.args.get("time_to") or None,
            max_legs=6,
            resample=str(request.args.get("resample") or "1m"),
        )
        return jsonify(out)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/option_chain_db/query")
def option_chain_db_query():
    """
    Filter captured chain snapshot.
    Query: day, ts?, time_from?, time_to?, week=both|current|next,
           side=both|CE|PE, moneyness=wing|atm|itm|otm|all, depth=5|10,
           ltp_min?, ltp_max?
    """
    try:
        from foxtrade.config_store import load_config
        from foxtrade.option_chain_db import db_from_config

        day = str(request.args.get("day") or "").strip()
        if not day:
            return jsonify({"error": "day required"}), 400

        def _f(name: str) -> float | None:
            v = request.args.get(name)
            if v is None or str(v).strip() == "":
                return None
            return float(v)

        depth_raw = request.args.get("depth", "10")
        try:
            depth = int(depth_raw)
        except ValueError:
            depth = 10
        snap = db_from_config(load_config()).query_snapshot(
            day=day,
            ts=request.args.get("ts") or None,
            time_from=request.args.get("time_from") or None,
            time_to=request.args.get("time_to") or None,
            week=str(request.args.get("week") or "both"),
            side=str(request.args.get("side") or "both"),
            moneyness=str(request.args.get("moneyness") or "wing"),
            depth=depth,
            ltp_min=_f("ltp_min"),
            ltp_max=_f("ltp_max"),
        )
        return jsonify(snap)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/engine/restart")
def engine_restart():
    """Soft-restart engine threads (never kills the process / live book)."""
    body = request.get_json(force=True, silent=True) or {}
    reason = str(body.get("reason") or "api")[:80]
    return jsonify(ENGINE.restart(reason=reason))


@app.post("/api/settings")
def settings():
    body = request.get_json(force=True, silent=True) or {}
    cfg = save_config(body)
    ENGINE.reload_config()
    try:
        from foxtrade.playback_auto import invalidate_cache

        invalidate_cache()
    except Exception:
        pass
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
    """Serve cached engine candles — never block Flask on Kite historical."""
    interval = request.args.get("interval", "5minute")
    try:
        cached = (
            ENGINE.market.nifty_candles_5m
            if interval.startswith("5")
            else ENGINE.market.nifty_candles_1m
        )
        if cached:
            candles = []
            for row in cached:
                dt = row.get("date")
                if dt is None:
                    continue
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
            if candles:
                return jsonify({"candles": candles, "source": "cache"})
        return jsonify({"candles": [], "source": "empty"})
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
        max_age_sec=float(_cfg.get("heartbeat_max_age_sec", 180)),
        max_data_age_sec=float(_cfg.get("zerodha_data_max_age_sec", 45)),
        voice_sec=float(_cfg.get("voice_alert_seconds", 10)),
        restart_cooldown_sec=float(
            _cfg.get(
                "stall_restart_cooldown_sec",
                _cfg.get("engine_restart_cooldown_sec", 90),
            )
        ),
    )
    # Soft-recover stays on; voice muted at boot (dashboard Unmute). Stops spam.
    stall_alert.mute()
    start_port5000_callback_forwarder(5050)
    print("Dashboard --> http://127.0.0.1:5050")
    print("Live trading default OFF (paper).")
    print("Stall watchdog ON — silent soft-restart (voice muted; Unmute on dashboard).")
    print("If Kite Redirect URL is :5000, login callback is auto-forwarded to :5050.")
    # threaded=True — a slow /api/state must never block mute/stall/heartbeat probes
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False, threaded=True)

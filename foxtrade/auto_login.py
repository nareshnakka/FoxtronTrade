"""
Unattended Zerodha Kite Connect login via user_id + password + TOTP.

Requires in .env (never commit):
  KITE_USER_ID=
  KITE_PASSWORD=
  KITE_TOTP_SECRET=   # base32 from authenticator setup

Daily token still expires ~06:00 IST — call ensure_kite_session() before market.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

from kite_client import (
    ROOT,
    check_session,
    exchange_request_token,
    load_settings,
    login_url,
)

_lock = threading.Lock()
_last_attempt = 0.0
_last_result: dict[str, Any] = {}


def _creds() -> dict[str, str] | None:
    load_dotenv(ROOT / ".env")
    uid = os.getenv("KITE_USER_ID", "").strip()
    pwd = os.getenv("KITE_PASSWORD", "").strip()
    totp = os.getenv("KITE_TOTP_SECRET", "").strip().replace(" ", "")
    if not uid or not pwd or not totp:
        return None
    return {"user_id": uid, "password": pwd, "totp_secret": totp}


def auto_login_configured() -> bool:
    return _creds() is not None


def _totp_now(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).now()


def perform_auto_login(*, force: bool = False) -> dict[str, Any]:
    """
    Full unattended login. Returns {ok, reason, ...}.
    Rate-limited to once / 90s unless force=True.
    """
    global _last_attempt, _last_result
    with _lock:
        now = time.time()
        if not force and _last_result.get("ok") and now - _last_attempt < 90:
            return dict(_last_result)
        if not force and now - _last_attempt < 45 and _last_result:
            # cooldown after failure/success
            out = dict(_last_result)
            out["skipped"] = "cooldown"
            return out
        _last_attempt = now

    creds = _creds()
    if not creds:
        out = {
            "ok": False,
            "reason": "missing KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env",
            "needs_manual": True,
        }
        _last_result = out
        return out

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "reason": f"config:{exc}", "needs_manual": True}
        _last_result = out
        return out

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FoxtronTrade/1.0",
            "X-Kite-Version": "3",
        }
    )

    try:
        r1 = sess.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": creds["user_id"], "password": creds["password"]},
            timeout=20,
        )
        j1 = r1.json()
        if not j1.get("status") == "success":
            out = {
                "ok": False,
                "reason": f"login_fail:{j1.get('message') or j1}",
                "needs_manual": True,
            }
            _last_result = out
            return out
        request_id = (j1.get("data") or {}).get("request_id")
        if not request_id:
            out = {"ok": False, "reason": "no request_id from login", "needs_manual": True}
            _last_result = out
            return out

        twofa = _totp_now(creds["totp_secret"])
        r2 = sess.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": creds["user_id"],
                "request_id": request_id,
                "twofa_value": twofa,
                "twofa_type": "totp",
                "skip_session": "true",
            },
            timeout=20,
        )
        j2 = r2.json()
        if not j2.get("status") == "success":
            out = {
                "ok": False,
                "reason": f"twofa_fail:{j2.get('message') or j2}",
                "needs_manual": True,
            }
            _last_result = out
            return out

        # Connect OAuth — follow redirects manually and STOP at request_token.
        # Do not allow_redirects into local /callback (dashboard would consume the token).
        connect_url = (
            f"https://kite.zerodha.com/connect/login?api_key={settings['api_key']}&v=3"
        )
        request_token = None
        final = connect_url
        cur = connect_url
        for _ in range(10):
            r3 = sess.get(cur, timeout=25, allow_redirects=False)
            loc = r3.headers.get("Location") or ""
            combined = loc or str(r3.url or "")
            final = combined or final
            if "request_token=" in combined:
                request_token = (parse_qs(urlparse(combined).query).get("request_token") or [None])[0]
                break
            m = re.search(r"request_token=([a-zA-Z0-9]+)", r3.text or "")
            if m:
                request_token = m.group(1)
                break
            if r3.status_code in (301, 302, 303, 307, 308) and loc:
                cur = loc if loc.startswith("http") else f"https://kite.zerodha.com{loc}"
                continue
            break
        if not request_token:
            out = {
                "ok": False,
                "reason": f"no request_token after oauth (url={final[:120]})",
                "needs_manual": True,
                "login_url": login_url(),
            }
            _last_result = out
            return out

        info = exchange_request_token(request_token)
        out = {
            "ok": True,
            "reason": "auto_login_ok",
            "user_name": info.get("user_name"),
            "user_id": info.get("user_id"),
            "saved_at": info.get("saved_at"),
            "needs_manual": False,
        }
        _last_result = out
        return out
    except Exception as exc:  # noqa: BLE001
        out = {
            "ok": False,
            "reason": f"auto_login_err:{exc}",
            "needs_manual": True,
            "login_url": None,
        }
        try:
            out["login_url"] = login_url()
        except Exception:
            pass
        _last_result = out
        return out


def ensure_kite_session(*, force_login: bool = False) -> dict[str, Any]:
    """
    Soft check; if stale/missing and auto-login configured, renew token.
    Never raises.
    """
    try:
        status = check_session(force_profile=True)
    except Exception as exc:  # noqa: BLE001
        status = {"ok": False, "needs_login": True, "reason": str(exc)}

    if status.get("ok") and not force_login:
        return {**status, "auto_login": "not_needed"}

    if not auto_login_configured():
        return {
            **status,
            "auto_login": "not_configured",
            "hint": "Add KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET to .env for unattended mornings",
        }

    result = perform_auto_login(force=force_login or bool(status.get("needs_login")))
    if result.get("ok"):
        try:
            status = check_session(force_profile=True)
        except Exception as exc:  # noqa: BLE001
            status = {"ok": True, "needs_login": False, "reason": f"saved but recheck:{exc}"}
        return {**status, "auto_login": "ok", "auto_login_detail": result}
    return {
        **status,
        "ok": False,
        "needs_login": True,
        "auto_login": "failed",
        "auto_login_detail": result,
    }


def morning_logout_relogin() -> dict[str, Any]:
    """
    Hard logout (delete access_token.json) then force TOTP login.
    Used once every weekday morning so the session is always fresh.
    Never raises.
    """
    from kite_client import clear_access_token, load_token_meta

    prev = (load_token_meta() or {}).get("saved_at")
    cleared = False
    try:
        cleared = bool(clear_access_token())
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "auto_login": "logout_failed",
            "reason": f"clear_token:{exc}",
            "prev_saved_at": prev,
        }

    status = ensure_kite_session(force_login=True)
    return {
        **status,
        "morning_refresh": True,
        "token_cleared": cleared,
        "prev_saved_at": prev,
        "new_saved_at": status.get("saved_at"),
    }

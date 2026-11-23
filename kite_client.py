"""Shared Kite Connect client helpers. No orders placed here."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from kiteconnect import KiteConnect

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "access_token.json"
# Durable UI Logout flag — blocks TOTP auto-login until Login is clicked again
MANUAL_LOGOUT_FLAG = ROOT / "data" / "manual_logout.flag"
IST = ZoneInfo("Asia/Kolkata")

# Kite order API checks client IP against allowlist. Prefer IPv4 so a listed
# IPv4 static/public address is used (Windows often egresses via IPv6 otherwise).
_IPV4_PATCHED = False


def prefer_ipv4() -> None:
    global _IPV4_PATCHED
    if _IPV4_PATCHED:
        return
    _orig = socket.getaddrinfo

    def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        try:
            v4 = _orig(host, port, socket.AF_INET, type, proto, flags)
            if v4:
                return v4
        except OSError:
            pass
        return _orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = _ipv4_first  # type: ignore[assignment]
    _IPV4_PATCHED = True


prefer_ipv4()


def _strip_env(val: str | None) -> str:
    """Trim whitespace and optional surrounding quotes from .env values."""
    s = (val or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def load_dotenv_files() -> Path:
    """
    Load project .env with override=True so a copied .env always wins over
    empty/stale process env vars. Strips UTF-8 BOM (Notepad copy artifact).
    """
    path = ROOT / ".env"
    # encoding_errors ignore keeps BOM from poisoning the first key name
    load_dotenv(path, override=True, encoding="utf-8-sig")
    return path


def load_settings() -> dict[str, str]:
    load_dotenv_files()
    api_key = _strip_env(os.getenv("KITE_API_KEY"))
    api_secret = _strip_env(os.getenv("KITE_API_SECRET"))
    redirect_url = _strip_env(
        os.getenv("KITE_REDIRECT_URL", "http://127.0.0.1:5050/callback")
    ) or "http://127.0.0.1:5050/callback"
    if not api_key or not api_secret:
        raise SystemExit(
            "Missing KITE_API_KEY / KITE_API_SECRET. "
            "Copy .env.example to .env and fill values from developers.kite.trade"
        )
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "redirect_url": redirect_url,
    }


def new_kite() -> KiteConnect:
    settings = load_settings()
    return KiteConnect(api_key=settings["api_key"])


def save_access_token(access_token: str) -> None:
    payload = {
        "access_token": access_token,
        "saved_at": datetime.now(IST).isoformat(),
    }
    TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_access_token() -> bool:
    """Remove local Kite session file. Returns True if a file was deleted."""
    if not TOKEN_FILE.exists():
        return False
    try:
        TOKEN_FILE.unlink()
        return True
    except OSError:
        return False


def is_manual_logout() -> bool:
    """True after dashboard Logout until Login / callback clears the flag."""
    try:
        return MANUAL_LOGOUT_FLAG.exists()
    except Exception:
        return False


def set_manual_logout(active: bool) -> None:
    """Persist / clear the manual-logout gate for auto-login paths."""
    try:
        MANUAL_LOGOUT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        if active:
            MANUAL_LOGOUT_FLAG.write_text(
                datetime.now(IST).isoformat(), encoding="utf-8"
            )
        elif MANUAL_LOGOUT_FLAG.exists():
            MANUAL_LOGOUT_FLAG.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass


def invalidate_and_clear_session() -> dict[str, Any]:
    """
    Real logout: arm manual-logout FIRST (so auto-login cannot race back in),
    then revoke at Zerodha (best-effort) and delete the local token file.
    """
    out: dict[str, Any] = {
        "invalidated": False,
        "cleared": False,
        "manual_logout": True,
    }
    # Gate FIRST — engine/unattended must not restore the session mid-logout
    set_manual_logout(True)
    token = None
    try:
        token = load_access_token()
    except Exception:
        token = None
    if token:
        try:
            kite = new_kite()
            kite.set_access_token(token)
            kite.invalidate_access_token()
            out["invalidated"] = True
        except Exception as exc:  # noqa: BLE001
            out["invalidate_error"] = str(exc)
    out["cleared"] = bool(clear_access_token())
    # Ensure flag still present even if clear_access_token raced something
    set_manual_logout(True)
    return out


def load_access_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return data.get("access_token")


def load_token_meta() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def login_url() -> str:
    return new_kite().login_url()


def _token_saved_date() -> datetime | None:
    meta = load_token_meta()
    raw = meta.get("saved_at")
    if not raw:
        # Legacy token file with no saved_at — treat as unknown / force check via API
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        return ts.astimezone(IST)
    except Exception:
        return None


def session_stale_by_date() -> bool:
    """
    Zerodha access tokens typically expire ~06:00 IST.
    Before 06:00 IST, yesterday's token is still treated as valid.
    """
    saved = _token_saved_date()
    if saved is None:
        return False  # unknown — rely on live profile() check
    now = datetime.now(IST)
    effective = now.date() if now.hour >= 6 else (now - timedelta(days=1)).date()
    return saved.date() < effective


def check_session(*, force_profile: bool = True) -> dict[str, Any]:
    """
    Validate Kite session for the dashboard.
    Returns {ok, reason, needs_login, user_name, user_id, saved_at, login_url}.
    """
    out: dict[str, Any] = {
        "ok": False,
        "needs_login": True,
        "reason": "no_token",
        "user_name": None,
        "user_id": None,
        "saved_at": load_token_meta().get("saved_at"),
        "login_url": None,
    }
    try:
        out["login_url"] = login_url()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"config:{exc}"
        return out

    token = load_access_token()
    if not token:
        out["reason"] = "missing_token"
        return out

    if session_stale_by_date():
        out["reason"] = "stale_day — login again (token from a previous day)"
        return out

    if not force_profile:
        out["ok"] = True
        out["needs_login"] = False
        out["reason"] = "token_present"
        return out

    try:
        kite = new_kite()
        kite.set_access_token(token)
        profile = kite.profile()
        out["ok"] = True
        out["needs_login"] = False
        out["reason"] = "ok"
        out["user_name"] = profile.get("user_name")
        out["user_id"] = profile.get("user_id")
        return out
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "token" in msg or "session" in msg or "invalid" in msg or "expired" in msg:
            out["reason"] = f"expired:{exc}"
        else:
            out["reason"] = f"auth_error:{exc}"
        return out


def exchange_request_token(request_token: str) -> dict[str, Any]:
    settings = load_settings()
    kite = new_kite()
    session = kite.generate_session(request_token, api_secret=settings["api_secret"])
    save_access_token(session["access_token"])
    kite.set_access_token(session["access_token"])
    profile = kite.profile()
    return {
        "ok": True,
        "user_name": profile.get("user_name"),
        "user_id": profile.get("user_id"),
        "saved_at": load_token_meta().get("saved_at"),
    }


def get_authenticated_kite() -> KiteConnect:
    kite = new_kite()
    token = load_access_token()
    if not token:
        raise RuntimeError("No access_token.json found. Login from the dashboard or run: python login.py")
    if session_stale_by_date():
        raise RuntimeError("Kite session stale (new day). Login again from the dashboard.")
    kite.set_access_token(token)
    return kite

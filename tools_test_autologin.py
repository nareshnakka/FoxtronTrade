"""One-shot logout + TOTP auto-login diagnostic (no secrets printed)."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from dotenv import load_dotenv

from foxtrade.auto_login import ensure_kite_session
from kite_client import TOKEN_FILE, check_session, exchange_request_token, load_settings

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)


def main() -> None:
    uid = os.getenv("KITE_USER_ID", "").strip()
    pwd = os.getenv("KITE_PASSWORD", "").strip()
    secret = os.getenv("KITE_TOTP_SECRET", "").strip().replace(" ", "")
    settings = load_settings()
    print("totp_secret_len", len(secret))
    print("api_key_set", bool(settings.get("api_key")))

    bak = TOKEN_FILE.with_suffix(".json.bak_logout_test")
    if TOKEN_FILE.exists():
        bak.write_bytes(TOKEN_FILE.read_bytes())
        TOKEN_FILE.unlink()
        print("wiped_token")

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FoxtronTrade/1.0",
            "X-Kite-Version": "3",
        }
    )

    r1 = sess.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": uid, "password": pwd},
        timeout=20,
    )
    j1 = r1.json()
    data = j1.get("data") or {}
    print("login", j1.get("status"), "twofa", data.get("twofa_type"), data.get("twofa_types"))
    request_id = data.get("request_id")
    if not request_id:
        print("FAIL no request_id", j1.get("message"))
        _restore(bak)
        return

    totp = pyotp.TOTP(secret)
    code = totp.now()
    r2 = sess.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": uid,
            "request_id": request_id,
            "twofa_value": code,
            "twofa_type": "totp",
            "skip_session": "true",
        },
        timeout=20,
    )
    j2 = r2.json()
    print("twofa", j2.get("status"), j2.get("message"))
    if j2.get("status") != "success":
        # retry once with previous window, fresh login
        time.sleep(1)
        r1 = sess.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": uid, "password": pwd},
            timeout=20,
        )
        request_id = (r1.json().get("data") or {}).get("request_id")
        code = totp.at(int(time.time()) - 15)
        r2 = sess.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": uid,
                "request_id": request_id,
                "twofa_value": code,
                "twofa_type": "totp",
                "skip_session": "true",
            },
            timeout=20,
        )
        j2 = r2.json()
        print("twofa_retry", j2.get("status"), j2.get("message"))
        if j2.get("status") != "success":
            print("FAIL twofa")
            _restore(bak)
            return

    connect_url = (
        f"https://kite.zerodha.com/connect/login?api_key={settings['api_key']}&v=3"
    )
    request_token = None
    cur = connect_url
    for step in range(10):
        resp = sess.get(cur, timeout=25, allow_redirects=False)
        loc = resp.headers.get("Location") or ""
        combined = loc or resp.url or ""
        host = urlparse(combined).netloc
        path = urlparse(combined).path[:80]
        print(f"oauth{step}", resp.status_code, host, path, "HAS_RT" if "request_token=" in combined else "")
        if "request_token=" in combined:
            request_token = parse_qs(urlparse(combined).query).get("request_token", [None])[0]
            break
        if resp.status_code in (301, 302, 303, 307, 308) and loc:
            cur = loc if loc.startswith("http") else f"https://kite.zerodha.com{loc}"
            continue
        m = re.search(r"request_token=([a-zA-Z0-9]+)", resp.text or "")
        if m:
            request_token = m.group(1)
            print("found_in_body")
            break
        break

    print("request_token", "yes" if request_token else "no")
    if not request_token:
        print("FAIL no request_token — trying ensure_kite_session path")
        res = ensure_kite_session(force_login=True)
        print("ensure", res.get("ok"), res.get("auto_login"), (res.get("auto_login_detail") or {}).get("reason"))
        if not res.get("ok"):
            _restore(bak)
        return

    try:
        info = exchange_request_token(request_token)
        print("exchange_ok", True)
        print("user_id", info.get("user_id"))
        st = check_session(force_profile=True)
        print("session_ok", st.get("ok"), st.get("reason"))
    except Exception as exc:  # noqa: BLE001
        print("exchange_err", type(exc).__name__, str(exc)[:250])
        # try built-in path
        res = ensure_kite_session(force_login=True)
        print("ensure", res.get("ok"), res.get("auto_login"), (res.get("auto_login_detail") or {}).get("reason"))
        if not res.get("ok"):
            _restore(bak)


def _restore(bak: Path) -> None:
    if bak.exists() and not TOKEN_FILE.exists():
        TOKEN_FILE.write_bytes(bak.read_bytes())
        print("restored_old_token")


if __name__ == "__main__":
    main()

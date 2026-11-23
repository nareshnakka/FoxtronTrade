"""
Diagnose + force Kite login for a second PC.

Usage:
  .venv\\Scripts\\python.exe tools_auth_fix.py
  .venv\\Scripts\\python.exe tools_auth_fix.py --login
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--login",
        action="store_true",
        help="Clear logout flag and force TOTP auto-login",
    )
    ap.add_argument(
        "--clear-logout",
        action="store_true",
        help="Only clear data/manual_logout.flag",
    )
    args = ap.parse_args()

    from foxtrade.auto_login import auto_login_configured, ensure_kite_session
    from foxtrade.engine import BUILD_ID
    from kite_client import (
        MANUAL_LOGOUT_FLAG,
        TOKEN_FILE,
        is_manual_logout,
        load_dotenv_files,
        set_manual_logout,
    )

    env_path = load_dotenv_files()
    print(f"build_id: {BUILD_ID}")
    print(f"root:     {ROOT}")
    print(f".env:     {env_path}  exists={env_path.is_file()}")
    print(f"auto_login_configured: {auto_login_configured()}")
    print(f"manual_logout: {is_manual_logout()}  ({MANUAL_LOGOUT_FLAG})")
    print(f"token: {TOKEN_FILE.is_file()}  {TOKEN_FILE}")

    if args.clear_logout or args.login:
        set_manual_logout(False)
        print("cleared manual_logout flag")

    if args.login:
        if not auto_login_configured():
            print("FAIL: .env missing KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET")
            return 2
        res = ensure_kite_session(force_login=True)
        keys = (
            "ok",
            "needs_login",
            "reason",
            "user_name",
            "user_id",
            "saved_at",
            "auto_login",
        )
        print(json.dumps({k: res.get(k) for k in keys}, indent=2, default=str))
        return 0 if res.get("ok") else 1

    return 0 if (env_path.is_file() and auto_login_configured()) else 2


if __name__ == "__main__":
    raise SystemExit(main())

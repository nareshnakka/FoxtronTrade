"""Check unattended / auto-login readiness without printing secrets."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _ok(name: str) -> bool:
    v = (os.getenv(name) or "").strip()
    return bool(v) and not v.lower().startswith("your_")


def main() -> None:
    print("=" * 60)
    print("FoxtronTrade — unattended readiness")
    print("=" * 60)
    need = [
        ("KITE_API_KEY", "Kite Connect API key"),
        ("KITE_API_SECRET", "Kite Connect API secret"),
        ("KITE_USER_ID", "Zerodha client ID (e.g. AB1234)"),
        ("KITE_PASSWORD", "Zerodha login password"),
        ("KITE_TOTP_SECRET", "Authenticator SECRET key (base32), NOT 6-digit OTP"),
    ]
    missing = []
    for key, label in need:
        status = "OK" if _ok(key) else "NEED — fill in .env"
        print(f"  {key:18} {status}")
        if not _ok(key):
            missing.append((key, label))

    from foxtrade.config_store import load_config
    from foxtrade.auto_login import auto_login_configured

    cfg = load_config()
    print()
    print(f"  unattended_mode       {cfg.get('unattended_mode')}")
    print(f"  unattended_auto_login {cfg.get('unattended_auto_login')}")
    print(f"  auto_login_configured {auto_login_configured()}")
    print()
    if missing:
        print("Still need from you:")
        for key, label in missing:
            print(f"  • {key} = {label}")
        print()
        print("After filling .env, re-run:")
        print("  .venv\\Scripts\\python.exe tools_check_unattended.py")
        print("Then start 24×7 with:")
        print("  start_unattended.bat")
    else:
        print("All credentials present. Testing session / auto-login…")
        from foxtrade.auto_login import ensure_kite_session

        res = ensure_kite_session(force_login=False)
        print(f"  session ok={res.get('ok')} needs_login={res.get('needs_login')}")
        print(f"  auto_login={res.get('auto_login')} reason={res.get('reason')}")
        if res.get("ok"):
            print()
            print("READY for 24×7. Run: start_unattended.bat")
            print("Keep laptop awake (Sleep = Never).")
        else:
            print()
            print("Credentials set but session failed — check USER_ID / PASSWORD / TOTP_SECRET.")
            detail = res.get("auto_login_detail") or {}
            if detail.get("reason"):
                print(f"  detail: {detail.get('reason')}")
    print("=" * 60)


if __name__ == "__main__":
    main()

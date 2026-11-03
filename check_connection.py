"""Read-only sanity check after login. Never places orders."""

from __future__ import annotations

from kite_client import get_authenticated_kite


def main() -> None:
    kite = get_authenticated_kite()
    profile = kite.profile()
    margins = kite.margins()
    print("=== Profile ===")
    print(f"User : {profile.get('user_name')} ({profile.get('user_id')})")
    print(f"Email: {profile.get('email')}")
    print(f"Exchanges: {profile.get('exchanges')}")
    print()
    equity = margins.get("equity", {})
    print("=== Equity margins (available) ===")
    print(f"Cash / available: {equity.get('available', {}).get('live_balance')}")
    print(f"Net             : {equity.get('net')}")
    print()
    print("Connection OK. Algorithm not configured yet — tell me which strategy you want.")


if __name__ == "__main__":
    main()

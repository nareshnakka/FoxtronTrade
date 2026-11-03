"""
Daily Zerodha login helper.

1. Starts a tiny local server on KITE_REDIRECT_URL
2. Opens the Kite login page in your browser
3. You complete userid / password / TOTP yourself
4. Captures request_token and saves access_token.json

Does not place any trades.
"""

from __future__ import annotations

import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from kite_client import load_settings, new_kite, save_access_token


def main() -> None:
    settings = load_settings()
    kite = new_kite()
    login_url = kite.login_url()

    parsed = urlparse(settings["redirect_url"])
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5000
    path = parsed.path or "/callback"

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            req = urlparse(self.path)
            if req.path != path:
                self.send_response(404)
                self.end_headers()
                return
            qs = parse_qs(req.query)
            request_token = (qs.get("request_token") or [None])[0]
            status = (qs.get("status") or [""])[0]
            if not request_token or status == "error":
                body = b"Login failed. Close this tab and retry python login.py"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)
                return

            session = kite.generate_session(request_token, api_secret=settings["api_secret"])
            save_access_token(session["access_token"])
            result["ok"] = "1"
            body = (
                b"Login OK. access_token saved. "
                b"You can close this tab and run: python check_connection.py"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = HTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Opening Kite login in your browser...")
    print(login_url)
    print(f"Listening for redirect on {settings['redirect_url']}")
    webbrowser.open(login_url)
    thread.join(timeout=300)
    server.server_close()

    if result.get("ok"):
        profile = kite.profile()
        print(f"Logged in as: {profile.get('user_name')} ({profile.get('user_id')})")
        print("Saved access_token.json — valid until ~6:00 AM IST next day.")
    else:
        raise SystemExit("Login timed out or failed. Run again: python login.py")


if __name__ == "__main__":
    main()

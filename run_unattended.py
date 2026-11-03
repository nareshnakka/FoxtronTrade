"""
24×7 supervisor for FoxtronTrade.

- Keeps app.py running
- Auto-restarts if process dies OR heartbeat goes stale (freeze)
- Pre-market (and on start): TOTP auto-login when configured
- Safe to leave overnight; laptop should stay awake

Usage:
  .venv\\Scripts\\python.exe run_unattended.py
  start_unattended.bat
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
APP = ROOT / "app.py"
HEARTBEAT = ROOT / "data" / "heartbeat.json"
LOG = ROOT / "data" / "unattended.log"

# Heartbeat older than this → treat engine as frozen
MAX_HB_AGE = 90.0
# Don't restart more often than this
MIN_RESTART_GAP = 45.0
# How often supervisor checks
CHECK_EVERY = 10.0


def _log(msg: str) -> None:
    line = f"{datetime.now(IST).isoformat()}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _hb_age() -> float | None:
    try:
        import json

        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(data["ts"]))
    except Exception:
        return None


def _ensure_login(tag: str) -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from foxtrade.auto_login import auto_login_configured, ensure_kite_session

        if not auto_login_configured():
            _log(f"[{tag}] auto-login not configured — set KITE_USER_ID/PASSWORD/TOTP_SECRET in .env")
            return
        res = ensure_kite_session(force_login=True)
        _log(f"[{tag}] session ok={res.get('ok')} auto={res.get('auto_login')} reason={res.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        _log(f"[{tag}] login error: {exc}")


def _kill_ports() -> None:
    # Best-effort free 5050/5000 on Windows
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "foreach ($p in 5050,5000) { "
                "Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | "
                "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }",
            ],
            cwd=str(ROOT),
            timeout=30,
            capture_output=True,
        )
    except Exception:
        pass


def _start_app() -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    py = str(PY if PY.exists() else sys.executable)
    _log(f"starting {py} app.py")
    return subprocess.Popen(
        [py, str(APP)],
        cwd=str(ROOT),
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def main() -> None:
    _log("FoxtronTrade unattended supervisor starting")
    _log("Dashboard will be http://127.0.0.1:5050 — leave this window open")
    last_restart = 0.0
    proc: subprocess.Popen | None = None

    # Morning / startup login before first launch
    _ensure_login("boot")

    while True:
        now = datetime.now(IST)
        mins = now.hour * 60 + now.minute

        # Pre-market login every weekday 08:20–09:10
        if now.weekday() < 5 and 8 * 60 + 20 <= mins <= 9 * 60 + 10:
            if time.time() - getattr(main, "_last_am_login", 0) > 180:
                _ensure_login("premarket")
                main._last_am_login = time.time()  # type: ignore[attr-defined]

        need_start = proc is None or proc.poll() is not None
        age = _hb_age()
        uptime = time.time() - last_restart if last_restart else 9999
        frozen = (
            proc is not None
            and proc.poll() is None
            and uptime > MIN_RESTART_GAP
            and (
                (age is not None and age > MAX_HB_AGE)
                or (age is None and uptime > 120)
            )
        )

        if need_start or frozen:
            why = "dead" if need_start else ("frozen" if age and age > MAX_HB_AGE else "no_heartbeat")
            # Voice alert ~60s (non-blocking) before / during restart
            try:
                sys.path.insert(0, str(ROOT))
                from foxtrade import voice_alert

                voice_alert.speak_for(
                    "engine_died",
                    "Engine died. Restarting Foxtron Trade.",
                    duration_sec=30.0,
                )
            except Exception as exc:  # noqa: BLE001
                _log(f"voice alert failed: {exc}")

            if proc and proc.poll() is None:
                _log(f"restart ({why}) hb_age={age} — killing pid={proc.pid}")
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except Exception:
                        proc.kill()
                except Exception:
                    pass
                _kill_ports()
                time.sleep(2)
            elif need_start and last_restart:
                _log(f"process exited code={proc.returncode if proc else '?'} — restarting")
                _kill_ports()
                time.sleep(2)

            _ensure_login(f"restart:{why}")
            proc = _start_app()
            last_restart = time.time()
            _log(f"app pid={proc.pid}")

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("supervisor stopped by user")

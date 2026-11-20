"""Detached relauncher: free ports 5050/5000, then start app.py."""
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
LOG = ROOT / "data" / "relaunch.log"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
APP = ROOT / "app.py"


def _log(msg: str) -> None:
    line = f"{datetime.now(IST).isoformat()}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _live_or_open() -> tuple[bool, int]:
    """Refuse hard kill while live trading or OPEN positions exist."""
    try:
        sys.path.insert(0, str(ROOT))
        from foxtrade.config_store import load_config
        from foxtrade import trade_store

        live = bool(load_config().get("live_trading"))
        n = len(trade_store.open_trades())
        return (live or n > 0), n
    except Exception:
        return False, 0


def _kill_stray_app_py() -> None:
    """Kill FoxtronTrade app.py only when NOT live/OPEN.

    On Windows, .venv\\Scripts\\python.exe is a launcher whose child is
    Local\\Programs\\Python\\...\\python.exe — both show app.py. Never treat
    the child as a 'stray twin'; killing it murders the live interpreter.
    """
    protect, opens = _live_or_open()
    if protect:
        _log(f"stray kill SKIPPED (live/open={opens})")
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'NiftyTrading.*app\\.py' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }",
            ],
            cwd=str(ROOT),
            timeout=30,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"stray app kill: {exc}")


def main() -> int:
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    lock = ROOT / "data" / "relaunch.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Dedupe: two stall watchdogs often fire relaunch within 1–2s
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < 45:
                _log(f"relaunch skipped (lock age={age:.0f}s) reason={reason}")
                return 0
        lock.write_text(f"{os.getpid()} {reason}\n", encoding="utf-8")
    except Exception:
        pass

    protect, opens = _live_or_open()
    if protect:
        _log(f"relaunch ABORTED (live/open={opens}) reason={reason} — will not kill app")
        return 0

    _log(f"relaunch requested reason={reason}")
    time.sleep(2.5)
    _kill_stray_app_py()
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "foreach ($p in 5050,5000) { "
                "Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | "
                "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force "
                "-ErrorAction SilentlyContinue } }",
            ],
            cwd=str(ROOT),
            timeout=30,
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"port kill: {exc}")
    time.sleep(1.5)
    # If unattended supervisor is alive, let IT restart app — avoid twin app.py
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'run_unattended\\.py' } | "
                "Measure-Object | Select-Object -ExpandProperty Count",
            ],
            cwd=str(ROOT),
            timeout=20,
            capture_output=True,
            text=True,
        )
        n = int((r.stdout or "0").strip() or "0")
        if n > 0:
            _log(f"unattended alive (n={n}) — skip spawn; supervisor will restart")
            return 0
    except Exception as exc:  # noqa: BLE001
        _log(f"unattended probe: {exc}")

    py = str(PY if PY.exists() else sys.executable)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    _log(f"starting {py} app.py")
    subprocess.Popen(
        [py, str(APP)],
        cwd=str(ROOT),
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    _log("relaunch spawned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

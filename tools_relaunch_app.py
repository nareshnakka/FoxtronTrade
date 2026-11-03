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


def main() -> int:
    reason = sys.argv[1] if len(sys.argv) > 1 else "manual"
    _log(f"relaunch requested reason={reason}")
    time.sleep(2.5)
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

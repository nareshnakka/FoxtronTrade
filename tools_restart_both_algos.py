"""Stop then start FoxtronTrade with Algo-CW + Algo-NW enabled."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def stop() -> None:
    ps = r"""
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue | ForEach-Object {
  if ($_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*' -or $_.CommandLine -like '*app.py*')) {
    Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue
  }
}
foreach ($p in 5050,5000) {
  Get-NetTCPConnection -LocalPort $p -EA SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue
  }
}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        check=False,
    )
    lock = ROOT / "data" / "unattended.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)
    time.sleep(2)
    print("stopped")


def ensure_config() -> None:
    from foxtrade.config_store import load_config, save_config

    save_config(
        {
            "algo_cw_enabled": True,
            "algo_nw_enabled": True,
            "algo_cw_skip_second_if_day_pnl_ge": 3000,
            "algo_cw_pair_hold_fallen_below": 0,
            "algo_cw_pair_exit_mult": 1.45,
            "algo_cw_max_setups_per_day": 2,
        }
    )
    cfg = load_config()
    print(
        "config",
        "cw=", cfg.get("algo_cw_enabled"),
        "nw=", cfg.get("algo_nw_enabled"),
        "skip=", cfg.get("algo_cw_skip_second_if_day_pnl_ge"),
    )


def start() -> None:
    log = ROOT / "data" / "start_both_algos.log"
    creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        subprocess.Popen(
            [str(PY), str(ROOT / "run_unattended.py")],
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT)},
        )
    print("started", PY)


def wait_up(timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:5050/api/state", timeout=3) as r:
                st = json.loads(r.read())
            cfg = st.get("config") or {}
            act = st.get("activity") or {}
            books = {}
            for k in ("algo_cw", "algo_nw", "algo2", "algo2nw"):
                b = st.get(k) or act.get(k)
                if b:
                    books[k] = {
                        "phase": b.get("phase") or b.get("state"),
                        "msg": (b.get("message") or b.get("last_message") or "")[:60],
                    }
            return {
                "ok": True,
                "cw": cfg.get("algo_cw_enabled"),
                "nw": cfg.get("algo_nw_enabled"),
                "skip": cfg.get("algo_cw_skip_second_if_day_pnl_ge"),
                "headline": (act.get("headline") or "")[:120],
                "books": books,
            }
        except Exception as exc:
            last = str(exc)
            time.sleep(1.5)
    return {"ok": False, "error": last}


def main() -> int:
    ensure_config()
    stop()
    start()
    st = wait_up()
    print(json.dumps(st, indent=2))
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

"""Force-kill anything on 5050/5000, then start unattended with both algos on."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def _run_ps(script: str) -> str:
    p = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        text=True,
        capture_output=True,
    )
    return (p.stdout or "") + (p.stderr or "")


def kill_ports() -> None:
    me = os.getpid()
    # Collect PIDs on trading ports
    out = _run_ps(
        r"""
$ids = @()
foreach ($port in 5050,5000) {
  Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
    ForEach-Object { $ids += $_.OwningProcess }
}
$ids | Select-Object -Unique | ForEach-Object { $_ }
"""
    )
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            if pid and pid != me:
                pids.append(pid)

    # Also match known app command lines
    out2 = _run_ps(
        r"""
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  ForEach-Object {
    $c = $_.CommandLine
    if ($c -and ($c -match 'run_unattended\.py' -or $c -match '[\\/ ]app\.py')) {
      $_.ProcessId
    }
  }
"""
    )
    for line in out2.splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            if pid and pid != me:
                pids.append(pid)

    pids = sorted(set(pids))
    print("kill targets", pids)
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    lock = ROOT / "data" / "unattended.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)
    time.sleep(2)
    print("stopped")


def start() -> None:
    log = ROOT / "data" / "start_both_algos.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    flags = 0x00000008 | 0x00000200
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- force start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        subprocess.Popen(
            [PY, str(ROOT / "run_unattended.py")],
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            env=env,
        )
    print("started")


def wait() -> dict:
    for _ in range(45):
        try:
            st = json.loads(
                urllib.request.urlopen("http://127.0.0.1:5050/api/state", timeout=3).read()
            )
            act = st.get("activity") or {}
            return {
                "headline": act.get("headline"),
                "activity_keys": [k for k in act if str(k).startswith("algo")],
                "top_keys": [k for k in st if str(k).startswith("algo")],
                "algo_names": st.get("algo_names"),
                "cw": (st.get("config") or {}).get("algo_cw_enabled"),
                "nw": (st.get("config") or {}).get("algo_nw_enabled"),
                "skip": (st.get("config") or {}).get("algo_cw_skip_second_if_day_pnl_ge"),
            }
        except Exception:
            time.sleep(1)
    return {"ok": False}


if __name__ == "__main__":
    from foxtrade.config_store import save_config

    save_config(
        {
            "algo_cw_enabled": True,
            "algo_nw_enabled": True,
            "algo_cw_skip_second_if_day_pnl_ge": 3000,
            "algo_cw_pair_hold_fallen_below": 0,
        }
    )
    kill_ports()
    start()
    print(json.dumps(wait(), indent=2))

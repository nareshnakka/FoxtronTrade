"""Hard boot: taskkill stubborn PIDs, wait until 5050 free, start unattended."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def port_pids() -> list[int]:
    ps = r"""
Get-NetTCPConnection -LocalPort 5050,5000 -ErrorAction SilentlyContinue |
  ForEach-Object { $_.OwningProcess } | Select-Object -Unique
"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
    ).stdout
    return [int(x) for x in out.split() if x.isdigit()]


def python_app_pids() -> list[int]:
    ps = r"""
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue |
  ForEach-Object {
    if ($_.CommandLine -match 'run_unattended\.py|[/\\]app\.py') { $_.ProcessId }
  }
"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
    ).stdout
    return [int(x) for x in out.split() if x.isdigit()]


def kill(pids: list[int]) -> None:
    me = os.getpid()
    for pid in sorted(set(pids)):
        if not pid or pid == me:
            continue
        print("taskkill", pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def port_free() -> bool:
    return len(port_pids()) == 0


def main() -> None:
    from foxtrade.config_store import save_config

    save_config(
        {
            "algo_cw_enabled": True,
            "algo_nw_enabled": True,
            "algo_cw_skip_second_if_day_pnl_ge": 3000,
            "algo_cw_pair_hold_fallen_below": 0,
        }
    )

    for round_i in range(5):
        pids = port_pids() + python_app_pids()
        # also known zombies
        pids += [25884]
        print(f"round {round_i} pids", sorted(set(pids)))
        kill(pids)
        time.sleep(1.5)
        if port_free():
            print("5050 free")
            break
        print("still busy", port_pids())
    else:
        print("WARNING: could not free 5050", port_pids())

    lock = ROOT / "data" / "unattended.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)

    log = ROOT / "data" / "start_both_algos.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- hard boot {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        subprocess.Popen(
            [PY, str(ROOT / "run_unattended.py")],
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=0x00000008 | 0x00000200,
            env=env,
        )
    print("started")

    for i in range(40):
        time.sleep(1)
        try:
            st = json.loads(
                urllib.request.urlopen("http://127.0.0.1:5050/api/state", timeout=3).read()
            )
            print(
                json.dumps(
                    {
                        "i": i,
                        "build_id": st.get("build_id"),
                        "algo_cw": "algo_cw" in st,
                        "algo2": "algo2" in st,
                        "headline": (st.get("activity") or {}).get("headline"),
                        "cw": (st.get("config") or {}).get("algo_cw_enabled"),
                        "nw": (st.get("config") or {}).get("algo_nw_enabled"),
                        "port_pids": port_pids(),
                    },
                    indent=2,
                )
            )
            return
        except Exception as exc:
            if i % 5 == 0:
                print("wait", i, exc, "port", port_pids())
    print("FAILED to come up")


if __name__ == "__main__":
    main()

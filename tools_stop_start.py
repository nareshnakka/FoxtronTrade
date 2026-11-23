"""Reliable stop + start for FoxtronTrade (replaces fragile bat-only kill)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "start_both_algos.log"
LOCK = ROOT / "data" / "unattended.lock"
HEARTBEAT = ROOT / "data" / "heartbeat.json"
PORTS = (5050, 5000)


def _ps(cmd: str) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "") + (r.stderr or "")


def _taskkill(pid: int) -> None:
    if not pid:
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
    )


def _pid_alive(pid: int) -> bool:
    """True if PID exists. Avoid os.kill(pid,0) on Windows — PermissionError ≠ dead."""
    if not pid:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = r.stdout or ""
        return str(pid) in out and "No tasks" not in out
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # exists but not signalable
        except OSError:
            return False


def _lock_pid() -> int | None:
    if not LOCK.exists():
        return None
    try:
        return int((LOCK.read_text(encoding="utf-8") or "0").strip().split()[0])
    except Exception:
        return None


def _clear_runtime_files() -> None:
    """Drop lock + corrupt heartbeat so the next start is clean."""
    try:
        LOCK.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        if HEARTBEAT.exists():
            raw = HEARTBEAT.read_bytes()
            # all-null / empty / non-json → remove so supervisor doesn't see "no hb"
            bad = (not raw) or (raw.count(0) > len(raw) // 2)
            if not bad:
                try:
                    json.loads(raw.decode("utf-8"))
                except Exception:
                    bad = True
            if bad:
                HEARTBEAT.unlink(missing_ok=True)
    except Exception:
        pass


def collect_pids() -> list[int]:
    """Every FoxtronTrade-related python + anyone holding 5050/5000."""
    me = os.getpid()
    pids: set[int] = set()

    lock_pid = _lock_pid()
    if lock_pid and lock_pid != me:
        pids.add(lock_pid)

    out = _ps(
        r"""
$root = '"""
        + str(ROOT).replace("'", "''")
        + r"""'
$ids = @()
foreach ($port in 5050,5000) {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { $ids += $_.OwningProcess }
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  ForEach-Object {
    $c = $_.CommandLine
    if (-not $c) { return }
    $hit = $false
    if ($c -match 'run_unattended\.py') { $hit = $true }
    if ($c -match '[\\/ ]app\.py(\s|$)') { $hit = $true }
    if ($c -like ("*" + $root + "*") -and ($c -match 'app\.py|run_unattended\.py')) { $hit = $true }
    if ($hit) { $ids += $_.ProcessId }
  }
$ids | Select-Object -Unique
"""
    )
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            pid = int(s)
            if pid and pid != me:
                pids.add(pid)
    return sorted(pids)


def is_running() -> dict:
    """Structured status for Start.bat / UI."""
    pids = collect_pids()
    listeners: list[dict] = []
    for port in PORTS:
        raw = _ps(
            f"Get-NetTCPConnection -LocalPort {port} -State Listen -EA SilentlyContinue | "
            "ForEach-Object { $_.OwningProcess }"
        )
        for line in raw.splitlines():
            s = line.strip()
            if s.isdigit():
                listeners.append({"port": port, "pid": int(s)})
    lock_pid = _lock_pid()
    lock_alive = bool(lock_pid and _pid_alive(lock_pid))
    # Stale lock alone does NOT count as running
    running = bool(pids) or bool(listeners)
    return {
        "running": running,
        "pids": pids,
        "listeners": listeners,
        "lock_pid": lock_pid,
        "lock_alive": lock_alive,
        "stale_lock": bool(lock_pid and not lock_alive),
    }


def stop() -> None:
    # Pass 1: known targets
    pids = collect_pids()
    print("stop targets:", pids or "(none)")
    for pid in pids:
        _taskkill(pid)

    # Pass 2: brute-force ports again (child may rebind briefly)
    for _ in range(3):
        time.sleep(0.4)
        left = collect_pids()
        if not left:
            break
        for pid in left:
            _taskkill(pid)

    # Pass 3: raw port owners even if CommandLine hidden
    _ps(
        r"""
foreach ($p in 5050,5000) {
  Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
    ForEach-Object {
      try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
    }
}
"""
    )

    _clear_runtime_files()

    st = is_running()
    if st["running"]:
        print("WARNING: still running after stop:", json.dumps(st))
    else:
        print("stopped")


def start() -> None:
    if not PY.exists():
        raise SystemExit(f"missing venv python: {PY}")
    # Never leave a stale lock that blocks the new supervisor
    st = is_running()
    if st.get("stale_lock"):
        print(f"clearing stale lock pid={st.get('lock_pid')}")
        _clear_runtime_files()
    if st.get("running"):
        print("already running — stop first:", json.dumps(st))
        return

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n--- tools_stop_start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        subprocess.Popen(
            [str(PY), str(ROOT / "run_unattended.py")],
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
        )
    print("started supervisor")


def wait_ready(timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:5050/", timeout=3).read(200)
            st = json.loads(
                urllib.request.urlopen("http://127.0.0.1:5050/api/state", timeout=5).read()
            )
            cfg = st.get("config") or {}
            act = st.get("activity") or {}
            return {
                "ok": True,
                "build_id": st.get("build_id"),
                "headline": act.get("headline"),
                "cw": cfg.get("algo_cw_enabled"),
                "nw": cfg.get("algo_nw_enabled"),
                "has_algo_cw": "algo_cw" in st,
                "has_algo2": "algo2" in st,
                "auth_saved_at": (st.get("auth") or {}).get("saved_at"),
                "kite_idle": (st.get("kite_idle") or {}).get("active"),
            }
        except Exception as exc:
            last = str(exc)
            time.sleep(1)
    return {"ok": False, "error": last}


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "restart").lower()
    if cmd == "status":
        st = is_running()
        print(json.dumps(st, indent=2))
        return 0 if not st["running"] else 1
    if cmd == "stop":
        stop()
        return 0
    if cmd == "start":
        start()
        print(json.dumps(wait_ready(), indent=2))
        return 0
    # restart
    stop()
    time.sleep(1)
    start()
    info = wait_ready()
    print(json.dumps(info, indent=2))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

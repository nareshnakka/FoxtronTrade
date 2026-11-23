"""
24×7 supervisor for FoxtronTrade.

- Keeps app.py running
- Auto-restarts if process dies OR heartbeat goes stale (freeze)
- Pre-market (and on start): TOTP auto-login when configured
- Safe to leave overnight; laptop should stay awake

Usage:
  .venv\\Scripts\\python.exe run_unattended.py
  Start.bat
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
LOCK = ROOT / "data" / "unattended.lock"

# Heartbeat older than this → treat engine as frozen.
# Keep high: engine may do slow Kite work; independent hb pulse keeps ts fresh.
# LIVE/OPEN path refuses kill entirely (see main loop).
MAX_HB_AGE = 180.0
# Don't restart more often than this
MIN_RESTART_GAP = 60.0
# How often supervisor checks
CHECK_EVERY = 10.0


def _other_supervisor_pids() -> list[int]:
    """Find other run_unattended.py processes (venv or system Python)."""
    me = os.getpid()
    parent = os.getppid()
    out: list[int] = []
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -and ($_.CommandLine -like '*run_unattended.py*') } | "
                "ForEach-Object { $_.ProcessId }",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pid = int(line)
                # venv python.exe is a launcher; its child is the real interpreter.
                # Do not treat that parent as a second supervisor.
                if pid in (me, parent):
                    continue
                # Verify still alive — PowerShell can race with exiting shells
                try:
                    os.kill(pid, 0)
                except OSError:
                    continue
                out.append(pid)
    except Exception:
        pass
    return out


def _acquire_singleton() -> None:
    """
    Only one supervisor may run. Multiple copies kill ports 5000/5050
    every few seconds and the engine never stays up long enough to trade.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    rivals = _other_supervisor_pids()
    if rivals:
        print(
            "ERROR: another unattended supervisor already running "
            f"(pid={', '.join(map(str, rivals))}).\n"
            "Close that window / run Stop.bat first. "
            "Only ONE unattended copy is allowed.",
            flush=True,
        )
        raise SystemExit(2)
    if LOCK.exists():
        try:
            old = int((LOCK.read_text(encoding="utf-8") or "0").strip().split()[0])
        except Exception:
            old = 0
        if old and old != my_pid:
            # tasklist: os.kill(pid,0) on Windows raises PermissionError ≠ dead
            alive = False
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {old}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                out = r.stdout or ""
                alive = str(old) in out and "No tasks" not in out
            except Exception:
                try:
                    os.kill(old, 0)
                    alive = True
                except PermissionError:
                    alive = True
                except OSError:
                    alive = False
            if alive:
                # Confirm it really is our supervisor — recycled PIDs are common after reboot
                still_rival = old in _other_supervisor_pids() or old in rivals
                if still_rival:
                    print(
                        f"ERROR: lock held by pid={old}. Stop that supervisor first "
                        f"or delete {LOCK} if stale.",
                        flush=True,
                    )
                    raise SystemExit(2)
                print(
                    f"stale lock pid={old} alive but not run_unattended — clearing",
                    flush=True,
                )
            else:
                print(f"stale lock pid={old} dead — clearing", flush=True)
            try:
                LOCK.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
    # Corrupt/all-null heartbeat makes the watchdog think the app never pulses
    try:
        if HEARTBEAT.exists():
            raw = HEARTBEAT.read_bytes()
            if (not raw) or raw.count(0) > max(8, len(raw) // 2):
                HEARTBEAT.unlink(missing_ok=True)  # type: ignore[call-arg]
                print("cleared corrupt heartbeat.json", flush=True)
    except Exception:
        pass
    LOCK.write_text(f"{my_pid} {datetime.now(IST).isoformat()}\n", encoding="utf-8")


def _release_singleton() -> None:
    try:
        if LOCK.exists():
            cur = (LOCK.read_text(encoding="utf-8") or "").strip().split()[0]
            if cur == str(os.getpid()):
                LOCK.unlink(missing_ok=True)  # type: ignore[call-arg]
    except Exception:
        pass


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

        raw = HEARTBEAT.read_bytes()
        # Zero-filled / empty file → treat as missing (do not restart-loop)
        if not raw or raw.count(0) > max(8, len(raw) // 2):
            return None
        data = json.loads(raw.decode("utf-8"))
        return max(0.0, time.time() - float(data["ts"]))
    except Exception:
        return None


def _hm_to_min(hm: str) -> int:
    try:
        h, m = str(hm or "09:00").strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 9 * 60


def _ensure_login(tag: str) -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from foxtrade.auto_login import auto_login_configured, ensure_kite_session
        from foxtrade.config_store import load_config
        from foxtrade.engine import kite_idle_now

        # After 15:40 IST / weekend — no Zerodha login needed (reports-only).
        if kite_idle_now(load_config()):
            _log(f"[{tag}] kite idle after 15:40 — skip login (reports still served)")
            return
        if not auto_login_configured():
            _log(f"[{tag}] auto-login not configured — set KITE_USER_ID/PASSWORD/TOTP_SECRET in .env")
            return
        res = ensure_kite_session(force_login=True)
        _log(f"[{tag}] session ok={res.get('ok')} auto={res.get('auto_login')} reason={res.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        _log(f"[{tag}] login error: {exc}")


def _morning_logout_relogin(tag: str = "morning_9am") -> None:
    """Once per weekday at configured IST time: wipe token, then TOTP login."""
    try:
        sys.path.insert(0, str(ROOT))
        from foxtrade.auto_login import auto_login_configured, morning_logout_relogin
        from foxtrade.config_store import load_config

        cfg = load_config()
        if not cfg.get("morning_token_refresh_enabled", True):
            _log(f"[{tag}] morning token refresh disabled")
            return
        if not auto_login_configured():
            _log(f"[{tag}] auto-login not configured — cannot morning refresh")
            return
        _log(f"[{tag}] logout + relogin starting…")
        res = morning_logout_relogin()
        _log(
            f"[{tag}] ok={res.get('ok')} auto={res.get('auto_login')} "
            f"cleared={res.get('token_cleared')} "
            f"prev={res.get('prev_saved_at')} new={res.get('new_saved_at') or res.get('saved_at')} "
            f"reason={res.get('reason')}"
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"[{tag}] morning refresh error: {exc}")


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
    # NOTE: On Windows, venv's python.exe is a launcher that spawns the base
    # Local\Programs\Python\...\python.exe as a child. Do NOT kill "system"
    # python app.py copies — that murders the live interpreter.
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

        # Exactly once each weekday at morning_token_refresh_hm (default 09:00 IST):
        # delete access_token.json then force TOTP login for a fresh session.
        try:
            sys.path.insert(0, str(ROOT))
            from foxtrade.config_store import load_config as _load_cfg

            _cfg = _load_cfg()
        except Exception:
            _cfg = {}
        refresh_hm = str(_cfg.get("morning_token_refresh_hm") or "09:00")
        refresh_m = _hm_to_min(refresh_hm)
        if (
            bool(_cfg.get("morning_token_refresh_enabled", True))
            and now.weekday() < 5
            and refresh_m <= mins <= refresh_m + 2
        ):
            day_key = now.date().isoformat()
            if getattr(main, "_morning_refresh_day", None) != day_key:
                main._morning_refresh_day = day_key  # type: ignore[attr-defined]
                _morning_logout_relogin(f"morning_{refresh_hm}")

        # Soft pre-market login window (if 09:00 refresh already done, this is a no-op when ok)
        if now.weekday() < 5 and 8 * 60 + 20 <= mins <= 9 * 60 + 10:
            if time.time() - getattr(main, "_last_am_login", 0) > 180:
                _ensure_login("premarket")
                main._last_am_login = time.time()  # type: ignore[attr-defined]

        need_start = proc is None or proc.poll() is not None
        age = _hb_age()
        uptime = time.time() - last_restart if last_restart else 9999
        # Missing/corrupt heartbeat: give the app longer to write a first pulse
        # before declaring frozen (avoids restart storms after a wiped hb file).
        frozen = (
            proc is not None
            and proc.poll() is None
            and uptime > MIN_RESTART_GAP
            and (
                (age is not None and age > MAX_HB_AGE)
                or (age is None and uptime > 300)
            )
        )

        if need_start or frozen:
            why = "dead" if need_start else ("frozen" if age and age > MAX_HB_AGE else "no_heartbeat")

            # LIVE SAFETY: never kill a running app while live trading or OPEN positions.
            # Heartbeat pulse + soft stall recovery handle freezes without wiping the book.
            protect = False
            open_n = 0
            try:
                sys.path.insert(0, str(ROOT))
                from foxtrade.config_store import load_config
                from foxtrade import trade_store

                protect = bool(load_config().get("live_trading"))
                open_n = len(trade_store.open_trades())
                if open_n > 0:
                    protect = True
            except Exception:
                pass

            if frozen and protect and proc and proc.poll() is None:
                _log(
                    f"FROZEN but LIVE/OPEN({open_n}) — NOT killing pid={proc.pid} "
                    f"hb_age={age} (waiting for pulse/soft recovery)"
                )
                time.sleep(CHECK_EVERY)
                continue

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
    _acquire_singleton()
    try:
        main()
    except KeyboardInterrupt:
        _log("supervisor stopped by user")
    finally:
        _release_singleton()

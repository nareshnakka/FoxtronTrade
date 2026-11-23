import json
import subprocess
import urllib.request
from pathlib import Path

ps = r"""
Get-NetTCPConnection -LocalPort 5050 -EA SilentlyContinue | ForEach-Object {
  $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.OwningProcess) -EA SilentlyContinue
  [PSCustomObject]@{Port=5050; Pid=$_.OwningProcess; Cmd=$p.CommandLine}
} | ConvertTo-Json
"""
print(subprocess.check_output(["powershell", "-NoProfile", "-Command", ps], text=True)[:1200])
st = json.loads(urllib.request.urlopen("http://127.0.0.1:5050/api/state", timeout=5).read())
act = st.get("activity") or {}
print("headline", act.get("headline"))
print("activity keys", [k for k in act if str(k).startswith("algo")])
print("top keys", [k for k in st if str(k).startswith("algo")])
cfg = st.get("config") or {}
print("enabled", cfg.get("algo_cw_enabled"), cfg.get("algo_nw_enabled"))
log = Path("data/start_both_algos.log")
if log.exists():
    print("--- log tail ---")
    print("\n".join(log.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]))

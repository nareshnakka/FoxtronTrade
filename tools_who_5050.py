import json
import subprocess
import urllib.request

ps = r"""
Get-NetTCPConnection -LocalPort 5050 -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,LocalPort,State,OwningProcess |
  ConvertTo-Json -Compress
Write-Output '---'
Get-NetTCPConnection -LocalPort 5050 -ErrorAction SilentlyContinue |
  ForEach-Object {
    $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    $c = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.OwningProcess) -EA SilentlyContinue).CommandLine
    [PSCustomObject]@{Pid=$_.OwningProcess; Name=$p.ProcessName; Path=$p.Path; Cmd=$c}
  } | ConvertTo-Json -Compress
"""
print(subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout)
for host in ("127.0.0.1", "localhost", "::1"):
    try:
        st = json.loads(urllib.request.urlopen(f"http://{host}:5050/api/state", timeout=3).read())
        print(host, "build_id=", st.get("build_id"), "algo_cw=", "algo_cw" in st, "algo2=", "algo2" in st)
    except Exception as exc:
        print(host, type(exc).__name__, exc)

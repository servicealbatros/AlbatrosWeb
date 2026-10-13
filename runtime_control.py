"""Start/stop only processes owned by the secured Albatros AI installation."""
from __future__ import annotations

import json
import os
import signal
import socket
import ctypes
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv("ALBATROS_DATA_DIR", BASE / "data")).resolve()
RUNTIME = DATA / "runtime"
STATE_FILE = RUNTIME / "runtime_state.json"
LEGACY_REGISTRY = RUNTIME / "owned_processes.json"
PORT = int(os.getenv("ALBATROS_PORT", "8000"))
BIND_HOST = os.getenv("ALBATROS_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
API = f"http://127.0.0.1:{PORT}"
TOKEN = os.getenv("ADMIN_API_TOKEN", "")
ROBOT_BROWSER_PROFILE = DATA / "chrome_robot_profile_v4"


def _save(rows: list[dict], enabled: bool) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    components = {row["name"]: row for row in rows}
    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    last_start = rows or previous.get("last_start_processes", [])
    last_components = {row["name"]: row for row in last_start}
    state = {
        "start_timestamp": min((row.get("started_at", time.time()) for row in last_start), default=None),
        "stop_timestamp": None if enabled else time.time(),
        "robot_enabled": enabled,
        "beta_mode": True,
        "port": PORT,
        "fastapi_pid": last_components.get("fastapi", {}).get("pid"),
        "ngrok_pid": last_components.get("ngrok", {}).get("pid"),
        "browser_robot_pid": last_components.get("robot_browser", {}).get("pid"),
        "worker_pid": last_components.get("worker", {}).get("pid"),
        "processes": rows,
        "last_start_processes": last_start,
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    LEGACY_REGISTRY.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _load() -> list[dict]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        rows = value.get("processes", [])
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        try:
            value = json.loads(LEGACY_REGISTRY.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []


def _persist_robot_disabled() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / "robot_enabled.state.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": False, "changed_by": "STOP SAFE",
                               "changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), encoding="utf-8")
    tmp.replace(target)


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 258
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _existing_rows(rows: list[dict]) -> list[dict]:
    if os.name != "nt" or not rows:
        return [row for row in rows if _alive(int(row.get("pid") or 0))]
    ids = [int(row.get("pid") or 0) for row in rows if int(row.get("pid") or 0) > 0]
    script = "$ids=@(" + ",".join(str(pid) for pid in ids) + "); Get-Process -Id $ids -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    existing = {int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()}
    return [row for row in rows if int(row.get("pid") or 0) in existing]


def _spawn(name: str, command: list[str], creationflags: int | None = None, **kwargs) -> dict:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if creationflags is None else creationflags
    process = subprocess.Popen(command, cwd=BASE, creationflags=flags, **kwargs)
    return {"name": name, "pid": process.pid, "parent_pid": os.getpid(), "executable": command[0],
            "command": command, "started_at": time.time()}


def _api(path: str, method: str = "GET") -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    with urlopen(Request(API + path, method=method, headers=headers), timeout=3) as response:
        return json.loads(response.read())


def _close_robot_browser_via_cdp() -> bool:
    """Close the dedicated robot tabs through DevTools before stopping its process."""
    try:
        with urlopen("http://127.0.0.1:9222/json/list", timeout=2) as response:
            targets = json.loads(response.read())
    except Exception:
        return False
    closed = False
    for target in targets if isinstance(targets, list) else []:
        target_id = str(target.get("id") or "").strip()
        if not target_id or target.get("type") not in {"page", "webview"}:
            continue
        try:
            urlopen(f"http://127.0.0.1:9222/json/close/{quote(target_id, safe='')}", timeout=2).read()
            closed = True
        except Exception:
            continue
    return closed


def _stop_dedicated_browser_processes(force: bool = False) -> None:
    if os.name != "nt":
        return
    profile = str(ROBOT_BROWSER_PROFILE)
    if force:
        script = (
            "$profile=$env:ALBATROS_PROFILE_TO_CHECK; Get-CimInstance Win32_Process | "
            "Where-Object {$_.Name -in @('chrome.exe','msedge.exe') -and $_.CommandLine -like ('*'+$profile+'*')} | "
            "ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
        )
    else:
        # Ask only the browser windows using the robot profile to close cleanly.
        # This avoids Windows 'msedge.exe - Application Error' dialogs caused
        # by terminating the browser tree before Edge has flushed its profile.
        script = (
            "$profile=$env:ALBATROS_PROFILE_TO_CHECK; Get-CimInstance Win32_Process | "
            "Where-Object {$_.Name -in @('chrome.exe','msedge.exe') -and $_.CommandLine -like ('*'+$profile+'*')} | "
            "ForEach-Object {$p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
            "if($p -and $p.MainWindowHandle -ne 0){[void]$p.CloseMainWindow()}}"
        )
    child_env = os.environ.copy()
    child_env["ALBATROS_PROFILE_TO_CHECK"] = profile
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=child_env,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def _dedicated_browser_count() -> int:
    if os.name != "nt":
        return 0
    profile = str(ROBOT_BROWSER_PROFILE)
    script = (
        "$profile=$env:ALBATROS_PROFILE_TO_CHECK; @((Get-CimInstance Win32_Process | "
        "Where-Object {$_.Name -in @('chrome.exe','msedge.exe') -and $_.CommandLine -like ('*'+$profile+'*')})).Count"
    )
    child_env = os.environ.copy()
    child_env["ALBATROS_PROFILE_TO_CHECK"] = profile
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], env=child_env,
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def _port_listening() -> bool:
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(("127.0.0.1", PORT)) == 0


def _local_port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _local_port_owner_pid(port: int) -> int:
    if os.name != "nt":
        return 0
    script = (
        f"$c=Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess; if($c){$c}"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _ngrok_has_tunnel() -> bool:
    try:
        payload = json.load(urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2))
        return any(str(item.get("public_url") or "").startswith("https://")
                   for item in payload.get("tunnels") or [])
    except Exception:
        return False


def _webhook_is_blocked() -> bool:
    try:
        urlopen(Request(API + "/webhook/whatsapp", method="POST", data=b"{}",
                        headers={"Content-Type": "application/json"}), timeout=2)
        return False
    except HTTPError as exc:
        return exc.code == 503
    except (URLError, TimeoutError, OSError):
        return True


def start() -> int:
    if any(_alive(int(row["pid"])) for row in _load() if row.get("pid")):
        print("Albatros AI securizat este deja pornit.")
        return 0
    with socket.socket() as probe:
        probe.settimeout(.3)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"Portul {PORT} este deja ocupat de un proces neînregistrat; START a fost anulat sigur.")
    logs = RUNTIME / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # START SAFE poate fi lansat dintr-un mediu de dezvoltare cu proxy local
    # temporar. Albatros trebuie să contacteze direct OpenAI, Meta și ngrok.
    for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(proxy_name, None)
    env["ALBATROS_DATA_DIR"] = str(DATA)
    env["ROBOT_ENABLED"] = "false"
    env["BETA_MODE"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"
    rows: list[dict] = []
    server_log = (logs / "server.log").open("a", encoding="utf-8")
    rows.append(_spawn("fastapi", [sys.executable, "-m", "uvicorn", "app:app", "--host", BIND_HOST, "--port", str(PORT)],
                       env=env, stdout=server_log, stderr=subprocess.STDOUT))
    _save(rows, False)
    for _ in range(40):
        try:
            _api("/health")
            break
        except (URLError, TimeoutError, OSError):
            time.sleep(.25)
    else:
        stop()
        raise RuntimeError("FastAPI nu a pornit în 10 secunde.")
    if not TOKEN:
        stop()
        raise RuntimeError("ADMIN_API_TOKEN lipsește; robotul rămâne oprit.")
    enabled = _api("/admin/robot/enable", "POST")
    print(f"[OK] FastAPI: {API}")
    print(f"[OK] Kill switch: ROBOT_ENABLED={str(enabled.get('robot_enabled')).lower()}")

    if os.getenv("ALBATROS_SKIP_EXTERNALS", "false").lower() not in {"1", "true", "yes"}:
        ngrok = Path(os.getenv("ALBATROS_NGROK_PATH", BASE / "ngrok.exe"))
        skip_ngrok = os.getenv("ALBATROS_SKIP_NGROK", "false").lower() in {"1", "true", "yes"}
        if ngrok.is_file() and not skip_ngrok:
            if _local_port_open(4040):
                stop()
                raise RuntimeError("Există deja o instanță ngrok neînregistrată pe portul 4040; START SAFE a fost anulat.")
            ngrok_log = (logs / "ngrok.log").open("a", encoding="utf-8")
            ngrok_env = env.copy()
            rows.append(_spawn("ngrok", [str(ngrok), "http", str(PORT), "--pooling-enabled"],
                               env=ngrok_env, stdout=ngrok_log, stderr=subprocess.STDOUT))
            _save(rows, True)
            for _ in range(10):
                if _ngrok_has_tunnel():
                    break
                time.sleep(.5)
            if not _alive(int(rows[-1]["pid"])) or not _ngrok_has_tunnel():
                stop()
                raise RuntimeError("ngrok SAFE nu a publicat niciun tunel; toate componentele SAFE au fost oprite.")
            print(f"[OK] ngrok test local: PID {rows[-1]['pid']}")
        elif skip_ngrok:
            print("[SKIP] ngrok: dezactivat explicit pentru acest test sigur.")
        else:
            print("[SKIP] ngrok: ALBATROS_NGROK_PATH nu indică un executabil.")
        chrome_default = Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
        edge_default = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        browser = Path(os.getenv("ALBATROS_BROWSER_PATH", str(chrome_default if chrome_default.is_file() else edge_default)))
        if browser.is_file():
            profile = ROBOT_BROWSER_PROFILE
            command = [str(browser), f"--user-data-dir={profile}", "--new-window", "--no-first-run",
                       "--no-default-browser-check",
                       "--remote-debugging-port=9222",
                       "--remote-allow-origins=http://127.0.0.1:9222",
                       "http://127.0.0.1:8010/cereri",
                       "https://ro.e-cat.intercars.eu/ro/",
                       "https://www3.eoriginal.ro/",
                       "https://apps.dsmpro.ro/special/home/index"]
            browser_log = (logs / "browser_launcher.log").open("a", encoding="utf-8")
            browser_row = _spawn(
                "robot_browser", command, env=env, stdout=browser_log,
                stderr=subprocess.STDOUT,
            )
            launcher_pid = int(browser_row["pid"])
            for _ in range(30):
                if _local_port_open(9222):
                    break
                if not _alive(launcher_pid):
                    raise RuntimeError("Browserul robotului nu a pornit; verifică browser_launcher.log.")
                time.sleep(.5)
            rows.append(browser_row)
            _save(rows, True)
            if not _local_port_open(9222):
                stop()
                raise RuntimeError(
                    "Browserul robotului nu a deschis portul CDP 9222; "
                    "START SAFE a fost anulat."
                )
            owner_pid = _local_port_owner_pid(9222)
            if owner_pid:
                rows[-1]["launcher_pid"] = rows[-1]["pid"]
                rows[-1]["pid"] = owner_pid
                _save(rows, True)
            print(f"[OK] {browser.stem} robot dedicat: PID {rows[-1]['pid']}; profil {profile}")
            print("[OK] Worker Albatros: extensia persistentă este activă în profilul dedicat")
        else:
            print("[FAIL] Browser robot: executabilul configurat nu există.")
    worker_log = (logs / "worker.log").open("a", encoding="utf-8")
    rows.append(_spawn("worker", [sys.executable, str(BASE / "worker_supervisor.py"), API],
                       env=env, stdout=worker_log, stderr=subprocess.STDOUT))
    print(f"[OK] Worker supervisor: PID {rows[-1]['pid']}")
    _save(rows, True)
    print(f"START OK: {', '.join(row['name'] for row in rows)}; BETA_MODE=true; ROBOT_ENABLED=true")
    return 0


def stop() -> int:
    rows = _load()
    _persist_robot_disabled()
    _save(rows, False)
    webhook_blocked = False
    try:
        result = _api("/admin/robot/disable", "POST")
        print(f"Kill switch: OFF; joburi întrerupte: {result.get('interrupted_jobs', 0)}")
        webhook_blocked = _webhook_is_blocked()
    except Exception as exc:
        print(f"Kill switch API indisponibil: {exc}")
    if _close_robot_browser_via_cdp():
        print("Browser robot: filele dedicate au primit închidere normală prin CDP.")
    # Exact PIDs recorded by START; never image-name matching and never port-wide killing.
    for row in reversed(rows):
        pid = int(row.get("pid") or 0)
        if pid and _alive(pid):
            if row.get("name") == "robot_browser":
                # The dedicated browser profile is closed through its visible
                # window first. Force is reserved for the timeout fallback.
                _stop_dedicated_browser_processes(force=False)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    _stop_dedicated_browser_processes(force=False)
    deadline = time.time() + 12
    while time.time() < deadline and any(_alive(int(r.get("pid") or 0)) for r in rows):
        time.sleep(.2)
    # Escalate only the exact owned PIDs that ignored graceful shutdown.
    for row in rows:
        pid = int(row.get("pid") or 0)
        if pid and _alive(pid):
            if (row.get("name") == "robot_browser"
                    and not _local_port_open(9222)
                    and _dedicated_browser_count() == 0):
                # The browser has already closed the dedicated profile. Its original
                # launcher PID may linger briefly; forcing that PID is what
                # produced the 0x80000003 breakpoint dialog on Windows.
                continue
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    _stop_dedicated_browser_processes(force=True)
    deadline = time.time() + 30
    while time.time() < deadline and any(_alive(int(r.get("pid") or 0)) for r in rows):
        time.sleep(.2)
    survivors = _existing_rows(rows)
    if not _local_port_open(9222) and _dedicated_browser_count() == 0:
        survivors = [row for row in survivors if row.get("name") != "robot_browser"]
    if survivors:
        time.sleep(3)
        survivors = _existing_rows(survivors)
    _save(survivors, False)
    try:
        persisted_disabled = json.loads((DATA / "robot_enabled.state.json").read_text(encoding="utf-8")).get("enabled") is False
    except (OSError, ValueError, AttributeError):
        persisted_disabled = False
    checks = {
        f"port_{PORT}_closed": not _port_listening(),
        "owned_pids_stopped": not survivors,
        "ngrok_stopped": not any(r.get("name") == "ngrok" and _alive(int(r.get("pid") or 0)) for r in rows),
        "robot_browser_stopped": (not any(r.get("name") == "robot_browser" for r in rows)
                                  or _dedicated_browser_count() == 0),
        "worker_stopped": not any(r.get("name") == "worker" and _alive(int(r.get("pid") or 0)) for r in rows),
        "robot_enabled_false": persisted_disabled,
        "webhook_blocked_or_unreachable": webhook_blocked or not _port_listening(),
    }
    for name, ok in checks.items():
        print(f"[{'OK' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        details = ", ".join(name for name, ok in checks.items() if not ok)
        print("STOP SAFE = FAIL: " + details)
        return 1
    print("STOP SAFE = PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "stop"}:
        raise SystemExit("Utilizare: runtime_control.py start|stop")
    if sys.argv[1] == "stop":
        raise SystemExit(stop())
    try:
        raise SystemExit(start())
    except SystemExit:
        raise
    except Exception as exc:
        stop_result = stop()
        print(f"START SAFE = FAIL: {exc}")
        raise SystemExit(1 if stop_result == 0 else stop_result)


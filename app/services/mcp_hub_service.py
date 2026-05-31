from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.paths import APP_DATA_ROOT, PROJECT_ROOT, RESOURCE_ROOT, RUNTIME_DIR


DEFAULT_PORT = 37373
MAX_LOG_LINES = 800


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _hidden_creationflags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:  # type: ignore[name-defined]
    if sys.platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    return startupinfo





def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_command() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def _write_port_cleanup_scripts(port: int, work_dir: Path) -> tuple[Path, Path, Path]:
    """Create PowerShell scripts used by the no-prompt elevated scheduled task."""
    work_dir.mkdir(parents=True, exist_ok=True)
    task_name = f"PyWin-FreePort-{port}"
    kill_script = work_dir / f"free-port-{port}.ps1"
    runner_script = work_dir / f"run-free-port-{port}.ps1"
    log_file = work_dir / f"free-port-{port}.log"
    ps_exe = _powershell_command()
    self_pid = os.getpid()

    kill_script_text = f"""$ErrorActionPreference = 'SilentlyContinue'
$Port = {int(port)}
$SelfPid = {int(self_pid)}
$LogPath = {_ps_quote(log_file)}
"[$(Get-Date -Format 'HH:mm:ss')] Checking port $Port" | Set-Content -LiteralPath $LogPath -Encoding UTF8
$targets = @()
try {{
  $targets += Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object {{ $_.OwningProcess -and $_.OwningProcess -ne $SelfPid }} |
    Select-Object -ExpandProperty OwningProcess -Unique
}} catch {{ }}
if (-not $targets -or $targets.Count -eq 0) {{
  try {{
    $targets += netstat -ano -p tcp |
      Select-String ":$Port\\s" |
      ForEach-Object {{
        if ($_.Line -match 'LISTENING\\s+(\\d+)\\s*$') {{ [int]$Matches[1] }}
      }}
  }} catch {{ }}
}}
$targets = @($targets | Where-Object {{ $_ -and $_ -ne $SelfPid }} | Sort-Object -Unique)
if (-not $targets -or $targets.Count -eq 0) {{
  "[$(Get-Date -Format 'HH:mm:ss')] No process is listening on port $Port." | Add-Content -LiteralPath $LogPath -Encoding UTF8
  exit 0
}}
foreach ($targetPid in $targets) {{
  try {{
    Stop-Process -Id $targetPid -Force -ErrorAction Stop
    "[$(Get-Date -Format 'HH:mm:ss')] Killed PID $targetPid on port $Port." | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }} catch {{
    "[$(Get-Date -Format 'HH:mm:ss')] Failed to kill PID $targetPid on port ${{Port}}: $($_.Exception.Message)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
  }}
}}
"""
    kill_script.write_text(kill_script_text, encoding="utf-8")

    runner_script_text = f"""$ErrorActionPreference = 'Stop'
$TaskName = {_ps_quote(task_name)}
$KillScript = {_ps_quote(kill_script)}
$LogPath = {_ps_quote(log_file)}
$PsExe = {_ps_quote(ps_exe)}
$Argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $KillScript + '"'
try {{ Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null }} catch {{ }}
$Action = New-ScheduledTaskAction -Execute $PsExe -Argument $Argument
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances Parallel
try {{
  Register-ScheduledTask -TaskName $TaskName -Action $Action -Principal $Principal -Settings $Settings -Force | Out-Null
}} catch {{
  Write-Output ("Elevated scheduled task could not be registered: " + $_.Exception.Message)
  exit 2
}}
Start-ScheduledTask -TaskName $TaskName
for ($i = 0; $i -lt 40; $i++) {{
  Start-Sleep -Milliseconds 250
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $task -or $task.State -ne 'Running') {{ break }}
}}
try {{ Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null }} catch {{ }}
if (Test-Path -LiteralPath $LogPath) {{ Get-Content -LiteralPath $LogPath -Raw }}
"""
    runner_script.write_text(runner_script_text, encoding="utf-8")
    return runner_script, kill_script, log_file


def _try_free_port_with_elevated_task(port: int, log: Any) -> None:
    """Best-effort no-prompt elevated port cleanup through Task Scheduler, then direct fallback."""
    if sys.platform != "win32":
        return
    work_dir = RUNTIME_DIR / "mcp-hub" / "tasks"
    runner_script, kill_script, log_file = _write_port_cleanup_scripts(port, work_dir)
    powershell = _powershell_command()
    log(f"Trying elevated scheduled task cleanup for port {port} before MCP Hub start.")
    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner_script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_hidden_creationflags(),
        startupinfo=_hidden_startupinfo(),
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    if completed.returncode == 0:
        log(output or f"Elevated scheduled task cleanup finished for port {port}.")
        return

    log(f"Elevated scheduled task cleanup failed with exit {completed.returncode}; trying direct non-elevated cleanup. {output}".strip())
    fallback = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(kill_script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_hidden_creationflags(),
        startupinfo=_hidden_startupinfo(),
    )
    fallback_output = "\n".join(part.strip() for part in (fallback.stdout, fallback.stderr) if part and part.strip())
    if log_file.exists():
        try:
            fallback_output = (fallback_output + "\n" + log_file.read_text(encoding="utf-8", errors="replace")).strip()
        except OSError:
            pass
    log(fallback_output or f"Direct cleanup finished for port {port} with exit {fallback.returncode}.")



def _can_bind_tcp_port(port: int) -> bool:
    """Return True only when both IPv4 and IPv6 loopback binds look available."""
    families = [socket.AF_INET]
    if socket.has_ipv6:
        families.append(socket.AF_INET6)
    for family in families:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.bind(("::1", int(port)))
            else:
                sock.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def _wait_for_port_free(port: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.1, seconds)
    while time.monotonic() < deadline:
        if _can_bind_tcp_port(port):
            return True
        time.sleep(0.25)
    return _can_bind_tcp_port(port)


def _package_signature(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part in {"node_modules", "dist", ".git"} for part in rel.parts):
            continue
        if path.is_file():
            digest.update(rel.as_posix().encode("utf-8", errors="ignore"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                pass
    return digest.hexdigest()

def _which(command: str) -> str:
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if sys.platform == "win32" and not command.lower().endswith(".cmd"):
        resolved_cmd = shutil.which(f"{command}.cmd")
        if resolved_cmd:
            return resolved_cmd
    return command


class MCPHubService:
    """Small process manager for the bundled Node-based MCP Hub."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._worker: threading.Thread | None = None
        self._active_process: subprocess.Popen[str] | None = None
        self._hub_process: subprocess.Popen[str] | None = None
        self._ngrok_process: subprocess.Popen[str] | None = None
        self._ngrok_worker: threading.Thread | None = None
        self._status = "stopped"
        self._last_exit_code: int | None = None
        self._last_error = ""
        self._port = DEFAULT_PORT

    @property
    def config_path(self) -> Path:
        # MCP Hub should use the normal user config location, matching:
        # C:\Users\paul\.config\mcp-hub\mcp.json on Paul's Windows machine.
        override = os.environ.get("PYWIN_MCP_HUB_CONFIG")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".config" / "mcp-hub" / "mcp.json"

    @property
    def runtime_package_dir(self) -> Path:
        return RUNTIME_DIR / "mcp-hub" / "package"

    @property
    def source_package_dir(self) -> Path:
        return RESOURCE_ROOT / "vendor" / "mcp-hub"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def append_log(self, message: str) -> None:
        text = str(message or "").rstrip()
        if not text:
            return
        with self._lock:
            for line in text.splitlines():
                self._logs.append(f"[{_now()}] {line}")

    def ensure_files(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        config = self.config_path
        config.parent.mkdir(parents=True, exist_ok=True)
        if not config.exists():
            bundled_config = RESOURCE_ROOT / "mcp.json"
            if bundled_config.exists():
                config.write_text(bundled_config.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                config.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")

        source = self.source_package_dir
        target = self.runtime_package_dir
        if not source.exists():
            raise FileNotFoundError(f"Bundled MCP Hub package not found: {source}")

        marker = target / ".pywin-mcp-hub-source.txt"
        source_marker = _package_signature(source)
        marker_value = marker.read_text(encoding="utf-8", errors="ignore") if marker.exists() else ""
        if not target.exists() or not (target / "package.json").exists() or marker_value != source_marker:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", "dist", ".git"))
            marker.write_text(source_marker, encoding="utf-8")

    def _is_installed(self) -> bool:
        package_dir = self.runtime_package_dir
        return (package_dir / "node_modules" / "express").exists() and (package_dir / "node_modules" / "@modelcontextprotocol" / "sdk").exists()

    def _run_blocking(self, command: list[str], cwd: Path, label: str) -> int:
        self.append_log(f"{label}: {' '.join(command)}")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_hidden_creationflags(),
            startupinfo=_hidden_startupinfo(),
        )
        with self._lock:
            self._active_process = process
        assert process.stdout is not None
        for line in process.stdout:
            self.append_log(line)
        code = process.wait()
        with self._lock:
            if self._active_process is process:
                self._active_process = None
        self.append_log(f"{label} exited with {code}.")
        return int(code)

    def start(self, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            if port:
                self._port = int(port)
            if self._worker and self._worker.is_alive():
                return self.state(message="MCP Hub is already starting/running.")
            if self._hub_process and self._hub_process.poll() is None:
                self._status = "running"
                return self.state(message="MCP Hub is already running.")
            self._status = "starting"
            self._last_error = ""
            self._last_exit_code = None
            self._worker = threading.Thread(target=self._start_worker, name="MCPHubService", daemon=True)
            self._worker.start()
        return self.state(message="MCP Hub start requested.")

    def _start_worker(self) -> None:
        try:
            _try_free_port_with_elevated_task(self._port, self.append_log)
            if not _wait_for_port_free(self._port, seconds=6.0):
                message = f"Port {self._port} is still occupied after cleanup; MCP Hub start aborted to avoid EADDRINUSE."
                with self._lock:
                    self._status = "error"
                    self._last_error = message
                self.append_log(message)
                return
            self.append_log(f"Port {self._port} is free; starting MCP Hub.")
            self.ensure_files()
            package_dir = self.runtime_package_dir
            if not self._is_installed():
                with self._lock:
                    self._status = "installing"
                self.append_log("node_modules missing; running npm install for bundled MCP Hub package.")
                npm = _which("npm")
                code = self._run_blocking([npm, "install"], package_dir, "npm install")
                if code != 0:
                    with self._lock:
                        self._status = "error"
                        self._last_exit_code = code
                        self._last_error = "npm install failed. Check the MCP Hub log panel."
                    return

            node = _which("node")
            cli = package_dir / "src" / "utils" / "cli.js"
            command = [node, str(cli), "--port", str(self._port), "--config", str(self.config_path), "--watch"]
            env = os.environ.copy()
            env.setdefault("NO_COLOR", "1")
            state_root = RUNTIME_DIR / "mcp-hub" / "state"
            data_root = RUNTIME_DIR / "mcp-hub" / "data"
            config_root = RUNTIME_DIR / "mcp-hub" / "config"
            state_root.mkdir(parents=True, exist_ok=True)
            data_root.mkdir(parents=True, exist_ok=True)
            config_root.mkdir(parents=True, exist_ok=True)
            env["XDG_STATE_HOME"] = str(state_root)
            env["XDG_DATA_HOME"] = str(data_root)
            env["XDG_CONFIG_HOME"] = str(config_root)

            self.append_log(f"Starting MCP Hub on {self.base_url}")
            self.append_log(f"Config: {self.config_path}")
            process = subprocess.Popen(
                command,
                cwd=str(package_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=_hidden_creationflags(),
                startupinfo=_hidden_startupinfo(),
            )
            with self._lock:
                self._active_process = process
                self._hub_process = process
                self._status = "running"

            assert process.stdout is not None
            for line in process.stdout:
                self.append_log(line)
            code = int(process.wait())
            with self._lock:
                self._last_exit_code = code
                if self._active_process is process:
                    self._active_process = None
                if self._hub_process is process:
                    self._hub_process = None
                if self._status == "stopping" or code == 0:
                    self._status = "stopped"
                else:
                    self._status = "error"
                    self._last_error = f"MCP Hub exited with code {code}."
            self.append_log(f"MCP Hub exited with {code}.")
        except Exception as exc:  # noqa: BLE001 - surfaced in UI.
            with self._lock:
                self._status = "error"
                self._last_error = f"{type(exc).__name__}: {exc}"
            self.append_log(self._last_error)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._active_process or self._hub_process
            if not process or process.poll() is not None:
                self._status = "stopped"
                self._active_process = None
                self._hub_process = None
                return self.state(message="MCP Hub is already stopped.")
            self._status = "stopping"
            self.append_log("Stopping MCP Hub...")

        try:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.append_log("Terminate timed out; killing MCP Hub process.")
                process.kill()
                process.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._status = "error"
            self.append_log(self._last_error)
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None
                if self._hub_process is process:
                    self._hub_process = None
                if self._status == "stopping":
                    self._status = "stopped"
        return self.state(message="MCP Hub stop requested.")

    def restart(self, port: int | None = None) -> dict[str, Any]:
        self.stop()
        return self.start(port=port)

    def _http_json(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost only.
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def _safe_http_json(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        try:
            return self._http_json(path, method=method, payload=payload), ""
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return None, f"HTTP {exc.code}: {body or exc.reason}"
        except (URLError, TimeoutError, ConnectionError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def start_server(self, name: str) -> dict[str, Any]:
        result, error = self._safe_http_json("/api/servers/start", method="POST", payload={"server_name": name})
        if error:
            self.append_log(f"Start server '{name}' failed: {error}")
            return {**self.state(), "ok": False, "error": error}
        self.append_log(f"Start server '{name}' requested.")
        return {**self.state(), "ok": True, "result": result}

    def stop_server(self, name: str, disable: bool = False) -> dict[str, Any]:
        suffix = "?disable=true" if disable else "?disable=false"
        result, error = self._safe_http_json(f"/api/servers/stop{suffix}", method="POST", payload={"server_name": name})
        if error:
            self.append_log(f"Stop server '{name}' failed: {error}")
            return {**self.state(), "ok": False, "error": error}
        self.append_log(f"Stop server '{name}' requested.")
        return {**self.state(), "ok": True, "result": result}

    def refresh(self) -> dict[str, Any]:
        result, error = self._safe_http_json("/api/restart", method="POST", payload={})
        if error:
            self.append_log(f"Hub reload failed: {error}")
            return {**self.state(), "ok": False, "error": error}
        self.append_log("Hub config reload requested.")
        return {**self.state(), "ok": True, "result": result}

    def start_ngrok(self, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            if port:
                self._port = int(port)
            if self._ngrok_process and self._ngrok_process.poll() is None:
                return self.state(message="ngrok is already running.")
            if self._ngrok_worker and self._ngrok_worker.is_alive():
                return self.state(message="ngrok is already starting.")
            self._ngrok_worker = threading.Thread(target=self._start_ngrok_worker, name="MCPHubNgrok", daemon=True)
            self._ngrok_worker.start()
        return self.state(message="ngrok start requested.")

    def stop_ngrok(self) -> dict[str, Any]:
        with self._lock:
            process = self._ngrok_process
            if not process or process.poll() is not None:
                self._ngrok_process = None
                return self.state(message="ngrok is already stopped.")
            self.append_log("Stopping ngrok...")

        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.append_log("ngrok terminate timed out; killing process.")
                process.kill()
                process.wait(timeout=3)
        except Exception as exc:  # noqa: BLE001 - UI-facing local automation errors.
            error = f"{type(exc).__name__}: {exc}"
            self.append_log(f"ngrok stop failed: {error}")
            return {**self.state(), "ok": False, "error": error}
        finally:
            with self._lock:
                if self._ngrok_process is process:
                    self._ngrok_process = None
        return self.state(message="ngrok stopped.")

    def _start_ngrok_worker(self) -> None:
        try:
            command = ["wsl", "ngrok", "http", str(self._port), "--host-header=rewrite"]
            self.append_log(f"Starting ngrok: {' '.join(command)}")
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_hidden_creationflags(),
                startupinfo=_hidden_startupinfo(),
            )
            with self._lock:
                self._ngrok_process = process
            assert process.stdout is not None
            for line in process.stdout:
                self.append_log(f"ngrok: {line}")
            code = int(process.wait())
            with self._lock:
                if self._ngrok_process is process:
                    self._ngrok_process = None
            self.append_log(f"ngrok exited with {code}.")
        except Exception as exc:  # noqa: BLE001 - surfaced in UI.
            with self._lock:
                self._ngrok_process = None
            self.append_log(f"ngrok failed: {type(exc).__name__}: {exc}")

    def _server_name_from_url(self, url: str) -> str:
        cleaned = url.strip().replace("https://", "").replace("http://", "")
        cleaned = cleaned.split("/", 1)[0].split(":", 1)[0]
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", cleaned).strip("-._")
        return cleaned or "remote-server"

    def _split_command_line(self, command_line: str) -> list[str]:
        try:
            parts = shlex.split(command_line, posix=False)
        except ValueError:
            parts = command_line.split()
        return [part.strip().strip('"').strip("'") for part in parts if part.strip().strip('"').strip("'")]

    def add_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self.ensure_files()
            path = self.config_path
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                config = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                return {**self.state(), "ok": False, "error": f"Existing mcp.json is invalid: {exc}"}

            if not isinstance(config, dict):
                config = {}
            servers = config.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
                config["mcpServers"] = servers

            server_type = str((payload or {}).get("serverType") or "remote").strip().lower()
            name = str((payload or {}).get("name") or "").strip()

            if server_type == "remote":
                url = str((payload or {}).get("url") or "").strip()
                if not url:
                    return {**self.state(), "ok": False, "error": "Remote URL is required."}
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"
                name = name or self._server_name_from_url(url)
                entry: dict[str, Any] = {"url": url}
            elif server_type == "local":
                command_line = str((payload or {}).get("commandLine") or "").strip()
                parts = self._split_command_line(command_line)
                if not parts:
                    return {**self.state(), "ok": False, "error": "Local start command is required."}
                name = name or Path(parts[0]).stem or "local-server"
                entry = {"type": "stdio", "command": parts[0], "args": parts[1:]}
            else:
                return {**self.state(), "ok": False, "error": "Server type must be remote or local."}

            if not re.match(r"^[A-Za-z0-9_.-]+$", name):
                return {**self.state(), "ok": False, "error": "Server name may only use letters, numbers, dot, underscore, and dash."}

            servers[name] = entry
            formatted = json.dumps(config, indent=2, ensure_ascii=False)
            json.loads(formatted)
            path.write_text(formatted + "\n", encoding="utf-8")

            self.append_log(f"Added MCP server '{name}' to {path}; JSON format check passed.")
            return {**self.state(message=f"Added MCP server '{name}'. JSON format check passed."), "ok": True, "serverName": name, "configPath": str(path)}
        except Exception as exc:  # noqa: BLE001 - UI-facing local automation errors.
            error = f"{type(exc).__name__}: {exc}"
            self.append_log(f"Add MCP server failed: {error}")
            return {**self.state(), "ok": False, "error": error}

    def open_config(self) -> dict[str, Any]:
        try:
            self.ensure_files()
            path = self.config_path
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self.append_log(f"Opened config: {path}")
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self.append_log(f"Open config failed: {error}")
            return {"ok": False, "error": error, "path": str(self.config_path)}

    def state(self, message: str | None = None) -> dict[str, Any]:
        with self._lock:
            process = self._hub_process
            active = self._active_process
            running = bool(process and process.poll() is None)
            ngrok_process = self._ngrok_process
            ngrok_running = bool(ngrok_process and ngrok_process.poll() is None)
            ngrok_pid = ngrok_process.pid if ngrok_running else None
            pid = process.pid if running else (active.pid if active and active.poll() is None else None)
            status = self._status
            logs = list(self._logs)
            last_error = self._last_error
            exit_code = self._last_exit_code
            port = self._port

        servers_data, servers_error = (None, "")
        health_data, health_error = (None, "")
        if running or status in {"running", "starting"}:
            servers_data, servers_error = self._safe_http_json("/api/servers")
            health_data, health_error = self._safe_http_json("/api/health")

        servers = []
        if servers_data and isinstance(servers_data.get("servers"), list):
            servers = servers_data["servers"]

        ready_state = "offline"
        if health_data and health_data.get("state"):
            ready_state = str(health_data.get("state"))
        elif status in {"installing", "starting", "running", "stopping", "error"}:
            ready_state = status

        setup = {
            "node": _which("node"),
            "npm": _which("npm"),
            "installed": self._is_installed(),
        }
        return {
            "ok": not bool(last_error) or status in {"stopped", "starting", "installing", "running"},
            "message": message or "",
            "status": status,
            "readyState": ready_state,
            "running": running,
            "pid": pid,
            "ngrokRunning": ngrok_running,
            "ngrokPid": ngrok_pid,
            "port": port,
            "baseUrl": f"http://127.0.0.1:{port}",
            "mcpEndpoint": f"http://127.0.0.1:{port}/mcp",
            "configPath": str(self.config_path),
            "packageDir": str(self.runtime_package_dir),
            "sourcePackageDir": str(self.source_package_dir),
            "lastExitCode": exit_code,
            "lastError": last_error,
            "logs": logs[-MAX_LOG_LINES:],
            "servers": servers,
            "serversError": servers_error,
            "health": health_data or {},
            "healthError": health_error,
            "setup": setup,
        }


mcp_hub_service = MCPHubService()

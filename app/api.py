from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import CONFIG
from app.core.paths import HISTORY_FILE, PROJECT_ROOT, SCRIPT_WORKSPACE, ensure_runtime_dirs
from app.services.history_store import append_history, read_history
from app.services.script_detector import LANGUAGE_LABELS, ScriptLanguage, detect_script_language, extract_argument_hints
from app.services.script_runner import RunnerRequest, result_to_dict, run_script
from app.services.system_info import detect_runtime_info, system_summary
from app.services.mcp_hub_service import mcp_hub_service





def _resolve_7zip_path() -> str:
    """Resolve the 7-Zip CLI without shelling out to git or other helpers."""
    for command in ("7z.exe", "7z", "7za.exe", "7za"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    candidates = [
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "7-Zip" / "7z.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("7-Zip CLI was not found. Install 7-Zip or add 7z.exe to PATH.")




def _has_7zip() -> bool:
    try:
        _resolve_7zip_path()
        return True
    except FileNotFoundError:
        return False


def _gitignore_lines(project_dir: Path) -> list[str]:
    """Read usable .gitignore lines for 7-Zip exclude switches."""
    ignore_file = project_dir / ".gitignore"
    if not ignore_file.exists():
        return []
    try:
        raw_lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    patterns: list[str] = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        # Git supports escaped comments/spaces and negation. 7-Zip only has exclude filters,
        # so keep the common ignore cases stable and skip re-include rules.
        if line.startswith("\\#"):
            line = line[1:]
        if line.startswith("\\!"):
            line = line[1:]
        line = line.replace("/", "\\").strip("\\")
        if line:
            patterns.append(line)
    return patterns


def _expand_7zip_exclude_patterns(project_dir: Path) -> list[str]:
    """Convert .gitignore-style entries into practical recursive 7-Zip filters."""
    defaults = [
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "*.pyc",
        ".DS_Store",
    ]
    root_name = project_dir.name
    expanded: set[str] = set()

    def add(pattern: str) -> None:
        value = pattern.strip().strip("\\")
        if not value:
            return
        expanded.add(value)
        if not any(char in value for char in "*?"):
            expanded.add(value + "\\*")
        if not value.startswith(root_name + "\\"):
            rooted = root_name + "\\" + value
            expanded.add(rooted)
            if not any(char in rooted for char in "*?"):
                expanded.add(rooted + "\\*")

    for pattern in defaults + _gitignore_lines(project_dir):
        add(pattern)

    return [f"-xr!{pattern}" for pattern in sorted(expanded, key=str.lower)]


def _zip_project_with_7zip(source: Path, destination: Path) -> dict[str, Any]:
    """Create source.name/source files as a zip, respecting .gitignore through 7-Zip filters."""
    seven_zip = _resolve_7zip_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    excludes = _expand_7zip_exclude_patterns(source)
    command = [
        seven_zip,
        "a",
        "-tzip",
        "-mx=5",
        "-r",
        str(destination),
        source.name,
        *excludes,
    ]
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=str(source.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return {
        "ok": completed.returncode == 0,
        "exitCode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "durationSeconds": round(time.time() - started, 3),
        "command": " ".join(command),
        "excludeCount": len(excludes),
    }


def _folder_latest_mtime(folder: Path) -> float:
    newest = _safe_modified_timestamp(folder)
    for child in folder.rglob("*"):
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


def _resolve_executable(command: str, fallback: str | None = None) -> str:
    """Resolve a Windows executable from PATH first, then a fallback path."""
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if fallback and Path(fallback).exists():
        return fallback
    return command


def _normalize_browser_url(raw_url: str) -> str:
    url = (raw_url or '').strip()
    if not url:
        url = 'https://example.com'
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f'https://{url}'
    return url


def _resolve_alacritty_path() -> Path | None:
    """Find Alacritty in PATH or common Windows install locations."""
    resolved = shutil.which("alacritty.exe") or shutil.which("alacritty")
    candidates = [
        Path(resolved) if resolved else None,
        Path(r"C:\Program Files\Alacritty\alacritty.exe"),
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "alacritty.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def _safe_modified_timestamp(path: Path) -> float:
    """Return a best-effort last-change timestamp without blocking the UI."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _format_timestamp(timestamp: float | int | None) -> str:
    if not timestamp:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
    except (OSError, OverflowError, ValueError):
        return ""


def _parse_iso_timestamp(raw_value: str) -> float:
    value = (raw_value or "").strip()
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _project_entry(path: Path) -> dict[str, str | float]:
    timestamp = _safe_modified_timestamp(path)
    return {
        "name": path.name,
        "path": str(path),
        "lastChanged": _format_timestamp(timestamp),
        "lastChangedTimestamp": timestamp,
    }


def _parse_repo_visibility_lines(output: str) -> list[dict[str, str | float]]:
    repos: list[dict[str, str | float]] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        timestamp = _parse_iso_timestamp(parts[2]) if len(parts) >= 3 else 0.0
        repos.append(
            {
                "name": parts[0],
                "visibility": "Private" if parts[1].lower() == "true" else "Public",
                "lastChanged": _format_timestamp(timestamp),
                "changedTimestamp": timestamp,
            }
        )
    repos.sort(key=lambda repo: (-(float(repo.get("changedTimestamp") or 0.0)), str(repo.get("name") or "").lower()))
    return repos


def _looks_like_git_remote(value: str) -> bool:
    lower = value.strip().lower()
    return lower.startswith(("http://", "https://", "ssh://", "git@")) or lower.endswith(".git")


def _current_github_login(gh_command: str) -> str:
    try:
        completed = subprocess.run(
            [gh_command, "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except Exception:
        pass
    return ""


def _github_remote_url_from_spec(spec: str, gh_command: str) -> str:
    value = (spec or "").strip()
    if not value:
        raise ValueError("Remote/repository value is empty.")
    if _looks_like_git_remote(value):
        return value
    repo_name = value.strip("/")
    if "/" not in repo_name:
        owner = _current_github_login(gh_command)
        if not owner:
            raise ValueError("Use owner/repo or a full remote URL. Could not read current gh user login.")
        repo_name = f"{owner}/{repo_name}"
    return f"https://github.com/{repo_name}.git"


class BackendApi:
    """Python methods exposed to JavaScript through pywebview's js_api bridge."""

    def __init__(self) -> None:
        ensure_runtime_dirs()

    def get_state(self) -> dict[str, Any]:
        """Return startup data for the HTML frontend."""
        return {
            "config": {
                "title": CONFIG.title,
                "subtitle": CONFIG.subtitle,
                "defaultTimeoutSeconds": CONFIG.default_timeout_seconds,
                "maxOutputChars": CONFIG.max_output_chars,
            },
            "paths": {
                "projectRoot": str(PROJECT_ROOT),
                "scriptWorkspace": str(SCRIPT_WORKSPACE),
                "historyFile": str(HISTORY_FILE),
            },
            "system": system_summary(),
            "runtimes": [runtime.__dict__ for runtime in detect_runtime_info()],
            "languages": {language.value: label for language, label in LANGUAGE_LABELS.items()},
            "history": read_history(limit=20),
        }

    def read_clipboard(self) -> dict[str, Any]:
        """Read text from the Windows clipboard using tkinter, avoiding an extra dependency."""
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return {"ok": True, "text": text}
        except Exception as exc:  # noqa: BLE001 - clipboard errors are UI-facing.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "text": ""}

    def detect_script(self, content: str) -> dict[str, Any]:
        result = detect_script_language(content or "")
        hints = extract_argument_hints(content or "", result.language)
        return {
            "language": result.language.value,
            "label": LANGUAGE_LABELS.get(result.language, result.language.value),
            "confidence": result.confidence,
            "reasons": list(result.reasons),
            "hints": [hint.__dict__ for hint in hints],
        }

    def run_script(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            language = ScriptLanguage(str(payload.get("language") or ScriptLanguage.TEXT.value))
        except ValueError:
            language = ScriptLanguage.TEXT

        request = RunnerRequest(
            content=str(payload.get("content") or ""),
            language=language,
            args_text=str(payload.get("argsText") or ""),
            working_directory=str(payload.get("workingDirectory") or ""),
            timeout_seconds=int(payload.get("timeoutSeconds") or CONFIG.default_timeout_seconds),
            env_text=str(payload.get("envText") or ""),
        )
        result = run_script(request)
        record = result_to_dict(result)
        record["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        append_history(record)
        return {"ok": result.exit_code == 0, "result": record, "history": read_history(limit=20)}

    def read_history(self, limit: int = 20) -> dict[str, Any]:
        return {"history": read_history(limit=max(1, min(int(limit or 20), 200)))}

    def get_browser_state(self) -> dict[str, Any]:
        return {
            "defaultUrl": "https://example.com",
            "devtoolsEnabled": True,
            "note": "Run the app with --debug to make pywebview devtools available.",
        }

    def open_browser_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Open a URL in a separate pywebview window; debug/devtools follows app debug mode."""
        try:
            import webview

            url = _normalize_browser_url(str(payload.get("url") or ""))
            title = str(payload.get("title") or "Browser WebView").strip() or "Browser WebView"
            webview.create_window(
                title=title,
                url=url,
                width=int(payload.get("width") or 1100),
                height=int(payload.get("height") or 800),
                min_size=(720, 460),
                text_select=True,
                background_color="#f3f3f3",
            )
            return {"ok": True, "url": url, "title": title}
        except Exception as exc:  # noqa: BLE001 - surfaced to UI.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


    def get_warehouse_state(self) -> dict[str, Any]:
        """Return defaults and discovered projects for the GUI.ahk-inspired warehouse tab."""
        default_project_dir = Path(r"C:\Users\paul\Documents\.projects")
        management_dir = Path(r"C:\Users\paul\Documents\.project-managment")
        scripts_dir = management_dir / "scripts"
        output_dir = management_dir / "repo-outputs"
        zip_dir = management_dir / "zips"
        config_path = management_dir / "repomix.config.json"

        projects: list[dict[str, str | float]] = []
        if default_project_dir.exists():
            folders = [
                item
                for item in default_project_dir.iterdir()
                if item.is_dir() and item.name not in {".git", ".project-managment"}
            ]
            folders.sort(key=lambda item: (-_safe_modified_timestamp(item), item.name.lower()))
            projects = [_project_entry(item) for item in folders]

        return {
            "defaultProjectDir": str(default_project_dir),
            "managementDir": str(management_dir),
            "scriptsDir": str(scripts_dir),
            "outputDir": str(output_dir),
            "zipDir": str(zip_dir),
            "configPath": str(config_path),
            "projects": projects,
            "scriptAvailability": {
                "repoVisibilityList": (scripts_dir / "repo-visibility-list.ps1").exists(),
                "repoVisibilityToggle": (scripts_dir / "repo-visibility-toggle.cmd").exists(),
                "sevenZipCli": _has_7zip(),
                "zipSelected": True,
                "updateZips": True,
                "createAllZips": True,
                "repomixConfig": config_path.exists(),
            },
        }

    def run_warehouse_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run local project-management actions ported from GUI.ahk."""
        action = str(payload.get("action") or "").strip()
        base_dir = Path(str(payload.get("baseDir") or r"C:\Users\paul\Documents\.projects")).expanduser()
        management_dir = Path(r"C:\Users\paul\Documents\.project-managment")
        scripts_dir = management_dir / "scripts"
        output_dir = management_dir / "repo-outputs"
        zip_dir = management_dir / "zips"
        config_path = management_dir / "repomix.config.json"

        def run_command(command: list[str] | str, cwd: Path | None = None, shell: bool = False) -> dict[str, Any]:
            started = time.time()
            completed = subprocess.run(
                command,
                cwd=str(cwd or base_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=shell,
            )
            return {
                "ok": completed.returncode == 0,
                "exitCode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "durationSeconds": round(time.time() - started, 3),
                "command": command if isinstance(command, str) else " ".join(command),
            }

        try:
            if action == "refresh_projects":
                return {"ok": True, "state": self.get_warehouse_state()}

            if action == "repo_visibility_list":
                # Prefer gh directly so we can sort by the newest repository activity (pushedAt).
                gh = shutil.which("gh")
                result: dict[str, Any] | None = None
                repos: list[dict[str, str | float]] = []
                if gh:
                    template = '{{range .}}{{.name}}|{{.isPrivate}}|{{.pushedAt}}{{"\n"}}{{end}}'
                    result = run_command([gh, "repo", "list", "--limit", "300", "--json", "name,isPrivate,pushedAt", "--template", template], base_dir)
                    repos = _parse_repo_visibility_lines(str(result.get("stdout") or ""))
                    if repos:
                        result["source"] = "gh repo list"

                if not repos:
                    script = scripts_dir / "repo-visibility-list.ps1"
                    if not script.exists():
                        if result and result.get("stderr"):
                            return {"ok": False, "error": str(result.get("stderr")), "command": result.get("command")}
                        return {"ok": False, "error": f"Missing script: {script}"}
                    result = run_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], base_dir)
                    repos = _parse_repo_visibility_lines(str(result.get("stdout") or ""))
                    result["source"] = "repo-visibility-list.ps1"

                result["repos"] = repos
                return result

            if action == "repo_visibility_toggle":
                script = scripts_dir / "repo-visibility-toggle.cmd"
                repo = str(payload.get("repo") or "").strip()
                if not repo:
                    return {"ok": False, "error": "No repository name supplied."}
                if not script.exists():
                    return {"ok": False, "error": f"Missing script: {script}"}
                return run_command([str(script), repo], base_dir)

            if action in {"git_status", "git_connect_remote", "git_create_remote_repo", "git_fetch", "git_push"}:
                target = Path(str(payload.get("projectDir") or base_dir)).expanduser()
                if not target.exists() or not target.is_dir():
                    return {"ok": False, "error": f"Project folder not found: {target}"}

                git = shutil.which("git") or "git"
                gh = shutil.which("gh") or "gh"

                def ensure_git_repo() -> dict[str, Any] | None:
                    probe = run_command([git, "rev-parse", "--is-inside-work-tree"], target)
                    if probe.get("ok"):
                        return None
                    init_result = run_command([git, "init"], target)
                    if not init_result.get("ok"):
                        return init_result
                    return None

                def combined_result(parts: list[dict[str, Any]], label: str) -> dict[str, Any]:
                    ok = all(bool(part.get("ok")) for part in parts)
                    stdout_chunks = []
                    stderr_chunks = []
                    for part in parts:
                        command_text = str(part.get("command") or "")
                        stdout_chunks.append(f"$ {command_text}\n{str(part.get('stdout') or '').rstrip()}".rstrip())
                        if part.get("stderr"):
                            stderr_chunks.append(f"$ {command_text}\n{str(part.get('stderr') or '').rstrip()}".rstrip())
                    return {
                        "ok": ok,
                        "exitCode": 0 if ok else 1,
                        "stdout": f"# {label}\n" + "\n\n".join(chunk for chunk in stdout_chunks if chunk),
                        "stderr": "\n\n".join(chunk for chunk in stderr_chunks if chunk),
                        "command": label,
                    }

                if action == "git_status":
                    probe = run_command([git, "rev-parse", "--is-inside-work-tree"], target)
                    if not probe.get("ok"):
                        return {
                            "ok": False,
                            "exitCode": probe.get("exitCode", 1),
                            "stdout": f"Project: {target}\nNot a git repository yet. Use Create Remote Repo or Connect Remote to initialize it.",
                            "stderr": str(probe.get("stderr") or ""),
                            "command": str(probe.get("command") or "git rev-parse"),
                        }
                    branch = run_command([git, "status", "--short", "--branch"], target)
                    remotes = run_command([git, "remote", "-v"], target)
                    return combined_result([branch, remotes], f"Git status for {target}")

                init_error = ensure_git_repo()
                if init_error:
                    return init_error

                if action == "git_connect_remote":
                    remote_spec = str(payload.get("remote") or "").strip()
                    remote_name = str(payload.get("remoteName") or "origin").strip() or "origin"
                    try:
                        remote_url = _github_remote_url_from_spec(remote_spec, gh)
                    except ValueError as exc:
                        return {"ok": False, "error": str(exc)}
                    existing = run_command([git, "remote", "get-url", remote_name], target)
                    if existing.get("ok"):
                        remote_result = run_command([git, "remote", "set-url", remote_name, remote_url], target)
                    else:
                        remote_result = run_command([git, "remote", "add", remote_name, remote_url], target)
                    status_result = run_command([git, "remote", "-v"], target)
                    return combined_result([remote_result, status_result], f"Connected {remote_name} to {remote_url}")

                if action == "git_create_remote_repo":
                    repo_name = str(payload.get("repoName") or target.name).strip()
                    visibility = str(payload.get("visibility") or "private").strip().lower()
                    if visibility not in {"public", "private", "internal"}:
                        visibility = "private"
                    if not repo_name:
                        return {"ok": False, "error": "Repository name is empty."}
                    create_result = run_command([gh, "repo", "create", repo_name, "--source", str(target), "--remote", "origin", f"--{visibility}"], target)
                    remote_result = run_command([git, "remote", "-v"], target)
                    return combined_result([create_result, remote_result], f"Created GitHub repo {repo_name}")

                if action == "git_fetch":
                    return run_command([git, "fetch", "--all", "--prune"], target)

                if action == "git_push":
                    remote_probe = run_command([git, "remote", "get-url", "origin"], target)
                    if not remote_probe.get("ok"):
                        return {"ok": False, "error": "No origin remote configured. Connect or create a remote first.", "stderr": str(remote_probe.get("stderr") or "")}
                    branch_probe = run_command([git, "branch", "--show-current"], target)
                    branch = str(branch_probe.get("stdout") or "").strip() or "main"
                    if not str(branch_probe.get("stdout") or "").strip():
                        rename_result = run_command([git, "branch", "-M", branch], target)
                        if not rename_result.get("ok"):
                            return rename_result
                    upstream_probe = run_command([git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], target)
                    if upstream_probe.get("ok"):
                        return run_command([git, "push"], target)
                    return run_command([git, "push", "-u", "origin", branch], target)

            def zip_projects_with_7zip(project_paths: list[Path], only_when_changed: bool = False) -> dict[str, Any]:
                zip_dir.mkdir(parents=True, exist_ok=True)
                logs: list[str] = []
                ok = True
                for source in project_paths:
                    if not source.exists() or not source.is_dir():
                        ok = False
                        logs.append(f"SKIP missing folder: {source}")
                        continue
                    destination_dir = zip_dir / source.name
                    destination = destination_dir / f"{source.name}.zip"
                    if only_when_changed and destination.exists() and destination.stat().st_mtime >= _folder_latest_mtime(source):
                        logs.append(f"UNCHANGED {source} -> {destination}")
                        continue
                    try:
                        result = _zip_project_with_7zip(source, destination)
                    except Exception as exc:  # noqa: BLE001 - UI-facing local automation errors.
                        ok = False
                        logs.append(f"FAIL {source}: {type(exc).__name__}: {exc}")
                        continue
                    ok = ok and bool(result.get("ok"))
                    status = "OK" if result.get("ok") else "FAIL"
                    logs.append(
                        f"{status} {source} -> {destination} "
                        f"(7-Zip filters: {result.get('excludeCount', 0)}, exit: {result.get('exitCode')})"
                    )
                    stdout = str(result.get("stdout") or "").strip()
                    stderr = str(result.get("stderr") or "").strip()
                    if stdout:
                        logs.append(stdout[-4000:])
                    if stderr:
                        logs.append(stderr[-4000:])
                return {"ok": ok, "stdout": "\n".join(logs), "zipDir": str(zip_dir)}

            if action == "zip_selected":
                selected = payload.get("projects") or []
                if not isinstance(selected, list) or not selected:
                    return {"ok": False, "error": "No projects selected."}
                project_paths = [
                    Path(str(entry.get("path") if isinstance(entry, dict) else entry)).expanduser()
                    for entry in selected
                ]
                return zip_projects_with_7zip(project_paths)

            if action in {"update_zips", "create_all_zips"}:
                if not base_dir.exists() or not base_dir.is_dir():
                    return {"ok": False, "error": f"Project folder not found: {base_dir}"}
                project_paths = [
                    item
                    for item in sorted(base_dir.iterdir(), key=lambda item: item.name.lower())
                    if item.is_dir() and item.name not in {".git", ".project-managment"}
                ]
                return zip_projects_with_7zip(project_paths, only_when_changed=(action == "update_zips"))

            if action == "repomix":
                target = Path(str(payload.get("targetDir") or base_dir)).expanduser()
                subfolders = bool(payload.get("subfolders"))
                output_dir.mkdir(parents=True, exist_ok=True)
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                targets = [p for p in sorted(target.iterdir(), key=lambda p: p.name.lower()) if p.is_dir() and p.name not in {".git", ".project-managment"}] if subfolders else [target]
                logs: list[str] = []
                ok = True
                repomix = shutil.which("repomix") or "repomix"
                for item in targets:
                    output_file = output_dir / f"{item.name}-{timestamp}.xml"
                    command = [repomix, "-c", str(config_path), "-o", str(output_file)]
                    result = run_command(command, item)
                    ok = ok and bool(result["ok"])
                    logs.append(f"# {item.name}\nCommand: {result['command']}\nExit: {result['exitCode']}\nOutput: {output_file}\n{result.get('stdout','')}{result.get('stderr','')}")
                return {"ok": ok, "stdout": "\n\n".join(logs), "outputDir": str(output_dir)}

            return {"ok": False, "error": f"Unknown warehouse action: {action}"}
        except Exception as exc:  # noqa: BLE001 - UI-facing local automation errors.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def get_mcp_hub_state(self) -> dict[str, Any]:
        """Return MCP Hub process/server state for the UI tab."""
        return mcp_hub_service.state()

    def start_mcp_hub(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        port_value = payload.get("port") if isinstance(payload, dict) else None
        try:
            port = int(port_value) if port_value else None
        except (TypeError, ValueError):
            port = None
        return mcp_hub_service.start(port=port)

    def stop_mcp_hub(self) -> dict[str, Any]:
        return mcp_hub_service.stop()

    def restart_mcp_hub(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        port_value = payload.get("port") if isinstance(payload, dict) else None
        try:
            port = int(port_value) if port_value else None
        except (TypeError, ValueError):
            port = None
        return mcp_hub_service.restart(port=port)

    def reload_mcp_hub_config(self) -> dict[str, Any]:
        return mcp_hub_service.refresh()

    def open_mcp_config(self) -> dict[str, Any]:
        return mcp_hub_service.open_config()

    def start_mcp_ngrok(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        port_value = payload.get("port") if isinstance(payload, dict) else None
        try:
            port = int(port_value) if port_value else None
        except (TypeError, ValueError):
            port = None
        return mcp_hub_service.start_ngrok(port=port)

    def stop_mcp_ngrok(self) -> dict[str, Any]:
        return mcp_hub_service.stop_ngrok()

    def add_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        return mcp_hub_service.add_server(payload or {})

    def start_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return {**mcp_hub_service.state(), "ok": False, "error": "No server name supplied."}
        return mcp_hub_service.start_server(name)

    def stop_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str((payload or {}).get("name") or "").strip()
        disable = bool((payload or {}).get("disable"))
        if not name:
            return {**mcp_hub_service.state(), "ok": False, "error": "No server name supplied."}
        return mcp_hub_service.stop_server(name, disable=disable)

    def get_terminal_state(self) -> dict[str, Any]:
        """Return terminal launcher defaults and detected executable paths."""
        alacritty_path = _resolve_alacritty_path()
        git_bash_path = _resolve_executable("bash.exe", r"C:\Program Files\Git\bin\bash.exe")
        return {
            "alacritty": {
                "path": str(alacritty_path or "alacritty.exe"),
                "exists": alacritty_path is not None,
                "supportsWindowsEmbed": False,
            },
            "shells": {
                "powershell": _resolve_executable("pwsh.exe", _resolve_executable("powershell.exe")),
                "cmd": _resolve_executable("cmd.exe"),
                "gitBash": git_bash_path,
            },
            "defaultWorkingDirectory": str(PROJECT_ROOT),
            "presets": [
                {"id": "ngrok", "name": "ngrok", "shell": "powershell", "command": "ngrok", "args": "http 8000 --host-header=rewrite"},
            ],
        }

    def launch_terminal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Launch an external Alacritty terminal with a selected shell/command."""
        alacritty_path = _resolve_alacritty_path()
        if not alacritty_path:
            return {"ok": False, "error": "Alacritty not found in PATH or common install locations."}

        title = str(payload.get("title") or "WebView Terminal").strip() or "WebView Terminal"
        shell = str(payload.get("shell") or "powershell").lower()
        command = str(payload.get("command") or "").strip()
        args_text = str(payload.get("argsText") or "").strip()
        if command and args_text:
            command = f"{command} {args_text}"
        cwd_raw = str(payload.get("cwd") or PROJECT_ROOT)
        cwd = Path(cwd_raw).expanduser()
        if not cwd.exists() or not cwd.is_dir():
            cwd = PROJECT_ROOT

        if shell == "cmd":
            shell_args = [_resolve_executable("cmd.exe"), "/k"]
            if command:
                shell_args.append(command)
        elif shell in {"git-bash", "bash"}:
            bash_exe = _resolve_executable("bash.exe", r"C:\Program Files\Git\bin\bash.exe")
            shell_args = [bash_exe, "-lc", command or "exec bash -i"]
        else:
            powershell_exe = _resolve_executable("pwsh.exe", _resolve_executable("powershell.exe"))
            shell_args = [
                powershell_exe,
                "-NoExit",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
            ]
            if command:
                shell_args += ["-Command", command]

        args = [
            "--title", title,
            "--working-directory", str(cwd),
            "--command",
            *shell_args,
        ]
        try:
            process = subprocess.Popen([str(alacritty_path), *args], cwd=str(cwd))
        except Exception as exc:  # noqa: BLE001 - surfaced to UI.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "alacrittyArgs": args}
        return {
            "ok": True,
            "pid": process.pid,
            "title": title,
            "cwd": str(cwd),
            "shell": shell,
            "command": command,
            "alacrittyArgs": args,
        }

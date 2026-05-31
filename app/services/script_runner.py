from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.config import CONFIG
from app.core.paths import SCRIPT_WORKSPACE, ensure_runtime_dirs
from app.services.script_detector import LANGUAGE_EXTENSIONS, LANGUAGE_LABELS, ScriptLanguage


@dataclass(frozen=True)
class RunnerRequest:
    content: str
    language: ScriptLanguage
    args_text: str = ""
    working_directory: str = ""
    timeout_seconds: int = CONFIG.default_timeout_seconds
    env_text: str = ""


@dataclass(frozen=True)
class RunnerResult:
    language: str
    script_path: str
    command: list[str]
    cwd: str
    started_at: float
    finished_at: float
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    opened_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def output(self) -> str:
        parts: list[str] = []
        if self.notes:
            parts.append("# Notes\n" + "\n".join(f"- {note}" for note in self.notes))
        if self.stdout:
            parts.append("# stdout\n" + self.stdout.rstrip())
        if self.stderr:
            parts.append("# stderr\n" + self.stderr.rstrip())
        if not parts:
            parts.append("No output.")
        return "\n\n".join(parts)


def parse_arguments(args_text: str) -> list[str]:
    """Parse a single argument line into argv-style tokens."""
    if not args_text.strip():
        return []
    # posix=False behaves more naturally for Windows paths like C:\Users\paul.
    try:
        return shlex.split(args_text, posix=platform.system() != "Windows")
    except ValueError:
        return shlex.split(args_text, posix=False)


def parse_environment(env_text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines into process environment additions."""
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def write_script(content: str, language: ScriptLanguage) -> Path:
    ensure_runtime_dirs()
    extension = LANGUAGE_EXTENSIONS.get(language, ".txt")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    fd, raw_path = tempfile.mkstemp(
        prefix=f"clipboard-{timestamp}-",
        suffix=extension,
        dir=SCRIPT_WORKSPACE,
        text=True,
    )
    path = Path(raw_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        if not content.endswith("\n"):
            handle.write("\n")
    return path


def _powershell_executable() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def _bash_command(script_path: Path, args: list[str]) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    bash = shutil.which("bash")
    if bash:
        return [bash, str(script_path), *args], notes

    if platform.system() == "Windows" and shutil.which("wsl.exe"):
        notes.append("No bash.exe in PATH; using wsl.exe bash. Windows paths are converted inside WSL.")
        quoted_path = str(script_path).replace("'", "'\\''")
        arg_part = " ".join(shlex.quote(arg) for arg in args)
        return ["wsl.exe", "bash", "-lc", f"bash $(wslpath -a '{quoted_path}') {arg_part}"], notes

    raise FileNotFoundError("Bash was not found. Install Git for Windows, WSL, or another bash provider.")


def build_command(language: ScriptLanguage, script_path: Path, args: list[str]) -> tuple[list[str], list[str]]:
    """Return a subprocess command and optional notes for the selected language."""
    notes: list[str] = []

    if language == ScriptLanguage.PYTHON:
        return [sys.executable, str(script_path), *args], notes

    if language == ScriptLanguage.JAVASCRIPT:
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node.js was not found in PATH. Install Node.js to run JavaScript scripts.")
        return [node, str(script_path), *args], notes

    if language == ScriptLanguage.POWERSHELL:
        return [_powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args], notes

    if language == ScriptLanguage.BATCH:
        return ["cmd.exe", "/d", "/c", str(script_path), *args], notes

    if language == ScriptLanguage.BASH:
        return _bash_command(script_path, args)

    if language == ScriptLanguage.HTML:
        return [], ["HTML files are opened in the default browser instead of executed as a process."]

    raise ValueError(f"Cannot run language '{language}'. Choose a supported script language first.")


def run_script(request: RunnerRequest) -> RunnerResult:
    """Run a script request synchronously for the pywebview API layer."""
    started_at = time.time()
    notes: list[str] = []
    script_path = write_script(request.content, request.language)
    cwd = Path(request.working_directory).expanduser() if request.working_directory.strip() else SCRIPT_WORKSPACE
    cwd.mkdir(parents=True, exist_ok=True)

    if request.language == ScriptLanguage.HTML:
        url = script_path.as_uri()
        webbrowser.open(url)
        finished_at = time.time()
        return RunnerResult(
            language=LANGUAGE_LABELS[request.language],
            script_path=str(script_path),
            command=["open", url],
            cwd=str(cwd),
            started_at=started_at,
            finished_at=finished_at,
            exit_code=0,
            stdout=f"Opened {url}",
            stderr="",
            opened_path=url,
            notes=["HTML clipboard content was saved and opened in the default browser."],
        )

    args = parse_arguments(request.args_text)
    command, command_notes = build_command(request.language, script_path, args)
    notes.extend(command_notes)

    env = os.environ.copy()
    env.update(parse_environment(request.env_text))

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(request.timeout_seconds)),
            shell=False,
        )
        finished_at = time.time()
        return RunnerResult(
            language=LANGUAGE_LABELS[request.language],
            script_path=str(script_path),
            command=command,
            cwd=str(cwd),
            started_at=started_at,
            finished_at=finished_at,
            exit_code=completed.returncode,
            stdout=completed.stdout[-CONFIG.max_output_chars :],
            stderr=completed.stderr[-CONFIG.max_output_chars :],
            notes=notes,
        )
    except subprocess.TimeoutExpired as exc:
        finished_at = time.time()
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        notes.append(f"Process timed out after {request.timeout_seconds} seconds.")
        return RunnerResult(
            language=LANGUAGE_LABELS[request.language],
            script_path=str(script_path),
            command=command,
            cwd=str(cwd),
            started_at=started_at,
            finished_at=finished_at,
            exit_code=-1,
            stdout=stdout[-CONFIG.max_output_chars :],
            stderr=stderr[-CONFIG.max_output_chars :],
            timed_out=True,
            notes=notes,
        )
    except Exception as exc:  # noqa: BLE001 - surfacing local runner errors to the UI is intended here.
        finished_at = time.time()
        return RunnerResult(
            language=LANGUAGE_LABELS.get(request.language, str(request.language)),
            script_path=str(script_path),
            command=command if "command" in locals() else [],
            cwd=str(cwd),
            started_at=started_at,
            finished_at=finished_at,
            exit_code=-2,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            notes=notes,
        )


def result_to_dict(result: RunnerResult) -> dict[str, object]:
    data = asdict(result)
    data["duration_seconds"] = round(result.duration_seconds, 3)
    return data

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT, SCRIPT_WORKSPACE


@dataclass(frozen=True)
class RuntimeInfo:
    name: str
    command: str
    status: str


def detect_runtime_info() -> list[RuntimeInfo]:
    candidates = [
        ("Python", sys.executable),
        ("Node.js", "node"),
        ("PowerShell", "pwsh" if shutil.which("pwsh") else "powershell"),
        ("CMD", "cmd.exe"),
        ("Bash", "bash"),
        ("WSL", "wsl.exe"),
    ]
    info: list[RuntimeInfo] = []
    for name, command in candidates:
        resolved = shutil.which(command) if command != sys.executable else command
        info.append(RuntimeInfo(name=name, command=command, status=resolved or "not found"))
    return info


def system_summary() -> dict[str, str]:
    return {
        "OS": f"{platform.system()} {platform.release()} ({platform.version()})",
        "Python": sys.version.split()[0],
        "Project root": str(PROJECT_ROOT),
        "Script workspace": str(SCRIPT_WORKSPACE),
    }

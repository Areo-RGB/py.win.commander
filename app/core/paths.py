from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    """Return True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """Resolve bundled resources in both source and PyInstaller modes."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def _runtime_root() -> Path:
    """Store writable runtime files outside the PyInstaller temp bundle."""
    if _is_frozen():
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "WindowsWebViewBackendScaffold"
    return PROJECT_ROOT


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = _resource_root()
APP_DATA_ROOT = _runtime_root()

RUNTIME_DIR = APP_DATA_ROOT / "runtime"
SCRIPT_WORKSPACE = RUNTIME_DIR / "scripts"
HISTORY_DIR = RUNTIME_DIR / "history"
HISTORY_FILE = HISTORY_DIR / "script_runs.jsonl"

WEB_DIR = RESOURCE_ROOT / "app" / "web"
INDEX_HTML = WEB_DIR / "index.html"
ASSETS_DIR = RESOURCE_ROOT / "app" / "assets"
ICON_FILE = ASSETS_DIR / "app.ico"


def ensure_runtime_dirs() -> None:
    """Create runtime folders used by the script runner."""
    SCRIPT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

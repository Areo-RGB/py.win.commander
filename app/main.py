from __future__ import annotations

import argparse
import ctypes
import sys

import webview

from app.api import BackendApi
from app.core.config import CONFIG
from app.core.paths import ICON_FILE, INDEX_HTML, ensure_runtime_dirs


APP_USER_MODEL_ID = "AreoRGB.WindowsWebViewBackendScaffold"


def set_windows_app_user_model_id() -> None:
    """Make the packaged app group correctly on the Windows taskbar."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        # Non-fatal: older shells or restricted hosts can ignore this.
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Windows WebView backend scaffold.")
    parser.add_argument("--debug", action="store_true", help="Enable pywebview debug mode/devtools.")
    parser.add_argument("--width", type=int, default=1920, help="Initial window width.")
    parser.add_argument("--height", type=int, default=1080, help="Initial window height.")
    parser.add_argument(
        "--no-maximize",
        action="store_true",
        help="Open at the requested width/height instead of maximizing the normal Windows window.",
    )
    return parser.parse_args()


def main() -> None:
    ensure_runtime_dirs()
    set_windows_app_user_model_id()

    args = parse_args()
    if not INDEX_HTML.exists():
        raise FileNotFoundError(f"Frontend not found: {INDEX_HTML}")
    if not ICON_FILE.exists():
        raise FileNotFoundError(f"Icon not found: {ICON_FILE}")

    api = BackendApi()
    html = INDEX_HTML.read_text(encoding="utf-8")
    window = webview.create_window(
        title=CONFIG.title,
        html=html,
        js_api=api,
        width=args.width,
        height=args.height,
        min_size=(940, 620),
        text_select=True,
        background_color="#f3f3f3",
        resizable=True,
        frameless=False,
    )

    def maximize_normal_window() -> None:
        # Keep the native Windows frame/buttons visible; do not use fullscreen or frameless mode.
        if args.no_maximize:
            return
        try:
            window.maximize()
        except Exception:
            pass

    webview.start(maximize_normal_window, debug=args.debug, icon=str(ICON_FILE))


if __name__ in {"__main__", "__mp_main__"}:
    main()

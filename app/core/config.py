from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    title: str = "Windows WebView Backend Scaffold"
    subtitle: str = "pywebview control center for local automation"
    default_host: str = "127.0.0.1"
    default_port: int = 8080
    default_timeout_seconds: int = 120
    max_output_chars: int = 120_000


CONFIG = AppConfig()

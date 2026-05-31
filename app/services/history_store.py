from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.paths import HISTORY_FILE, ensure_runtime_dirs


def append_history(record: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(limit: int = 30) -> list[dict[str, Any]]:
    path = Path(HISTORY_FILE)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records

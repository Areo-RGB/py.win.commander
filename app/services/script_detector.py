from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ScriptLanguage(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    POWERSHELL = "powershell"
    BATCH = "batch"
    BASH = "bash"
    HTML = "html"
    TEXT = "text"


@dataclass(frozen=True)
class DetectionResult:
    language: ScriptLanguage
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ArgumentHint:
    name: str
    source: str
    example: str = ""


LANGUAGE_LABELS: dict[ScriptLanguage, str] = {
    ScriptLanguage.PYTHON: "Python",
    ScriptLanguage.JAVASCRIPT: "JavaScript / Node.js",
    ScriptLanguage.POWERSHELL: "PowerShell",
    ScriptLanguage.BATCH: "Batch / CMD",
    ScriptLanguage.BASH: "Bash / WSL",
    ScriptLanguage.HTML: "HTML file",
    ScriptLanguage.TEXT: "Plain text",
}

LANGUAGE_EXTENSIONS: dict[ScriptLanguage, str] = {
    ScriptLanguage.PYTHON: ".py",
    ScriptLanguage.JAVASCRIPT: ".js",
    ScriptLanguage.POWERSHELL: ".ps1",
    ScriptLanguage.BATCH: ".cmd",
    ScriptLanguage.BASH: ".sh",
    ScriptLanguage.HTML: ".html",
    ScriptLanguage.TEXT: ".txt",
}


def detect_script_language(content: str) -> DetectionResult:
    """Detect a script language from clipboard content using transparent heuristics."""
    text = content.strip()
    lowered = text.lower()
    scores: dict[ScriptLanguage, float] = {language: 0.0 for language in ScriptLanguage}
    reasons: dict[ScriptLanguage, list[str]] = {language: [] for language in ScriptLanguage}

    def add(language: ScriptLanguage, amount: float, reason: str) -> None:
        scores[language] += amount
        reasons[language].append(reason)

    if not text:
        return DetectionResult(ScriptLanguage.TEXT, 0.0, ("clipboard is empty",))

    first_line = text.splitlines()[0].strip()
    if first_line.startswith("#!"):
        if "python" in first_line:
            add(ScriptLanguage.PYTHON, 4.0, "python shebang")
        elif "node" in first_line or "deno" in first_line:
            add(ScriptLanguage.JAVASCRIPT, 4.0, "node/deno shebang")
        elif "bash" in first_line or "sh" in first_line:
            add(ScriptLanguage.BASH, 4.0, "shell shebang")
        elif "pwsh" in first_line or "powershell" in first_line:
            add(ScriptLanguage.POWERSHELL, 4.0, "PowerShell shebang")

    python_patterns = [
        (r"\bdef\s+\w+\s*\(", "function definition"),
        (r"\bclass\s+\w+\s*[:(]", "class definition"),
        (r"\bfrom\s+[\w.]+\s+import\b", "from-import statement"),
        (r"\bimport\s+[\w.]+", "import statement"),
        (r"if\s+__name__\s*==\s*['\"]__main__['\"]", "Python main guard"),
        (r"argparse\.ArgumentParser", "argparse usage"),
        (r"print\s*\(", "print function"),
    ]
    for pattern, reason in python_patterns:
        if re.search(pattern, text):
            add(ScriptLanguage.PYTHON, 1.25, reason)

    js_patterns = [
        (r"\bconsole\.log\s*\(", "console.log usage"),
        (r"\brequire\s*\(", "CommonJS require"),
        (r"\bimport\s+.*\s+from\s+['\"]", "ES module import"),
        (r"\bexport\s+(default\s+)?", "ES module export"),
        (r"process\.argv", "Node process.argv usage"),
        (r"=>\s*[{(]", "arrow function"),
        (r"\bconst\s+\w+\s*=", "const declaration"),
        (r"\blet\s+\w+\s*=", "let declaration"),
    ]
    for pattern, reason in js_patterns:
        if re.search(pattern, text):
            add(ScriptLanguage.JAVASCRIPT, 1.1, reason)

    powershell_patterns = [
        (r"(?im)^\s*param\s*\(", "PowerShell param block"),
        (r"\bWrite-Host\b", "Write-Host cmdlet"),
        (r"\b(Get|Set|New|Remove|Start|Stop|Test|Join|Split)-[A-Za-z]+\b", "PowerShell verb-noun cmdlet"),
        (r"\$env:", "PowerShell env variable"),
        (r"\$PSScriptRoot", "PowerShell script root"),
    ]
    for pattern, reason in powershell_patterns:
        if re.search(pattern, text):
            add(ScriptLanguage.POWERSHELL, 1.4, reason)

    batch_patterns = [
        (r"(?im)^\s*@echo\s+off\b", "@echo off"),
        (r"(?im)^\s*setlocal\b", "setlocal"),
        (r"%[A-Za-z_][A-Za-z0-9_]*%", "CMD percent variable"),
        (r"(?im)^\s*(rem|goto|call)\b", "batch control command"),
    ]
    for pattern, reason in batch_patterns:
        if re.search(pattern, text):
            add(ScriptLanguage.BATCH, 1.4, reason)

    bash_patterns = [
        (r"(?im)^\s*set\s+-e", "set -e"),
        (r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "shell variable"),
        (r"(?im)^\s*(for|while)\b.*\bdo\b", "shell loop"),
        (r"\bchmod\s+\+x\b", "chmod command"),
        (r"\bapt(-get)?\s+install\b", "apt install command"),
    ]
    for pattern, reason in bash_patterns:
        if re.search(pattern, text):
            add(ScriptLanguage.BASH, 1.0, reason)

    if re.search(r"(?is)<\s*html\b|<\s*script\b|<!doctype html", text):
        add(ScriptLanguage.HTML, 3.0, "HTML/script tag")

    # Common disambiguation: Python imports should outrank shell variable noise.
    if scores[ScriptLanguage.PYTHON] >= 2.0 and scores[ScriptLanguage.BASH] <= 1.0:
        scores[ScriptLanguage.PYTHON] += 0.5

    best = max(scores, key=scores.get)
    best_score = scores[best]
    if best_score <= 0.1:
        return DetectionResult(ScriptLanguage.TEXT, 0.1, ("no strong script markers found",))

    confidence = min(0.99, best_score / 5.0)
    return DetectionResult(best, round(confidence, 2), tuple(reasons[best]))


def extract_argument_hints(content: str, language: ScriptLanguage) -> list[ArgumentHint]:
    """Extract a small set of argument hints for the GUI helper area."""
    hints: list[ArgumentHint] = []

    if language == ScriptLanguage.PYTHON:
        for match in re.finditer(r"add_argument\s*\(\s*['\"](?P<name>-{1,2}[\w-]+)['\"]", content):
            name = match.group("name")
            example = f"{name} value" if name.startswith("--") else name
            hints.append(ArgumentHint(name=name, source="argparse", example=example))

    elif language == ScriptLanguage.JAVASCRIPT:
        if "process.argv" in content:
            hints.append(ArgumentHint(name="process.argv", source="Node.js", example="--name Paul"))
        for match in re.finditer(r"(?:minimist|yargs)\([^)]*\).*?[\"'](?P<name>[\w-]+)[\"']", content, re.S):
            hints.append(ArgumentHint(name=f"--{match.group('name')}", source="JS args", example=f"--{match.group('name')} value"))

    elif language == ScriptLanguage.POWERSHELL:
        param_block = re.search(r"(?is)param\s*\((?P<body>.*?)\)", content)
        if param_block:
            for match in re.finditer(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)", param_block.group("body")):
                name = f"-{match.group('name')}"
                hints.append(ArgumentHint(name=name, source="param block", example=f"{name} value"))

    elif language == ScriptLanguage.BASH:
        for match in re.finditer(r"\$\{?(?P<num>[1-9][0-9]*)\}?", content):
            hints.append(ArgumentHint(name=f"${match.group('num')}", source="positional", example="value"))

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[ArgumentHint] = []
    for hint in hints:
        if hint.name not in seen:
            seen.add(hint.name)
            unique.append(hint)
    return unique[:12]

"""Secret detection for repository files."""

from __future__ import annotations

import os
import re
from pathlib import Path

EXCLUDED_DIRS = {
    ".venv", "venv", ".env",
    "node_modules",
    ".git",
    "__pycache__", ".pytest_cache",
    "build", "dist", "target",
    ".traces", ".checkpoints",
    "*.egg-info",
    ".tox", ".mypy_cache",
    "coverage", "htmlcov",
    ".idea", ".vscode",
    "migrations",
}

# Filename patterns that indicate sensitive content.
_SENSITIVE_NAMES: set[str] = {".env", "id_rsa", "id_ed25519"}
_SENSITIVE_EXTENSIONS: set[str] = {".pem", ".key"}
_SENSITIVE_SUBSTRINGS: list[str] = ["credentials", "secret", "token"]

# Content regex patterns (compiled once).
_AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN\s.*\sPRIVATE\sKEY-----")
_GENERIC_SECRET_RE = re.compile(
    r"""(?:key|secret|password|token)\s*=\s*['"][^'"]{21,}['"]""",
    re.IGNORECASE,
)


def scan_for_secrets(file_path: str, content: str) -> list[str]:
    """Return a list of reason strings if *file_path* / *content* look sensitive.

    The content itself is never included in the returned reasons.
    """
    reasons: list[str] = []
    name = os.path.basename(file_path)
    _, ext = os.path.splitext(name)

    # ── filename checks ──────────────────────────────────────────────
    if name in _SENSITIVE_NAMES:
        reasons.append(f"Sensitive filename: {name}")
    if ext in _SENSITIVE_EXTENSIONS:
        reasons.append(f"Sensitive file extension: {ext}")
    for sub in _SENSITIVE_SUBSTRINGS:
        if sub in name.lower():
            reasons.append(f"Filename contains '{sub}'")
            break  # one match is enough

    # ── content checks ───────────────────────────────────────────────
    if _AWS_KEY_RE.search(content):
        reasons.append("AWS access key pattern detected")
    if _PRIVATE_KEY_RE.search(content):
        reasons.append("Private key header detected")
    if _GENERIC_SECRET_RE.search(content):
        reasons.append("High-entropy secret assignment detected")

    return reasons


def filter_repo_files(repo_path: str) -> tuple[list[str], list[str]]:
    """Walk *repo_path*, classify each file as safe or excluded.

    Returns ``(safe_files, excluded_files)`` where paths are relative to
    *repo_path* using forward-slash separators.
    """
    safe_files: list[str] = []
    excluded_files: list[str] = []
    root = Path(repo_path)

    for current_root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if d not in EXCLUDED_DIRS
            and not d.endswith(".egg-info")
        ]

        for filename in files:
            file_path = Path(current_root) / filename
            try:
                rel = file_path.relative_to(root).as_posix()
            except ValueError:
                continue

            try:
                if os.path.getsize(file_path) > 1_000_000:
                    safe_files.append(rel)
                    continue
            except OSError:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                excluded_files.append(rel)
                continue

            if scan_for_secrets(rel, content):
                excluded_files.append(rel)
            else:
                safe_files.append(rel)

    return safe_files, excluded_files

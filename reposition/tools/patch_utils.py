"""Patch and diff utility functions."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile


def is_unified_diff(content: str) -> bool:
    """Return *True* if *content* looks like a unified diff."""
    return content.startswith("--- ") or content.startswith("diff --git")


def count_file_lines(content: str) -> int:
    """Return the number of lines in *content*."""
    return content.count("\n") + 1 if content else 0


def validate_diff_syntax(diff_content: str) -> tuple[bool, str]:
    """Dry-run a unified diff through ``patch`` to check syntax.

    Returns ``(True, "")`` on success, ``(False, error_output)`` on failure.
    """
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".diff")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(diff_content)

        result = subprocess.run(
            ["patch", "--dry-run", "-p1", f"--input={tmp_path}", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return False, str(exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


_TARGET_RE = re.compile(r"^\+\+\+\s+(?:b/)?(.+)$", re.MULTILINE)


def extract_modified_files(diff_content: str) -> list[str]:
    """Parse ``+++ b/path`` lines from a unified diff and return the file paths."""
    return [m.strip() for m in _TARGET_RE.findall(diff_content)]

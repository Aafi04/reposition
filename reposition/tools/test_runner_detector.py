"""Auto-detect the test runner for a repository."""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path


def detect_test_runner(repo_path: str) -> str | None:
    """Return the test command for *repo_path*, or *None* if undetectable.

    Detection order mirrors the most common conventions:
    npm/yarn → pytest → make → go → cargo.
    """
    root = Path(repo_path)

    # 1. package.json with a "test" script
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict) and "test" in scripts:
                return f"npm test"
        except (json.JSONDecodeError, OSError):
            pass

    # 2. pyproject.toml with [tool.pytest.ini_options]
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
            if "[tool.pytest.ini_options]" in text:
                return "pytest"
        except OSError:
            pass

    # 3. setup.cfg with [tool:pytest]
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        try:
            cp = ConfigParser()
            cp.read(str(setup_cfg), encoding="utf-8")
            if cp.has_section("tool:pytest"):
                return "pytest"
        except OSError:
            pass

    # 4. Makefile with a "test:" target
    makefile = root / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("test:") or stripped.startswith("test "):
                    return "make test"
        except OSError:
            pass

    # 5. go.mod → go test
    if (root / "go.mod").is_file():
        return "go test ./..."

    # 6. Cargo.toml → cargo test
    if (root / "Cargo.toml").is_file():
        return "cargo test"

    return None

"""Package-local CLI shim for console-script entry points.

This module exposes a stable `cli` object importable as `reposition.cli:cli`
so platform launchers (especially Windows `.exe` shims) can always resolve it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_root_main_module() -> ModuleType:
    root_main = Path(__file__).resolve().parent.parent / "main.py"
    if not root_main.exists():
        raise RuntimeError(f"Unable to locate CLI module at: {root_main}")

    spec = importlib.util.spec_from_file_location("reposition_root_main", root_main)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load CLI module specification for main.py")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root_main = _load_root_main_module()
cli: Any = getattr(_root_main, "cli")

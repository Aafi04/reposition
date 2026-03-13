"""Scanner agent – builds the repository manifest and creates the sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from reposition.config import get_config
from reposition.observability.tracer import RunTracer
from reposition.sandbox import E2BSandboxManager
from reposition.state import RepositionState
from reposition.tools.ast_parser import extract_top_level_declarations
from reposition.tools.secret_scanner import EXCLUDED_DIRS, filter_repo_files
from reposition.tools.test_runner_detector import detect_test_runner

# In-process task registry keyed by run_id. Tasks are consumed by planner.
_sandbox_tasks: dict[str, asyncio.Task] = {}

_ENTRY_POINT_RE = re.compile(
    r"(^|/)((main|index|app|server)\.[^/]+)$", re.IGNORECASE
)

_DEPENDENCY_FILES: set[str] = {
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".so", ".dll", ".dylib",
    ".jpg", ".jpeg", ".png",
    ".gif", ".ico", ".svg",
    ".pdf", ".zip", ".tar",
    ".gz", ".whl", ".egg",
    ".lock", ".sum",
}


def _has_main(content: str, language: str) -> bool:
    """Rough heuristic: does the file contain a main() / if __name__ guard."""
    if language == "python":
        return '__name__' in content and '__main__' in content
    if language in ("go",):
        return "func main()" in content
    if language in ("javascript", "typescript"):
        return False  # rely on filename pattern
    return False


def _extract_imports(content: str, language: str) -> list[str]:
    """Extract a coarse set of imported module names for ranking heuristics."""
    imports: set[str] = set()
    if language == "python":
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith("import "):
                names = line[len("import "):].split(",")
                for name in names:
                    token = name.strip().split(" as ")[0].split(".")[0]
                    if token:
                        imports.add(token)
            elif line.startswith("from "):
                token = line[len("from "):].split(" import ")[0].strip().split(".")[0]
                if token:
                    imports.add(token)
    elif language in ("javascript", "typescript"):
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith("import "):
                if " from " in line:
                    module = line.split(" from ", 1)[1].strip().strip(";\"")
                    module = module.strip("'\"")
                    if module:
                        imports.add(module.split("/")[0])
            elif line.startswith("const ") or line.startswith("let ") or line.startswith("var "):
                if "require(" in line:
                    part = line.split("require(", 1)[1]
                    module = part.split(")", 1)[0].strip().strip("'\"")
                    if module:
                        imports.add(module.split("/")[0])
    return sorted(imports)


async def scanner_agent(state: RepositionState) -> dict:
    """LangGraph node: scan the repo and produce the manifest + sandbox."""
    cfg = get_config()
    repo_path = state["repo_path"]

    # ── 1. Secret scan ───────────────────────────────────────────────
    safe_files, excluded_files = filter_repo_files(repo_path)
    secrets_detected = len(excluded_files) > 0

    # ── 2. Test runner ───────────────────────────────────────────────
    test_runner = detect_test_runner(repo_path)

    # ── 3. Build file entries ────────────────────────────────────────
    root = Path(repo_path)
    file_entries: list[dict] = []
    entry_points: list[str] = []
    module_boundaries: dict[str, list[str]] = {}
    dependency_files: list[str] = []
    total_lines = 0

    for rel in safe_files:
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS or p.endswith(".egg-info") for p in parts):
            continue

        _, ext = os.path.splitext(rel)
        if ext.lower() in SKIP_EXTENSIONS:
            continue

        fp = root / rel
        dirname = str(Path(rel).parent) if str(Path(rel).parent) != "." else "/"
        module_boundaries.setdefault(dirname, []).append(rel)

        basename = os.path.basename(rel)
        if basename in _DEPENDENCY_FILES:
            dependency_files.append(rel)

        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        line_count = content.count("\n") + 1 if content else 0
        total_lines += line_count

        language = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".go": "go", ".rs": "rust",
        }.get(ext, "unknown")

        is_entry = bool(_ENTRY_POINT_RE.search(rel)) or _has_main(content, language)
        if is_entry:
            entry_points.append(rel)

        if line_count <= cfg.scanner.large_file_threshold_lines:
            ast_info = extract_top_level_declarations(rel, content)
            file_entries.append({
                "path": rel,
                "language": language,
                "line_count": line_count,
                "is_entry_point": is_entry,
                "declarations": ast_info["declarations"],
                "imports": _extract_imports(content, language),
                "full_content_available": True,
            })
        else:
            ast_info = extract_top_level_declarations(rel, content)
            file_entries.append({
                "path": rel,
                "language": language,
                "line_count": line_count,
                "is_entry_point": is_entry,
                "declarations": ast_info["declarations"],
                "imports": _extract_imports(content, language),
                "full_content_available": False,
            })

    # ── 4. Assemble manifest ─────────────────────────────────────────
    manifest: dict = {
        "files": file_entries,
        "entry_points": entry_points,
        "module_boundaries": module_boundaries,
        "test_runner": test_runner,
        "dependency_files": dependency_files,
        "total_files": len(file_entries),
        "total_lines": total_lines,
    }

    # ── 5. Token estimate & compression ──────────────────────────────
    estimated_tokens = len(json.dumps(manifest)) // 4
    manifest_compressed: dict | None = None

    if estimated_tokens > cfg.scanner.max_manifest_tokens:
        manifest_compressed = {
            "entry_points": entry_points,
            "module_boundaries": module_boundaries,
            "test_runner": test_runner,
            "dependency_files": dependency_files,
            "total_files": len(file_entries),
            "total_lines": total_lines,
            "files": [
                {
                    "path": f["path"],
                    "language": f["language"],
                    "line_count": f["line_count"],
                    "is_entry_point": f["is_entry_point"],
                }
                for f in file_entries
            ],
        }

    # ── 6. Start sandbox pre-warm task (do not await) ───────────────
    sandbox_mgr = E2BSandboxManager()
    sandbox_task = asyncio.create_task(
        sandbox_mgr.create_sandbox(repo_path, excluded_files)
    )
    _sandbox_tasks[state["run_id"]] = sandbox_task

    # ── 7. Trace ─────────────────────────────────────────────────────
    tracer = RunTracer(state["run_id"], state["trace_path"])
    tracer.log(
        agent_name="scanner",
        decision="scan_complete",
        output={
            "total_files": len(file_entries),
            "total_lines": total_lines,
            "excluded_files_count": len(excluded_files),
            "test_runner": test_runner,
            "manifest_compressed": manifest_compressed is not None,
        },
    )

    return {
        "manifest": manifest,
        "manifest_compressed": manifest_compressed,
        "secrets_detected": secrets_detected,
        "excluded_files": excluded_files,
        "test_runner": test_runner,
        "e2b_sandbox_id": None,
    }

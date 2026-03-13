"""File ranking heuristics used by analyzer agents."""

from __future__ import annotations

import re

_SECURITY_NAME_TOKENS = (
    "auth",
    "login",
    "session",
    "token",
    "password",
    "user",
    "admin",
    "api",
    "route",
    "handler",
    "view",
)

_SECURITY_IMPORT_TOKENS = {
    "requests",
    "httpx",
    "flask",
    "django",
    "fastapi",
    "sqlalchemy",
    "pymongo",
    "psycopg2",
}

_TEST_NAME_RE = re.compile(r"(^|/)(test_|.*_test\.|.*spec\.|.*mock|.*fixture)", re.IGNORECASE)
_MIGRATION_RE = re.compile(r"migration", re.IGNORECASE)


def _files(manifest: dict) -> list[dict]:
    entries = manifest.get("files", []) if manifest else []
    return entries if isinstance(entries, list) else []


def _path(file_entry: dict) -> str:
    return str(file_entry.get("path", ""))


def _is_test_file(path: str) -> bool:
    lower = path.lower()
    return bool(_TEST_NAME_RE.search(lower))


def _decl_count(file_entry: dict) -> int:
    decls = file_entry.get("declarations", [])
    return len(decls) if isinstance(decls, list) else 0


def _imports(file_entry: dict) -> set[str]:
    imports = file_entry.get("imports", [])
    if not isinstance(imports, list):
        return set()
    return {str(name).lower() for name in imports}


def _sort_by_score(scored: list[tuple[int, int, str]]) -> list[str]:
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [path for _, _, path in scored]


def rank_files_for_security(manifest: dict) -> list[str]:
    """Return top file paths for security analysis, max 20."""
    scored: list[tuple[int, int, str]] = []
    for idx, file_entry in enumerate(_files(manifest)):
        path = _path(file_entry)
        if not path:
            continue
        lower = path.lower()
        score = 0

        if any(token in lower for token in _SECURITY_NAME_TOKENS):
            score += 3
        if bool(file_entry.get("is_entry_point", False)):
            score += 2
        if _SECURITY_IMPORT_TOKENS.intersection(_imports(file_entry)):
            score += 2
        if _decl_count(file_entry) > 5:
            score += 1
        if _is_test_file(path):
            score -= 1

        scored.append((score, idx, path))

    return _sort_by_score(scored)[:20]


def rank_files_for_refactor(manifest: dict) -> list[str]:
    """Return top file paths for refactor analysis, max 15."""
    scored: list[tuple[int, int, str]] = []
    for idx, file_entry in enumerate(_files(manifest)):
        path = _path(file_entry)
        if not path:
            continue
        lower = path.lower()
        line_count = int(file_entry.get("line_count", 0) or 0)
        score = 0

        if line_count > 300:
            score += 3
        elif line_count > 150:
            score += 2

        if _decl_count(file_entry) > 10:
            score += 2
        if not _is_test_file(path):
            score += 1
        if _is_test_file(path) or bool(_MIGRATION_RE.search(lower)):
            score -= 2

        scored.append((score, idx, path))

    return _sort_by_score(scored)[:15]


def _extract_paths_from_security_report(security_report: list) -> set[str]:
    paths: set[str] = set()
    for item in security_report or []:
        if isinstance(item, dict):
            file_path = item.get("file")
            if isinstance(file_path, str) and file_path:
                paths.add(file_path)
    return paths


def _extract_paths_from_refactor_report(refactor_report: list) -> set[str]:
    paths: set[str] = set()
    for item in refactor_report or []:
        if not isinstance(item, dict):
            continue
        files = item.get("files")
        if isinstance(files, list):
            for file_path in files:
                if isinstance(file_path, str) and file_path:
                    paths.add(file_path)
    return paths


def _has_matching_test(path: str, all_paths: set[str]) -> bool:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for candidate in all_paths:
        lower = candidate.lower()
        if not _is_test_file(candidate):
            continue
        if f"test_{stem}" in lower or f"{stem}_test" in lower:
            return True
    return False


def rank_files_for_coverage(
    manifest: dict,
    security_report: list,
    refactor_report: list,
) -> list[str]:
    """Return top file paths for coverage analysis, max 15."""
    files = _files(manifest)
    all_paths = {_path(entry) for entry in files if _path(entry)}

    security_paths = _extract_paths_from_security_report(security_report)
    refactor_paths = _extract_paths_from_refactor_report(refactor_report)

    scored: list[tuple[int, int, str]] = []
    for idx, file_entry in enumerate(files):
        path = _path(file_entry)
        if not path:
            continue

        score = 0
        if path in security_paths:
            score += 3
        if path in refactor_paths:
            score += 2
        if _decl_count(file_entry) > 5 and not _has_matching_test(path, all_paths):
            score += 2
        if bool(file_entry.get("is_entry_point", False)):
            score += 1
        if _is_test_file(path):
            score -= 3

        scored.append((score, idx, path))

    return _sort_by_score(scored)[:15]

"""Planner agent – synthesises analyzer reports into executable work packages."""

from __future__ import annotations

import json
import re

from reposition.config import get_config
from reposition.agents.scanner import _sandbox_tasks
from reposition.llm_client import call_llm, get_llm
from reposition.observability.tracer import RunTracer
from reposition.state import RepositionState

PLANNER_SYSTEM_PROMPT = """\
You are the orchestration engine for an automated code improvement system.
You receive three analysis reports and must synthesize them into precise,
executable work packages.

Output ONLY a valid JSON array of work packages. Each work package:
{
  "id": "wp-{sequential_number}",
  "priority": 1,
  "priority_label": "CRITICAL_SECURITY|HIGH_SECURITY|BUILD_RUNTIME|HIGH_TECH_DEBT|MISSING_TESTS",
  "files_to_modify": ["relative/path"],
  "issue_description": "One paragraph: precise description of the problem",
  "acceptance_criteria": ["List of verifiable pass/fail criteria"],
  "estimated_lines": 50,
  "source_issues": ["issue identifiers from input reports"]
}

HARD CONSTRAINTS:
1. Maximum 3 files per work package
2. Maximum 200 lines estimated per work package
3. Each file may appear in AT MOST ONE work package
4. No work package may introduce new external dependencies
5. No work package may change public API signatures
6. Priority ordering: CRITICAL_SECURITY > HIGH_SECURITY > BUILD_RUNTIME > HIGH_TECH_DEBT > MISSING_TESTS
7. Tie-breaking within a tier: prefer packages affecting authentication/auth code,
   then prefer packages with higher blast radius (more call sites affected),
   then prefer packages with lower estimated_lines
"""

_PRIORITY_ORDER: dict[str, int] = {
    "CRITICAL_SECURITY": 0,
    "HIGH_SECURITY": 1,
    "BUILD_RUNTIME": 2,
    "HIGH_TECH_DEBT": 3,
    "MISSING_TESTS": 4,
}

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _sort_key(pkg: dict) -> tuple[int, int]:
    """Return a sort key: (priority_tier, estimated_lines)."""
    label = pkg.get("priority_label", "MISSING_TESTS")
    return _PRIORITY_ORDER.get(label, 99), pkg.get("estimated_lines", 999)


def _deduplicate_file_locks(packages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Enforce one-file-per-package rule.

    Walk packages in priority order; for each file that already has a lock,
    remove it from the lower-priority package's ``files_to_modify`` list.
    If a package ends up with zero files, discard it entirely.
    """
    file_locks: dict[str, str] = {}
    kept: list[dict] = []

    for pkg in packages:
        surviving_files: list[str] = []
        for f in pkg.get("files_to_modify", []):
            if f not in file_locks:
                file_locks[f] = pkg["id"]
                surviving_files.append(f)
        if surviving_files:
            pkg["files_to_modify"] = surviving_files
            kept.append(pkg)

    return kept, file_locks


def _build_user_message(state: RepositionState) -> str:
    cfg = get_config()
    estimated = len(json.dumps(state["manifest"])) // 4 if state["manifest"] else 0
    manifest = (
        state["manifest_compressed"]
        if estimated > cfg.scanner.max_manifest_tokens and state["manifest_compressed"]
        else state["manifest"]
    )
    return (
        "=== SECURITY REPORT ===\n"
        + json.dumps(state["security_report"], indent=2)
        + "\n\n=== REFACTOR REPORT ===\n"
        + json.dumps(state["refactor_report"], indent=2)
        + "\n\n=== COVERAGE REPORT ===\n"
        + json.dumps(state["coverage_report"], indent=2)
        + "\n\n=== REPOSITORY MANIFEST ===\n"
        + json.dumps(manifest, indent=2)
        + "\n\nSynthesize the above reports into work packages."
    )


async def planner_agent(state: RepositionState) -> dict:
    """LangGraph node: produce prioritised work packages from analyzer reports."""
    cfg = get_config()
    tracer = RunTracer(state["run_id"], state["trace_path"])
    sandbox_id = state.get("e2b_sandbox_id")

    # 1 — verify analyzer completion synchronously before any await.
    analyzer_statuses = dict(state.get("analyzer_statuses", {}))
    required = {"security", "refactor", "coverage"}
    completed = {
        key
        for key, value in analyzer_statuses.items()
        if value in ("COMPLETE", "TIMED_OUT", "ERROR")
    }
    if not required.issubset(completed):
        missing = sorted(required - completed)
        tracer.log(
            agent_name="planner",
            decision="skipped_analyzer_incomplete",
            output={"missing": missing, "analyzer_statuses": analyzer_statuses},
        )
        return {
            "e2b_sandbox_id": sandbox_id,
        }

    # 2 — resolve scanner-started sandbox prewarm task if needed.
    task = _sandbox_tasks.pop(state["run_id"], None)
    if sandbox_id is None and task is not None:
        try:
            sandbox_id = await task
        except Exception as exc:
            tracer.log(
                agent_name="planner",
                decision="sandbox_prewarm_failed",
                output={"error": str(exc)},
            )
            sandbox_id = None

    # 3–5 — call LLM
    llm = get_llm("heavy", max_tokens=8192)
    user_message = _build_user_message(state)

    raw_text, token_usage = call_llm(llm, PLANNER_SYSTEM_PROMPT, user_message)

    cleaned = _strip_fences(raw_text)
    packages: list[dict] = json.loads(cleaned)

    # 6 — sort by priority then deduplicate file locks
    packages.sort(key=_sort_key)
    packages, file_locks = _deduplicate_file_locks(packages)

    # 7 — trim to max allowed
    packages = packages[: cfg.planner.max_work_packages_per_run]

    # 8 — re-index priorities sequentially
    for idx, pkg in enumerate(packages, start=1):
        pkg["priority"] = idx

    # 9 — trace
    tracer.log(
        agent_name="planner",
        decision="planning_complete",
        output={
            "work_packages_count": len(packages),
            "file_locks_count": len(file_locks),
        },
        token_usage=token_usage,
    )

    # 10 — return state update
    return {
        "work_packages": packages,
        "file_locks": file_locks,
        "current_package_index": 0,
        "e2b_sandbox_id": sandbox_id,
    }

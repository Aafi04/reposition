"""Single source of truth for all data flowing between Reposition agents."""

from __future__ import annotations

import uuid
from typing import Literal, TypedDict

ValidatorVerdict = Literal["PASS", "FAIL_COMPILE", "FAIL_TEST", "PASS_NO_TESTS"]
AnalyzerStatus = Literal["COMPLETE", "TIMED_OUT", "ERROR"]
PackageStatus = Literal["PASS", "FAIL_COMPILE", "FAIL_TEST", "ABORTED", "PENDING"]


class RepositionState(TypedDict):
    # Run metadata
    run_id: str
    repo_path: str
    thread_id: str
    e2b_sandbox_id: str | None
    dry_run: bool

    # Scanner outputs
    manifest: dict | None
    manifest_compressed: dict | None
    secrets_detected: bool
    excluded_files: list[str]
    test_runner: str | None

    # Analyzer outputs
    security_report: list[dict]
    refactor_report: list[dict]
    coverage_report: list[dict]
    analyzer_statuses: dict[str, str]

    # Planner outputs
    work_packages: list[dict]
    current_package_index: int
    file_locks: dict[str, str]

    # Coder/Validator cycle state
    current_patch: str | None
    retry_count: int
    package_results: list[dict]
    active_package_ids: list[str]
    pending_package_ids: list[str]

    # PR Agent outputs
    pr_url: str | None
    pr_number: int | None
    pr_branch: str

    # Observability
    trace_path: str


def make_initial_state(repo_path: str) -> RepositionState:
    """Return a RepositionState with all fields set to correct defaults and a fresh UUID4 run_id."""
    rid = str(uuid.uuid4())
    return RepositionState(
        run_id=rid,
        repo_path=repo_path,
        thread_id=rid,
        e2b_sandbox_id=None,
        dry_run=False,
        manifest=None,
        manifest_compressed=None,
        secrets_detected=False,
        excluded_files=[],
        test_runner=None,
        security_report=[],
        refactor_report=[],
        coverage_report=[],
        analyzer_statuses={},
        work_packages=[],
        current_package_index=0,
        file_locks={},
        current_patch=None,
        retry_count=0,
        package_results=[],
        active_package_ids=[],
        pending_package_ids=[],
        pr_url=None,
        pr_number=None,
        pr_branch="",
        trace_path="",
    )

"""Validator agent – applies patches and runs tests in the E2B sandbox."""

from __future__ import annotations

import hashlib
import json

from reposition.config import get_config
from reposition.observability.tracer import RunTracer
from reposition.sandbox import E2BSandboxManager
from reposition.state import RepositionState
from reposition.tools.patch_utils import is_unified_diff

_COMPILE_ERROR_KEYWORDS = [
    "SyntaxError",
    "error:",
    "cannot find symbol",
    "undefined reference",
    "ImportError",
    "ModuleNotFoundError",
]


async def validator_agent(state: RepositionState) -> dict:
    """LangGraph node: apply the current patch, run tests, and emit a verdict."""
    cfg = get_config()
    tracer = RunTracer(state["run_id"], state["trace_path"])
    sandbox = E2BSandboxManager()
    sandbox_id = state["e2b_sandbox_id"]
    assert sandbox_id is not None, "Sandbox must be initialised before validator runs"

    patch_map: dict[str, str] = json.loads(state["current_patch"] or "{}")

    # ── PHASE 1 — Apply Patch ────────────────────────────────────────
    for filename, content in patch_map.items():
        if is_unified_diff(content):
            # Write diff to a temp file inside the sandbox
            name_hash = hashlib.sha256(filename.encode()).hexdigest()[:12]
            diff_path = f"/tmp/patch_{name_hash}.diff"
            await sandbox.write_file(sandbox_id, diff_path, content)

            # Dry-run first
            dry_result = await sandbox.apply_patch(sandbox_id, content, dry_run=True)
            if not dry_result["success"]:
                # Immediate FAIL_COMPILE — do not proceed to tests
                work_package = state["work_packages"][state["current_package_index"]]
                package_result = {
                    "package_id": work_package["id"],
                    "status": "FAIL_COMPILE",
                    "verdict_detail": {
                        "stdout": dry_result["output"][-3000:],
                        "stderr": "",
                        "exit_code": 1,
                    },
                }
                tracer.log(
                    agent_name="validator",
                    decision="FAIL_COMPILE",
                    output={"package_id": work_package["id"]},
                )
                return {
                    "package_results": [*state["package_results"], package_result],
                    "current_patch": None,
                }

            # Apply for real
            await sandbox.apply_patch(sandbox_id, content, dry_run=False)
        else:
            # Full file content — write directly
            remote_path = f"/home/user/repo/{filename}"
            await sandbox.write_file(sandbox_id, remote_path, content)

    # ── PHASE 2 — Build Validation ───────────────────────────────────
    test_runner = state["test_runner"]
    pkg_id = state["work_packages"][state["current_package_index"]]["id"]

    await sandbox.run_command(sandbox_id, "cd /home/user/repo && git add -A")

    if test_runner is None:
        test_result = await sandbox.run_command(
            sandbox_id,
            f"cd /home/user/repo && git commit -m 'reposition: {pkg_id}' --allow-empty",
        )
    else:
        test_result = await sandbox.run_command(
            sandbox_id,
            f"cd /home/user/repo && {test_runner}",
            timeout=cfg.validator.test_timeout_seconds,
        )
        if test_result["exit_code"] == 0:
            await sandbox.run_command(
                sandbox_id,
                f"cd /home/user/repo && git commit -m 'reposition: {pkg_id}' --allow-empty",
            )

    # ── PHASE 3 — Parse Results and Emit Verdict ─────────────────────
    exit_code = test_result["exit_code"]
    stdout = test_result["stdout"]
    stderr = test_result["stderr"]
    combined = stdout + stderr

    if test_runner is None:
        verdict = "PASS_NO_TESTS"
    elif exit_code == 0:
        verdict = "PASS"
    elif any(kw in combined for kw in _COMPILE_ERROR_KEYWORDS):
        verdict = "FAIL_COMPILE"
    else:
        verdict = "FAIL_TEST"

    work_package = state["work_packages"][state["current_package_index"]]
    package_result = {
        "package_id": work_package["id"],
        "status": verdict,
        "verdict_detail": {
            "stdout": stdout[-3000:],
            "stderr": stderr[-3000:],
            "exit_code": exit_code,
        },
    }

    tracer.log(
        agent_name="validator",
        decision=verdict,
        output={"package_id": work_package["id"]},
    )

    return {
        "package_results": [*state["package_results"], package_result],
        "current_patch": None,
    }

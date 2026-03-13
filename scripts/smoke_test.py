#!/usr/bin/env python3
"""Smoke test — real end-to-end dry-run against tests/fixtures/sample_repo."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# ── 1. Environment setup ────────────────────────────────────────────────

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on shell env

_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

provider = os.environ.get("REPOSITION_LLM_PROVIDER", "anthropic").lower()
api_key_var = _API_KEY_ENV.get(provider)

missing: list[str] = []
if api_key_var and not os.environ.get(api_key_var):
    missing.append(api_key_var)
if not os.environ.get("E2B_API_KEY"):
    missing.append("E2B_API_KEY")

if missing:
    print(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

# ── 2. Run the dry-run pipeline ─────────────────────────────────────────

FIXTURE = str(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_repo")


async def run_dry() -> dict:
    """Execute Scanner → Analyzers → Planner and return final state."""
    from reposition.state import make_initial_state
    from reposition.agents.scanner import scanner_agent
    from reposition.graph import run_analyzers_parallel
    from reposition.agents.planner import planner_agent
    from reposition.observability.tracer import RunTracer

    state = make_initial_state(FIXTURE)
    trace_dir = Path(".traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    state["trace_path"] = str(trace_dir / f"{state['run_id']}.jsonl")
    RunTracer(state["run_id"], state["trace_path"])

    print("Running scanner...")
    state.update(await scanner_agent(state))

    print("Running analyzers...")
    state.update(await run_analyzers_parallel(state))

    print("Running planner...")
    state.update(await planner_agent(state))

    return state


state = asyncio.run(run_dry())

# ── 3. Assertions ──────────────────────────────────────────────────────

checks: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, reason: str = "") -> None:
    checks.append((name, passed, reason))
    tag = "PASS" if passed else "FAIL"
    detail = f" — {reason}" if reason else ""
    print(f"  [{tag}] {name}{detail}")


print("\n=== Smoke test checks ===\n")

# 1. manifest
manifest = state.get("manifest") or {}
files = manifest.get("files", [])
check("manifest produced", len(files) >= 1, f"{len(files)} file(s)")

# 2. secrets_detected
check("secrets_detected is True", state.get("secrets_detected") is True)

# 3. security_report severity
sec = state.get("security_report", [])
has_high_sev = any(
    item.get("severity", "").upper() in ("CRITICAL", "HIGH") for item in sec
)
check(
    "security_report has CRITICAL/HIGH item",
    has_high_sev,
    f"{len(sec)} item(s)",
)

# 4. refactor_report
ref = state.get("refactor_report", [])
check("refactor_report has items", len(ref) >= 1, f"{len(ref)} item(s)")

# 5. coverage_report
cov = state.get("coverage_report", [])
check("coverage_report has items", len(cov) >= 1, f"{len(cov)} item(s)")

# 6. work_packages
wps = state.get("work_packages", [])
check("work_packages produced", len(wps) >= 1, f"{len(wps)} package(s)")

# 7. first work package priority
first_label = wps[0].get("priority_label", "") if wps else ""
check(
    "first package is security priority",
    first_label in ("CRITICAL_SECURITY", "HIGH_SECURITY"),
    f"got {first_label!r}",
)

# 8. max 3 files per package
over_3 = [wp["id"] for wp in wps if len(wp.get("files_to_modify", [])) > 3]
check("no package exceeds 3 files", len(over_3) == 0, f"violators: {over_3}" if over_3 else "")

# 9. no duplicate files across packages
all_files: list[str] = []
dupes: list[str] = []
for wp in wps:
    for f in wp.get("files_to_modify", []):
        if f in all_files:
            dupes.append(f)
        all_files.append(f)
check("no file in multiple packages", len(dupes) == 0, f"duplicates: {dupes}" if dupes else "")

# ── 4. Summary ──────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in checks if ok)
total = len(checks)
print(f"\nSmoke test: {passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)

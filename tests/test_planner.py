"""Tests for the planner agent — file-lock dedup, priority ordering, max packages."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reposition.agents.planner import (
    _deduplicate_file_locks,
    _sort_key,
    _strip_fences,
)


# ── Pure helpers ────────────────────────────────────────────────────────


class TestStripFences:
    def test_removes_json_fence(self):
        text = '```json\n[{"id": "wp-1"}]\n```'
        assert _strip_fences(text) == '[{"id": "wp-1"}]'

    def test_returns_plain_text_unchanged(self):
        text = '[{"id": "wp-1"}]'
        assert _strip_fences(text) == text


class TestSortKey:
    def test_critical_security_before_high_tech_debt(self):
        critical = {"priority_label": "CRITICAL_SECURITY", "estimated_lines": 100}
        tech_debt = {"priority_label": "HIGH_TECH_DEBT", "estimated_lines": 10}
        assert _sort_key(critical) < _sort_key(tech_debt)

    def test_same_tier_prefers_fewer_lines(self):
        small = {"priority_label": "HIGH_SECURITY", "estimated_lines": 20}
        large = {"priority_label": "HIGH_SECURITY", "estimated_lines": 80}
        assert _sort_key(small) < _sort_key(large)


# ── File-lock deduplication ─────────────────────────────────────────────


class TestDeduplicateFileLocks:
    def test_second_package_loses_conflicting_file(self):
        """When two packages both target auth.py, only the first keeps it."""
        packages = [
            {"id": "wp-1", "files_to_modify": ["app/auth.py", "app/utils.py"]},
            {"id": "wp-2", "files_to_modify": ["app/auth.py"]},
        ]
        kept, locks = _deduplicate_file_locks(packages)

        # wp-2 has no surviving files → dropped entirely
        assert len(kept) == 1
        assert kept[0]["id"] == "wp-1"
        assert locks["app/auth.py"] == "wp-1"

    def test_non_overlapping_packages_both_survive(self):
        packages = [
            {"id": "wp-1", "files_to_modify": ["app/a.py"]},
            {"id": "wp-2", "files_to_modify": ["app/b.py"]},
        ]
        kept, _ = _deduplicate_file_locks(packages)
        assert len(kept) == 2


# ── Full planner_agent with mocked LLM ────────────────────────────────


def _make_planner_state() -> dict:
    """Create a minimal state dict with fake analyzer reports."""
    from reposition.state import make_initial_state

    state = make_initial_state("/fake/repo")
    state["trace_path"] = "dummy.jsonl"
    state["manifest"] = {"files": [], "total_files": 0, "total_lines": 0}
    state["manifest_compressed"] = None
    state["analyzer_statuses"] = {
        "security": "COMPLETE",
        "refactor": "COMPLETE",
        "coverage": "COMPLETE",
    }
    state["security_report"] = [
        {"cwe_id": "CWE-89", "severity": "CRITICAL", "file": "app/main.py",
         "description": "SQL injection", "remediation": "Use parameterized queries"},
    ]
    state["refactor_report"] = [
        {"type": "duplication", "severity": "HIGH", "files": ["app/main.py"],
         "description": "Duplicated report function", "expected_improvement": "reduce duplication"},
    ]
    state["coverage_report"] = [
        {"uncovered_path": "app/auth.py:check_token", "file": "app/auth.py",
         "criticality": "HIGH", "suggested_test_description": "test check_token"},
    ]
    return state


def _fake_llm_packages(label_first: str, label_second: str) -> list[dict]:
    """Build two work packages with given priority labels."""
    return [
        {
            "id": "wp-1", "priority": 1, "priority_label": label_first,
            "files_to_modify": ["app/main.py"],
            "issue_description": "Fix SQL injection",
            "acceptance_criteria": ["No raw SQL"],
            "estimated_lines": 30, "source_issues": ["CWE-89"],
        },
        {
            "id": "wp-2", "priority": 2, "priority_label": label_second,
            "files_to_modify": ["app/auth.py"],
            "issue_description": "Remove hardcoded key",
            "acceptance_criteria": ["Key from env"],
            "estimated_lines": 15, "source_issues": ["hardcoded-key"],
        },
    ]


class TestPlannerAgent:
    @pytest.fixture(autouse=True)
    def _mock_tracer(self):
        with patch("reposition.agents.planner.RunTracer") as MockCls:
            instance = MockCls.return_value
            instance.log = lambda **kw: None
            yield instance

    @pytest.fixture(autouse=True)
    def _mock_get_llm(self):
        with patch("reposition.agents.planner.get_llm"):
            yield

    @pytest.mark.asyncio
    async def test_priority_ordering_critical_before_tech_debt(self):
        """CRITICAL_SECURITY packages must appear before HIGH_TECH_DEBT."""
        from reposition.agents.planner import planner_agent

        # Return them in wrong order; planner must fix it
        packages = _fake_llm_packages("HIGH_TECH_DEBT", "CRITICAL_SECURITY")

        state = _make_planner_state()
        with patch("reposition.agents.planner.call_llm") as mock_call:
            mock_call.return_value = (json.dumps(packages), {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
            result = await planner_agent(state)

        wps = result["work_packages"]
        labels = [wp["priority_label"] for wp in wps]
        assert labels.index("CRITICAL_SECURITY") < labels.index("HIGH_TECH_DEBT")

    @pytest.mark.asyncio
    async def test_file_lock_enforcement(self):
        """If two packages target the same file, only one keeps it."""
        from reposition.agents.planner import planner_agent

        packages = [
            {
                "id": "wp-1", "priority": 1, "priority_label": "CRITICAL_SECURITY",
                "files_to_modify": ["app/auth.py"],
                "issue_description": "a", "acceptance_criteria": ["x"],
                "estimated_lines": 10, "source_issues": ["s1"],
            },
            {
                "id": "wp-2", "priority": 2, "priority_label": "HIGH_TECH_DEBT",
                "files_to_modify": ["app/auth.py"],
                "issue_description": "b", "acceptance_criteria": ["y"],
                "estimated_lines": 10, "source_issues": ["s2"],
            },
        ]
        state = _make_planner_state()

        with patch("reposition.agents.planner.call_llm") as mock_call:
            mock_call.return_value = (json.dumps(packages), {})
            result = await planner_agent(state)

        wps = result["work_packages"]
        all_files = [f for wp in wps for f in wp["files_to_modify"]]
        # auth.py must appear at most once across all packages
        assert all_files.count("app/auth.py") == 1

    @pytest.mark.asyncio
    async def test_max_work_packages_limit(self):
        """Output should never exceed max_work_packages_per_run."""
        from reposition.agents.planner import planner_agent

        # Generate 15 packages — limit is 10
        packages = [
            {
                "id": f"wp-{i}", "priority": i,
                "priority_label": "MISSING_TESTS",
                "files_to_modify": [f"file_{i}.py"],
                "issue_description": f"issue {i}",
                "acceptance_criteria": [f"crit {i}"],
                "estimated_lines": 10, "source_issues": [f"s{i}"],
            }
            for i in range(1, 16)
        ]
        state = _make_planner_state()

        with patch("reposition.agents.planner.call_llm") as mock_call:
            mock_call.return_value = (json.dumps(packages), {})
            result = await planner_agent(state)

        from reposition.config import get_config
        limit = get_config().planner.max_work_packages_per_run
        assert len(result["work_packages"]) <= limit

    @pytest.mark.asyncio
    async def test_planner_proceeds_when_analyzer_status_is_error(self):
        """Planner should continue when an analyzer status is ERROR."""
        from reposition.agents.planner import planner_agent

        packages = _fake_llm_packages("CRITICAL_SECURITY", "MISSING_TESTS")
        state = _make_planner_state()
        state["analyzer_statuses"] = {
            "security": "COMPLETE",
            "refactor": "ERROR",
            "coverage": "TIMED_OUT",
        }

        with patch("reposition.agents.planner.call_llm") as mock_call:
            mock_call.return_value = (json.dumps(packages), {})
            result = await planner_agent(state)

        assert len(result["work_packages"]) == 2
        assert "file_locks" in result

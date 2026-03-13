"""Unit tests for package scheduling and scheduler routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from reposition.graph import package_scheduler_node, route_after_package_scheduler
from reposition.state import make_initial_state


class _NoopTracer:
    def __init__(self, *args, **kwargs):
        pass

    def log(self, **kwargs):
        return None


def _cfg(max_concurrent_packages: int) -> SimpleNamespace:
    return SimpleNamespace(
        planner=SimpleNamespace(max_concurrent_packages=max_concurrent_packages)
    )


def _make_state(work_packages: list[dict], *, active_ids: list[str] | None = None) -> dict:
    state = make_initial_state("/fake/repo")
    state["work_packages"] = work_packages
    state["package_results"] = []
    state["active_package_ids"] = active_ids or []
    state["pending_package_ids"] = []
    state["trace_path"] = "dummy-trace.jsonl"
    return state


@pytest.mark.asyncio
async def test_no_file_overlap_schedules_same_batch():
    work_packages = [
        {"id": "wp-1", "files_to_modify": ["a.py"]},
        {"id": "wp-2", "files_to_modify": ["b.py"]},
    ]
    state = _make_state(work_packages)

    async def _fake_batch(packages, _state):
        return [{"package_id": pkg["id"], "status": "PASS"} for pkg in packages]

    with patch("reposition.graph.get_config", return_value=_cfg(2)), patch(
        "reposition.graph.run_package_batch", side_effect=_fake_batch
    ), patch("reposition.graph.RunTracer", _NoopTracer):
        result = await package_scheduler_node(state)

    assert result["pending_package_ids"] == []
    assert [r["package_id"] for r in result["package_results"]] == ["wp-1", "wp-2"]


@pytest.mark.asyncio
async def test_file_overlap_defers_second_package_to_next_batch():
    work_packages = [
        {"id": "wp-1", "files_to_modify": ["shared.py"]},
        {"id": "wp-2", "files_to_modify": ["shared.py"]},
    ]
    state = _make_state(work_packages)

    async def _fake_batch(packages, _state):
        return [{"package_id": pkg["id"], "status": "PASS"} for pkg in packages]

    with patch("reposition.graph.get_config", return_value=_cfg(2)), patch(
        "reposition.graph.run_package_batch", side_effect=_fake_batch
    ), patch("reposition.graph.RunTracer", _NoopTracer):
        result = await package_scheduler_node(state)

    assert [r["package_id"] for r in result["package_results"]] == ["wp-1"]
    assert result["pending_package_ids"] == ["wp-2"]


@pytest.mark.asyncio
async def test_a_and_c_overlap_b_does_not():
    work_packages = [
        {"id": "wp-a", "files_to_modify": ["x.py"]},
        {"id": "wp-b", "files_to_modify": ["y.py"]},
        {"id": "wp-c", "files_to_modify": ["x.py"]},
    ]
    state = _make_state(work_packages)

    async def _fake_batch(packages, _state):
        return [{"package_id": pkg["id"], "status": "PASS"} for pkg in packages]

    with patch("reposition.graph.get_config", return_value=_cfg(2)), patch(
        "reposition.graph.run_package_batch", side_effect=_fake_batch
    ), patch("reposition.graph.RunTracer", _NoopTracer):
        result = await package_scheduler_node(state)

    assert [r["package_id"] for r in result["package_results"]] == ["wp-a", "wp-b"]
    assert result["pending_package_ids"] == ["wp-c"]


@pytest.mark.asyncio
async def test_max_concurrent_one_forces_serial_execution():
    work_packages = [
        {"id": "wp-1", "files_to_modify": ["a.py"]},
        {"id": "wp-2", "files_to_modify": ["b.py"]},
    ]
    state = _make_state(work_packages)

    async def _fake_batch(packages, _state):
        return [{"package_id": pkg["id"], "status": "PASS"} for pkg in packages]

    with patch("reposition.graph.get_config", return_value=_cfg(1)), patch(
        "reposition.graph.run_package_batch", side_effect=_fake_batch
    ), patch("reposition.graph.RunTracer", _NoopTracer):
        result = await package_scheduler_node(state)

    assert [r["package_id"] for r in result["package_results"]] == ["wp-1"]
    assert result["pending_package_ids"] == ["wp-2"]


def test_route_after_scheduler_all_complete_goes_to_pr_agent():
    state = make_initial_state("/fake/repo")
    state["pending_package_ids"] = []
    state["active_package_ids"] = []

    assert route_after_package_scheduler(state) == "pr_agent"


def test_route_after_scheduler_with_active_packages_goes_to_wait_node():
    state = make_initial_state("/fake/repo")
    state["pending_package_ids"] = []
    state["active_package_ids"] = ["wp-1"]

    assert route_after_package_scheduler(state) == "wait_node"

"""Tests for package scheduler routing decisions."""

from __future__ import annotations

from reposition.graph import route_after_package_scheduler
from reposition.state import make_initial_state


def _make_state() -> dict:
    """Build a minimal RepositionState for scheduler route tests."""
    state = make_initial_state("/fake/repo")
    state["work_packages"] = [
        {"id": "wp-1", "files_to_modify": ["f.py"]},
        {"id": "wp-2", "files_to_modify": ["g.py"]},
    ]
    return state


class TestRouteAfterPackageScheduler:
    def test_routes_to_pr_agent_when_no_pending_or_active(self):
        state = _make_state()
        state["pending_package_ids"] = []
        state["active_package_ids"] = []
        assert route_after_package_scheduler(state) == "pr_agent"

    def test_routes_to_wait_node_when_only_active_remain(self):
        state = _make_state()
        state["pending_package_ids"] = []
        state["active_package_ids"] = ["wp-1"]
        assert route_after_package_scheduler(state) == "wait_node"

    def test_routes_back_to_scheduler_when_pending_exist(self):
        state = _make_state()
        state["pending_package_ids"] = ["wp-2"]
        state["active_package_ids"] = []
        assert route_after_package_scheduler(state) == "package_scheduler"

    def test_pending_takes_precedence_over_active(self):
        state = _make_state()
        state["pending_package_ids"] = ["wp-2"]
        state["active_package_ids"] = ["wp-1"]
        assert route_after_package_scheduler(state) == "package_scheduler"

"""Tests for the scanner agent and supporting tools."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

SAMPLE_REPO = str(
    Path(__file__).resolve().parent / "fixtures" / "sample_repo"
)


# ── secret_scanner ──────────────────────────────────────────────────────


class TestSecretScanner:
    def test_flags_hardcoded_key_in_auth(self):
        """app/auth.py contains a high-entropy secret assignment that must be caught."""
        from reposition.tools.secret_scanner import scan_for_secrets

        auth_path = os.path.join(SAMPLE_REPO, "app", "auth.py")
        content = Path(auth_path).read_text(encoding="utf-8")
        reasons = scan_for_secrets("app/auth.py", content)
        assert len(reasons) > 0, "Expected secret_scanner to flag auth.py"
        assert any("secret" in r.lower() or "entropy" in r.lower() for r in reasons)

    def test_clean_file_not_flagged(self):
        from reposition.tools.secret_scanner import scan_for_secrets

        reasons = scan_for_secrets("utils.py", "x = 1\n")
        assert reasons == []

    def test_filter_repo_files_excludes_auth(self):
        from reposition.tools.secret_scanner import filter_repo_files

        safe, excluded = filter_repo_files(SAMPLE_REPO)
        # auth.py has a hardcoded secret → must be excluded
        auth_excluded = any("auth.py" in f for f in excluded)
        assert auth_excluded, f"auth.py should be excluded but excluded={excluded}"
        # main.py is safe (SQL injection is not a secret)
        main_safe = any("main.py" in f and "test" not in f for f in safe)
        assert main_safe, f"app/main.py should be safe but safe={safe}"


# ── test_runner_detector ────────────────────────────────────────────────


class TestTestRunnerDetector:
    def test_detects_pytest_for_sample_repo(self):
        """sample_repo has pyproject.toml with [tool.pytest.ini_options]."""
        from reposition.tools.test_runner_detector import detect_test_runner

        result = detect_test_runner(SAMPLE_REPO)
        assert result == "pytest"

    def test_returns_none_for_empty_dir(self, tmp_path):
        from reposition.tools.test_runner_detector import detect_test_runner

        assert detect_test_runner(str(tmp_path)) is None


# ── ast_parser ──────────────────────────────────────────────────────────


class TestAstParser:
    def test_extracts_python_declarations(self):
        from reposition.tools.ast_parser import extract_top_level_declarations

        code = textwrap.dedent("""\
            def foo():
                pass

            class Bar:
                pass
        """)
        result = extract_top_level_declarations("example.py", code)
        assert result["language"] == "python"
        names = [d["name"] for d in result["declarations"]]
        assert "foo" in names
        assert "Bar" in names

    def test_unknown_extension_returns_empty(self):
        from reposition.tools.ast_parser import extract_top_level_declarations

        result = extract_top_level_declarations("data.csv", "a,b,c\n1,2,3\n")
        assert result["language"] == "unknown"
        assert result["declarations"] == []


# ── scanner_agent ───────────────────────────────────────────────────────


class TestScannerAgent:
    @pytest.fixture()
    def _mock_sandbox(self):
        with patch(
            "reposition.agents.scanner.E2BSandboxManager"
        ) as MockCls:
            instance = MockCls.return_value
            instance.create_sandbox = AsyncMock(return_value="sandbox-123")
            yield instance

    @pytest.fixture()
    def _mock_tracer(self):
        with patch(
            "reposition.agents.scanner.RunTracer"
        ) as MockCls:
            instance = MockCls.return_value
            instance.log = lambda **kw: None
            yield instance

    @pytest.mark.asyncio
    async def test_manifest_has_required_keys(self, _mock_sandbox, _mock_tracer):
        from reposition.agents.scanner import scanner_agent
        from reposition.state import make_initial_state

        state = make_initial_state(SAMPLE_REPO)
        state["trace_path"] = "dummy.jsonl"
        result = await scanner_agent(state)

        manifest = result["manifest"]
        for key in ("files", "entry_points", "module_boundaries",
                     "test_runner", "dependency_files", "total_files", "total_lines"):
            assert key in manifest, f"Missing manifest key: {key}"

        assert isinstance(manifest["files"], list)
        assert manifest["total_files"] > 0

    @pytest.mark.asyncio
    async def test_large_file_gets_ast_only_manifest(
        self, _mock_sandbox, _mock_tracer, tmp_path
    ):
        """Files exceeding large_file_threshold_lines get full_content_available=False."""
        from reposition.agents.scanner import scanner_agent
        from reposition.state import make_initial_state

        # Create a Python file with 600 lines (above the 500-line threshold)
        big_file = tmp_path / "big.py"
        big_file.write_text(
            "\n".join(f"x_{i} = {i}" for i in range(600)) + "\n",
            encoding="utf-8",
        )

        state = make_initial_state(str(tmp_path))
        state["trace_path"] = "dummy.jsonl"
        result = await scanner_agent(state)

        manifest = result["manifest"]
        big_entries = [f for f in manifest["files"] if f["path"] == "big.py"]
        assert len(big_entries) == 1
        assert big_entries[0]["full_content_available"] is False
        assert big_entries[0]["line_count"] > 500

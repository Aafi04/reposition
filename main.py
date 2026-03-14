"""Reposition CLI - entry point for the pipeline."""

from __future__ import annotations

import asyncio
import json
import getpass
import importlib.util
import os
import subprocess
import sys
import threading
import time
import traceback
import signal
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

console = Console(legacy_windows=False)
_logo_shown = False

LOGO = """
[bold cyan]
██████╗ ███████╗██████╗  ██████╗ ███████╗██╗████████╗██╗ ██████╗ ███╗   ██╗
██╔══██╗██╔════╝██╔══██╗██╔═══██╗██╔════╝██║╚══██╔══╝██║██╔═══██╗████╗  ██║
██████╔╝█████╗  ██████╔╝██║   ██║███████╗██║   ██║   ██║██║   ██║██╔██╗ ██║
██╔══██╗██╔══╝  ██╔═══╝ ██║   ██║╚════██║██║   ██║   ██║██║   ██║██║╚██╗██║
██║  ██║███████╗██║     ╚██████╔╝███████║██║   ██║   ██║╚██████╔╝██║ ╚████║
╚═╝  ╚═╝╚══════╝╚═╝      ╚═════╝ ╚══════╝╚═╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
[/bold cyan]
[dim]autonomous code improvement agent[/dim]
[dim]github.com/Aafi04/reposition[/dim]
"""


# -- pipeline display ---------------------------------------------------------

_STAGE_ORDER = ["scanner", "analyzers", "planner", "coder", "validator", "pr_agent"]
_STAGE_LABELS = {
    "scanner": "Scanner",
    "analyzers": "Analyzers",
    "planner": "Planner",
    "coder": "Coder",
    "validator": "Validator",
    "pr_agent": "PR Agent",
}

_ANALYZER_NAMES = {
    "security": "Security Analyzer",
    "refactor": "Refactor Analyzer",
    "coverage": "Coverage Analyzer",
}

_STATS_ORDER = ["Scanner", "Security", "Refactor", "Coverage", "Planner", "Coder", "Validator", "PR Agent"]

_PROVIDER_OPTIONS: dict[int, dict[str, str]] = {
    1: {
        "name": "Gemini",
        "slug": "gemini",
        "env_key": "GEMINI_API_KEY",
        "url": "aistudio.google.com/app/apikey",
    },
    2: {
        "name": "OpenAI",
        "slug": "openai",
        "env_key": "OPENAI_API_KEY",
        "url": "platform.openai.com/api-keys",
    },
    3: {
        "name": "Anthropic",
        "slug": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "url": "console.anthropic.com",
    },
    4: {
        "name": "Groq",
        "slug": "groq",
        "env_key": "GROQ_API_KEY",
        "url": "console.groq.com/keys",
    },
}

_PROVIDER_MODULE: dict[str, str] = {
    "gemini": "langchain_google_genai",
    "openai": "langchain_openai",
    "anthropic": "langchain_anthropic",
    "groq": "langchain_groq",
}

_PROVIDER_PACKAGE: dict[str, str] = {
    "gemini": "langchain-google-genai>=1.0.0",
    "openai": "langchain-openai>=0.1.0",
    "anthropic": "langchain-anthropic>=0.3.0",
    "groq": "langchain-groq>=0.1.0",
}


class TerminalDisplay:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._lock = threading.Lock()
        self._rendered = False
        self._line_count = 0
        self._suppress_timer = False

    def full_render(self, lines: list[str]) -> None:
        """Full redraw on state changes only."""
        with self._lock:
            self._suppress_timer = True
            try:
                if self._rendered:
                    sys.stdout.write(f"\033[{self._line_count}A\033[J")
                    sys.stdout.flush()
                for line in lines:
                    self.console.print(line)
                self._line_count = len(lines)
                self._rendered = True
            finally:
                self._suppress_timer = False

    def update_lines(self, updates: dict[int, str]) -> None:
        """Update specific lines in place without full redraw."""
        with self._lock:
            if self._suppress_timer:
                return
            if not self._rendered:
                return
            for line_idx, content in updates.items():
                # Move cursor to the target line from the bottom
                lines_up = self._line_count - line_idx
                if lines_up <= 0:
                    continue
                # Save/restore cursor around each in-place update to avoid drift.
                # Drift causes right-edge corruption and partial clears on interrupt.
                sys.stdout.write("\033[s")
                sys.stdout.write(f"\033[{lines_up}A\033[2K\r")
                self.console.print(content, end="")
                sys.stdout.write("\033[u")
                sys.stdout.flush()

    def clear(self) -> None:
        with self._lock:
            if self._rendered:
                sys.stdout.write(f"\033[{self._line_count}A\033[J")
                sys.stdout.flush()
                self._rendered = False
                self._line_count = 0


def _new_display_state(dry_run: bool) -> dict[str, Any]:
    now = time.monotonic()
    base = {
        "dry_run": dry_run,
        "is_dry_run": dry_run,
        "start_time": now,
        "current_activity": "[cyan]>[/cyan] Scanner    Initializing pipeline...",
        # nested structure used by existing helpers
        "stages": {name: {"status": "waiting", "retry": 0} for name in _STAGE_ORDER},
        "analyzer_statuses": {"security": "waiting", "refactor": "waiting", "coverage": "waiting"},
        # stage_times keyed by stage_key (scanner, analyzers, ...)
        "stage_times": {name: {"start": None, "end": None} for name in _STAGE_ORDER},
        "stats": {agent: {"status": "-", "detail": "-"} for agent in _STATS_ORDER},
        "summary_ready": False,
        "summary_lines": [],
    }

    # Flat keys used by table rendering and direct lookups
    flat: dict[str, Any] = {
        "current_activity": base["current_activity"],
        "stage_scanner": "pending",
        "stage_analyzers": "pending",
        "stage_planner": "pending",
        "stage_coder": "pending",
        "stage_validator": "pending",
        "stage_pr_agent": "pending",
        "stage_times": {
            "Scanner": {"start": None, "end": None},
            "Analyzers": {"start": None, "end": None},
            "Planner": {"start": None, "end": None},
            "Coder": {"start": None, "end": None},
            "Validator": {"start": None, "end": None},
            "PR Agent": {"start": None, "end": None},
        },
        "scanner_status": "-",
        "scanner_detail": "-",
        "security_status": "-",
        "security_detail": "-",
        "refactor_status": "-",
        "refactor_detail": "-",
        "coverage_status": "-",
        "coverage_detail": "-",
        "planner_status": "-",
        "planner_detail": "-",
        "coder_status": "-",
        "coder_detail": "-",
        "validator_status": "-",
        "validator_detail": "-",
        "pr_agent_status": "-",
        "pr_agent_detail": "-",
    }

    # Merge and return; keep nested base keys for compatibility
    # Don't let flat stage_times overwrite nested stage_times used by helpers
    flat.pop("stage_times", None)
    base.update(flat)
    return base


def _env_has_values(env_path: Path) -> bool:
    """Return True if .env has at least one non-comment key assignment."""
    if not env_path.exists():
        return False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            return True
    return False


def _prompt_secret(prompt: str) -> str:
    """Prompt for a secret with masking and re-entry support."""
    while True:
        value = getpass.getpass(prompt).strip()
        print(f"Received {len(value)} characters.")
        retry = input("Press Enter to continue, or type r to re-enter: ").strip().lower()
        if retry != "r":
            return value


def _handle_sigint(signum, frame):
    """Called on Ctrl+C. Cancels the running asyncio tasks gracefully."""
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        return
    for task in asyncio.all_tasks(loop):
        try:
            task.cancel()
        except Exception:
            pass


def _ensure_provider_dependency(provider_slug: str) -> bool:
    """Install the selected provider SDK on demand if it is missing."""
    module_name = _PROVIDER_MODULE[provider_slug]
    if importlib.util.find_spec(module_name):
        return True

    package_name = _PROVIDER_PACKAGE[provider_slug]
    print(f"Missing provider SDK '{module_name}'. Installing {package_name}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package_name],
        check=False,
    )
    if result.returncode != 0:
        print(f"[FAIL] Unable to install {package_name}.")
        return False

    return importlib.util.find_spec(module_name) is not None


def _set_stage_status(display_state: dict[str, Any], stage: str, status: str, retry: int = 0) -> None:
    if stage not in display_state["stages"]:
        return
    now = time.monotonic()
    display_state["stages"][stage]["status"] = status
    display_state["stages"][stage]["retry"] = retry
    times = display_state["stage_times"][stage]

    if status in ("running", "retrying") and times["start"] is None:
        times["start"] = now
        times["end"] = None

    if status in ("complete", "failed"):
        if times["start"] is None:
            times["start"] = now
        times["end"] = now


def _set_activity(display_state: dict[str, Any], agent: str, detail: str) -> None:
    display_state["current_activity"] = f"[cyan]>[/cyan] {agent:<9} {detail}"


def _set_stat(display_state: dict[str, Any], agent: str, status: str, detail: str) -> None:
    if agent not in display_state["stats"]:
        return
    display_state["stats"][agent] = {"status": status, "detail": detail}


def _fmt_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    mins = total // 60
    secs = total % 60
    return f"{mins}m {secs}s"


def _fmt_stage_time(seconds: float) -> str:
    total = max(0, int(seconds))
    mins = total // 60
    secs = total % 60
    return f"{mins}:{secs:02d}"


def _stage_elapsed(display_state: dict[str, Any], stage: str) -> float:
    times = display_state["stage_times"][stage]
    start = times["start"]
    end = times["end"]
    if start is None:
        return 0.0
    if end is None:
        end = time.monotonic()
    return max(0.0, end - start)


def _security_detail(security_report: list[dict]) -> str:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for item in security_report:
        sev = str(item.get("severity", "")).upper()
        if sev in counts:
            counts[sev] += 1
    total = len(security_report)
    parts = [f"{counts[k]} {k}" for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW") if counts[k] > 0]
    if parts:
        return f"{total} findings ({', '.join(parts)})"
    return f"{total} findings"


def _render_activity_section(display_state: dict[str, Any]) -> Panel:
    elapsed = _fmt_clock(time.monotonic() - display_state["start_time"])
    grid = Table.grid(expand=True)
    grid.add_column(ratio=5)
    grid.add_column(ratio=1, justify="right")
    grid.add_row(Text.from_markup(display_state["current_activity"]), Text(f"Elapsed: {elapsed}", style="bold"))
    return Panel(grid, title="Current Activity", box=box.ASCII)


def _render_progress_section(display_state: dict[str, Any]) -> Panel:
    parts: list[str] = []
    blink = int(time.monotonic() * 2) % 2 == 0
    for stage in _STAGE_ORDER:
        label = _STAGE_LABELS[stage]
        status = display_state["stages"][stage]["status"]
        elapsed = _fmt_stage_time(_stage_elapsed(display_state, stage))
        if status == "complete":
            segment = f"{label} [green]OK[/green] {elapsed}"
        elif status == "failed":
            segment = f"{label} [red]FAIL[/red] {elapsed}"
        elif status in ("running", "retrying"):
            dots = "..." if blink else "   "
            segment = f"{label} [yellow]{dots}[/yellow] {elapsed}"
        else:
            segment = f"[dim]{label}[/dim]"
        parts.append(segment)

    return Panel(Text.from_markup("  ->  ".join(parts)), title="Pipeline Progress", box=box.ASCII)


def _render_stats_section(display_state: dict[str, Any]) -> Panel:
    table = Table(box=box.ASCII, expand=True)
    table.add_column("Agent", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    for agent in _STATS_ORDER[:8]:
        row = display_state["stats"][agent]
        table.add_row(agent, row["status"], row["detail"])

    return Panel(table, title="Live Stats", box=box.ASCII)


def _render_summary_section(display_state: dict[str, Any]) -> Panel:
    return Panel(Text("\n".join(display_state["summary_lines"])), title="Final Summary", box=box.ASCII)


def _build_renderables(display_state: dict[str, Any]) -> Group:
    renderables: list[Any] = [
        _render_activity_section(display_state),
        _render_progress_section(display_state),
        _render_stats_section(display_state),
    ]
    if display_state["summary_ready"]:
        renderables.append(_render_summary_section(display_state))
    return Group(*renderables)


def _build_progress_line(display_state: dict[str, Any], now: float, width: int = 74) -> str:
    stages = [
        "Scanner",
        "Analyzers",
        "Planner",
        "Coder",
        "Validator",
        "PR Agent",
    ]
    stage_parts: list[str] = []

    stage_times: dict[str, dict[str, float | None]] = display_state.get("stage_times", {})

    for stage_key in _STAGE_ORDER:
        label = _STAGE_LABELS[stage_key]
        status = display_state["stages"][stage_key]["status"]
        # stage_times is keyed by stage_key in internal state
        times = stage_times.get(stage_key, {})
        start = times.get("start")
        end = times.get("end")

        if status == "complete" and start is not None and end is not None:
            t = int(max(0, end - start))
            m, sc = divmod(t, 60)
            stage_parts.append(f"{label} {m}:{sc:02d}")
        elif status in ("running", "retrying") and start is not None:
            t = int(max(0, now - start))
            m, sc = divmod(t, 60)
            stage_parts.append(f"{label} {m}:{sc:02d}")
        elif status == "failed" and start is not None and end is not None:
            t = int(max(0, end - start))
            m, sc = divmod(t, 60)
            stage_parts.append(f"{label} {m}:{sc:02d}")
        else:
            stage_parts.append(label)

    prog = " -> ".join(stage_parts)
    inner_w = max(10, width - 4)
    if len(prog) > inner_w:
        prog = prog[: inner_w - 3] + "..."
    else:
        prog = f"{prog:<{inner_w}}"
    return f"| {prog} |"


def build_display_lines(
    display_state: dict[str, Any],
    pipeline_start_time: float,
) -> dict:
    """Return tabular layout lines and dynamic indices for the live display."""
    now = time.monotonic()
    elapsed = int(now - pipeline_start_time)
    mins, secs = divmod(elapsed, 60)
    elapsed_str = f"{mins}m {secs:02d}s"

    W = 74  # total display width
    border = "+" + "-" * (W - 2) + "+"

    lines: list[str] = []

    # Top border
    lines.append(f"[dim]{border}[/dim]")  # line 0

    # Activity + elapsed (strict fixed width to prevent right-edge wrap)
    activity = str(display_state.get("current_activity", "Initializing..."))
    try:
        activity_plain = Text.from_markup(activity).plain
    except Exception:
        activity_plain = activity
    right_text = f"Elapsed: {elapsed_str}"
    inner_w = W - 4
    left = f"> {activity_plain}"
    max_left = max(4, inner_w - len(right_text) - 1)
    if len(left) > max_left:
        left = left[: max_left - 3] + "..."
    spaces = max(1, inner_w - len(left) - len(right_text))
    activity_line = f"| {left}{' ' * spaces}{right_text} |"
    lines.append(activity_line)  # line 1

    # Middle border
    lines.append(f"[dim]{border}[/dim]")  # line 2

    # Pipeline progress row
    progress_line = _build_progress_line(display_state, now, width=W)
    lines.append(progress_line)  # line 3

    # Second border
    lines.append(f"[dim]{border}[/dim]")  # line 4

    # Table header (strict fixed widths)
    agent_w = 16
    status_w = 9
    detail_w = max(8, (W - 4) - agent_w - status_w - 2)
    lines.append(f"| {'Agent':<{agent_w}}{'Status':<{status_w}}  {'Detail':<{detail_w}} |")  # line 5
    lines.append(f"| {'-'*agent_w}{'-'*status_w}  {'-'*detail_w} |")  # line 6

    agents = [
        "Scanner",
        "Security",
        "Refactor",
        "Coverage",
        "Planner",
        "Coder",
        "Validator",
        "PR Agent",
    ]
    for agent in agents:
        key = agent.lower().replace(" ", "_")
        status = display_state.get(f"{key}_status", "-")
        detail = display_state.get(f"{key}_detail", "-")
        # Bug 3: PR Agent should not display in dry run
        is_dry = display_state.get("is_dry_run", False)
        if agent == "PR Agent" and is_dry:
            status = "-"
            detail = "N/A (dry run)"

        max_detail = detail_w
        if len(detail) > max_detail:
            detail = detail[: max_detail - 3] + "..."
        if status == "OK":
            s_plain = "OK"
            s_str = "[green]OK[/green]"
        elif status == "RUNNING":
            s_plain = "RUNNING"
            s_str = "[yellow]RUNNING[/yellow]"
        elif status == "FAIL":
            s_plain = "FAIL"
            s_str = "[red]FAIL[/red]"
        else:
            s_plain = "-"
            s_str = "[dim]-[/dim]"
        s_cell = f"{s_str}{' ' * max(0, status_w - len(s_plain))}"

        # Bug 1: Add explicit space after status markup and avoid width padding on markup
        lines.append(
            f"| [bold]{agent:<{agent_w}}[/bold]"
            f"{s_cell}  {detail:<{detail_w}} |"
        )

    # Bottom border
    lines.append(f"[dim]{border}[/dim]")

    return {
        "lines": lines,
        "dynamic": {
            1: activity_line,
            3: progress_line,
        },
    }


def _status_from_analyzer(value: str) -> str:
    if value == "COMPLETE":
        return "OK"
    if value in ("ERROR", "TIMED_OUT"):
        return "FAIL"
    return "RUNNING"


def _mark_final_summary(display_state: dict[str, Any], final_state: dict[str, Any], dry_run: bool) -> None:
    elapsed = _fmt_clock(time.monotonic() - display_state["start_time"])
    if dry_run:
        planned = len(final_state.get("work_packages", []))
        packages = final_state.get("work_packages", [])
        top_priority = packages[0].get("priority_label", "N/A") if packages else "N/A"
        display_state["summary_lines"] = [
            "+-- Dry Run Complete ---------------------------------------+",
            f"|  [OK] {planned} work packages planned",
            f"|  [OK] Top priority: {top_priority}",
            "|  [OK] No changes made to repo",
            "|  Run without --dry-run to execute",
            f"|  Time: {elapsed}",
            "+-----------------------------------------------------------+",
        ]
    else:
        results = final_state.get("package_results", [])
        attempted = len(results)
        passed = sum(1 for r in results if r.get("status") in ("PASS", "PASS_NO_TESTS"))
        failed = attempted - passed
        pr_url = final_state.get("pr_url") or "(none)"
        run_id = final_state.get("run_id")
        trace_path = final_state.get("trace_path") or (f".traces/{run_id}.jsonl" if run_id else "(unknown)")

        display_state["summary_lines"] = [
            "+-- Run Complete -------------------------------------------+",
            f"|  [OK] {passed}/{attempted} packages passed",
            f"|  [OK] PR opened: {pr_url}",
            f"|  [OK] Trace: {trace_path}",
            f"|  [!] Packages failed: {failed}",
            f"|  Time: {elapsed}",
            "+-----------------------------------------------------------+",
        ]

    display_state["summary_ready"] = True


def _apply_event_update(
    display_state: dict[str, Any],
    node_name: str,
    merged_state: dict[str, Any],
    update: dict[str, Any],
    max_retries: int,
) -> None:
    # Map merged_state keys to flat display_state keys and keep existing nested state updated
    now = time.monotonic()

    # Scanner completed -> manifest present
    if "manifest" in update:
        manifest = update.get("manifest") or {}
        files = manifest.get("files") or []
        excluded = update.get("excluded_files", []) or []

        display_state["scanner_status"] = "OK"
        display_state["scanner_detail"] = f"{len(files)} files, {len(excluded)} secrets excluded"
        display_state["stage_scanner"] = "complete"
        # nested helpers
        _set_stage_status(display_state, "scanner", "complete")
        # set nested end time
        try:
            display_state["stage_times"]["scanner"]["end"] = now
        except Exception:
            pass

        # Start analyzers
        display_state["stage_analyzers"] = "running"
        _set_stage_status(display_state, "analyzers", "running")
        display_state["current_activity"] = "Analyzers running in parallel..."
        # set running details
        display_state["security_status"] = "RUNNING"
        display_state["security_detail"] = "Analyzing authentication boundaries..."
        display_state["refactor_status"] = "RUNNING"
        display_state["refactor_detail"] = "Scanning for SRP violations..."
        display_state["coverage_status"] = "RUNNING"
        display_state["coverage_detail"] = "Checking uncovered execution paths..."

    # Security report arrived
    if "security_report" in update:
        sec = update.get("security_report", []) or []
        count = len(sec)
        crits = sum(1 for x in sec if str(x.get("severity")).upper() == "CRITICAL")
        highs = sum(1 for x in sec if str(x.get("severity")).upper() == "HIGH")
        meds = sum(1 for x in sec if str(x.get("severity")).upper() == "MEDIUM")
        lows = sum(1 for x in sec if str(x.get("severity")).upper() == "LOW")
        parts = []
        if crits:
            parts.append(f"{crits} CRITICAL")
        if highs:
            parts.append(f"{highs} HIGH")
        if meds:
            parts.append(f"{meds} MEDIUM")
        if lows:
            parts.append(f"{lows} LOW")
        detail = ", ".join(parts) if parts else "0 findings"
        # Bug 2: always mark security as OK when report key is present
        display_state["security_status"] = "OK"
        display_state["security_detail"] = (f"{count} findings ({detail})" if count else "0 findings")
        # mark analyzers running
        display_state["stage_analyzers"] = display_state.get("stage_analyzers", "running")

    # Refactor report
    if "refactor_report" in update:
        ref = update.get("refactor_report", []) or []
        display_state["refactor_status"] = "OK"
        display_state["refactor_detail"] = f"{len(ref)} findings"

    # Coverage report
    if "coverage_report" in update:
        cov = update.get("coverage_report", []) or []
        display_state["coverage_status"] = "OK"
        display_state["coverage_detail"] = f"{len(cov)} uncovered paths"
        # All analyzers done
        display_state["stage_analyzers"] = "complete"
        _set_stage_status(display_state, "analyzers", "complete")
        try:
            display_state["stage_times"]["analyzers"]["end"] = now
        except Exception:
            pass
        # Start planner
        display_state["stage_planner"] = "running"
        _set_stage_status(display_state, "planner", "running")
        display_state["current_activity"] = "Planner synthesizing work packages..."

    # Work packages (planner complete)
    if "work_packages" in update:
        pkgs = update.get("work_packages", [])
        if pkgs is None:
            pkgs = []
        # Bug 4: planner detail wording and fallback
        display_state["planner_status"] = "OK"
        if not pkgs:
            planner_detail = "0 work packages planned"
        else:
            top_label = pkgs[0].get("priority_label", "")
            if top_label:
                planner_detail = (
                    f"{len(pkgs)} work packages "
                    f"({top_label} first)"
                )
            else:
                planner_detail = f"{len(pkgs)} work packages"
        display_state["planner_detail"] = planner_detail
        display_state["stage_planner"] = "complete"
        _set_stage_status(display_state, "planner", "complete")
        try:
            display_state["stage_times"]["planner"]["end"] = now
        except Exception:
            pass
        if not display_state.get("is_dry_run", False):
            # Start coder
            display_state["stage_coder"] = "running"
            _set_stage_status(display_state, "coder", "running")
            display_state["current_activity"] = "Coder generating patches..."

    # Current patch (coder running)
    if "current_patch" in update:
        if not display_state.get("is_dry_run", False):
            idx = int(update.get("current_package_index", 0))
            total = len(update.get("work_packages", []))
            pkgs = update.get("work_packages", []) or []
            pkg = pkgs[idx] if 0 <= idx < len(pkgs) else {}
            files = pkg.get("files_to_modify", []) if pkg else []
            file_str = files[0] if files else "..."
            display_state["coder_status"] = "RUNNING"
            display_state["coder_detail"] = f"Package {idx+1}/{total} -- {file_str}"
            if display_state.get("stage_coder") != "running":
                display_state["stage_coder"] = "running"
                try:
                    display_state["stage_times"]["coder"]["start"] = now
                    display_state["stage_times"]["coder"]["end"] = None
                except Exception:
                    pass

    # Package results / validator
    if "package_results" in update:
        if not display_state.get("is_dry_run", False):
            results = update.get("package_results", []) or []
            passed = sum(1 for r in results if r.get("status") in ("PASS", "PASS_NO_TESTS"))
            failed = sum(1 for r in results if r.get("status") in ("FAIL_COMPILE", "FAIL_TEST", "ABORTED"))
            last = results[-1] if results else {}
            verdict = last.get("status", "")
            if verdict in ("PASS", "PASS_NO_TESTS"):
                display_state["validator_status"] = "OK"
                display_state["validator_detail"] = verdict
                display_state["coder_status"] = "OK"
                _set_stage_status(display_state, "validator", "complete")
                _set_stage_status(display_state, "coder", "complete")
                try:
                    display_state["stage_times"]["validator"]["end"] = now
                    display_state["stage_times"]["coder"]["end"] = now
                except Exception:
                    pass
            elif verdict in ("FAIL_COMPILE", "FAIL_TEST"):
                display_state["validator_status"] = "FAIL"
                display_state["validator_detail"] = verdict
                display_state["coder_status"] = "RETRYING"

    # PR opened
    if "pr_url" in update:
        if not display_state.get("is_dry_run", False):
            display_state["pr_agent_status"] = "OK"
            pr_num = update.get("pr_number")
            detail = f"PR #{pr_num}" if pr_num else "PR opened"
            display_state["pr_agent_detail"] = detail
            display_state["stage_pr_agent"] = "complete"
            _set_stage_status(display_state, "pr_agent", "complete")
            try:
                display_state["stage_times"]["pr_agent"]["end"] = now
            except Exception:
                pass
            # mark coder/validator complete as well
            display_state["stage_coder"] = "complete"
            display_state["stage_validator"] = "complete"
            try:
                display_state["stage_times"]["coder"]["end"] = now
                display_state["stage_times"]["validator"]["end"] = now
            except Exception:
                pass


async def _run_with_live(stream, max_retries: int, dry_run: bool = False) -> dict[str, Any]:
    # Capture pipeline start so timers can use monotonic time.
    pipeline_start_time = time.monotonic()

    display_state = _new_display_state(dry_run)
    display_state["start_time"] = pipeline_start_time
    _set_stage_status(display_state, "scanner", "running")
    _set_activity(display_state, "Scanner", "Uploading repo to E2B sandbox...")
    _set_stat(display_state, "Scanner", "RUNNING", "Scanning repository...")

    final_state: dict[str, Any] = {}
    interrupted = False

    console.print()
    display = TerminalDisplay(console)
    stop_timer = threading.Event()

    def _timer_loop() -> None:
        while not stop_timer.is_set():
            stop_timer.wait(timeout=1.0)
            if stop_timer.is_set():
                break
            try:
                dynamic = build_display_lines(display_state, pipeline_start_time)["dynamic"]
                display.update_lines({1: dynamic[1], 3: dynamic[3]})
            except Exception:
                pass

    async def _consume_stream() -> None:
        nonlocal interrupted
        try:
            async for event in stream:
                for node_name, update in event.items():
                    if not isinstance(update, dict):
                        continue
                    final_state.update(update)
                    _apply_event_update(display_state, node_name, final_state, update, max_retries)
                try:
                    result = build_display_lines(display_state, pipeline_start_time)
                    display.full_render(result["lines"])
                except Exception:
                    pass
        except KeyboardInterrupt:
            interrupted = True
        except asyncio.CancelledError:
            # Propagate cancellation to outer scope for centralized handling
            raise

    timer_thread = threading.Thread(target=_timer_loop, daemon=True)
    timer_thread.start()
    try:
        await _consume_stream()
    finally:
        stop_timer.set()
        timer_thread.join(timeout=2.0)
        display.clear()

    if interrupted:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await aclose()
        raise KeyboardInterrupt

    return final_state


def _print_plain_summary(final_state: dict[str, Any], elapsed_seconds: float, dry_run: bool = False) -> None:
    elapsed = _fmt_clock(elapsed_seconds)

    if dry_run:
        packages = final_state.get("work_packages", [])
        top_priority = packages[0].get("priority_label", "N/A") if packages else "N/A"
        run_id = final_state.get("run_id")
        console.print()
        console.print("[bold cyan]Run complete[/bold cyan]")
        if run_id:
            console.print(f"  Run ID:             {run_id}")
        console.print(f"  Packages planned:   {len(packages)}")
        console.print(f"  Top priority:       {top_priority}")
        console.print("  No changes made to repo")
        console.print(f"  Time: {elapsed}")
        return

    results = final_state.get("package_results", [])
    attempted = len(results)
    passed = sum(1 for r in results if r.get("status") in ("PASS", "PASS_NO_TESTS"))
    failed = attempted - passed
    pr_url = final_state.get("pr_url")
    run_id = final_state.get("run_id")

    console.print()
    console.print("[bold cyan]Run complete[/bold cyan]")
    if run_id:
        console.print(f"  Run ID:             {run_id}")
    console.print(f"  Packages attempted: {attempted}")
    console.print(f"  Packages passed:    [green]{passed}[/green]")
    console.print(f"  Packages failed:    [red]{failed}[/red]")
    if pr_url:
        console.print(f"  PR opened: [cyan]{pr_url}[/cyan]")
    console.print(f"  Time: {elapsed}")


# -- CLI ---------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Reposition - AI-powered repository improvement pipeline."""
    global _logo_shown
    if not _logo_shown:
        try:
            console.print(LOGO)
        except UnicodeEncodeError:
            print("Reposition")
            print("autonomous code improvement agent")
            print("github.com/Aafi04/reposition")
        _logo_shown = True


@cli.command()
@click.argument("repo", required=False)
@click.option("--dry-run", is_flag=True, help="Run Scanner + Analyzers + Planner only.")
@click.option("--config", "config_path", default=None, help="Path to config YAML.")
@click.option(
    "--clone-dir",
    default=None,
    help="Where to clone the repo (default: ~/.reposition/repos/).",
)
@click.option(
    "--pr-repo",
    default=None,
    help="Open PR on this repo instead of the analysis target.",
)
def run(repo: str | None, dry_run: bool, config_path: str | None, clone_dir: str | None, pr_repo: str | None) -> None:
    """Run the Reposition pipeline on a repository."""
    if config_path:
        from reposition.config import load_config
        import reposition.config as _cfg_mod

        _cfg_mod._singleton = load_config(config_path)

    from reposition.config import get_config
    from reposition.graph import resolve_repo_path
    from reposition.tools.github_tools import normalize_repo

    cfg = get_config()
    default_clone_root = cfg.github.clone_dir

    # Preflight: fail fast when provider / key aren't configured, and avoid loading any LLMs.
    provider_env = os.environ.get("REPOSITION_LLM_PROVIDER", "").strip().lower()
    if not provider_env:
        console.print("[red]Not configured.[/red] Run [cyan]reposition setup[/cyan] first.")
        sys.exit(1)

    provider = provider_env
    env_key = _PROVIDER_OPTIONS.get(
        next((k for k, v in _PROVIDER_OPTIONS.items() if v["slug"] == provider), -1),
        {},
    ).get("env_key")

    api_key_present = False
    if provider == "gemini":
        api_key_present = bool(os.environ.get("GOOGLE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip())
    elif env_key:
        api_key_present = bool(os.environ.get(env_key, "").strip())

    if not api_key_present:
        console.print(f"[red]Missing API key for '{provider}'.[/red]")
        console.print("Run [cyan]reposition setup[/cyan] to reconfigure.")
        sys.exit(1)

    normalized_analysis_repo: dict[str, str] | None = None
    if repo:
        try:
            normalized_analysis_repo = normalize_repo(repo)
        except ValueError as exc:
            local_repo_path = Path(repo).expanduser()
            if local_repo_path.exists():
                normalized_analysis_repo = None
            else:
                console.print(f"[red]{exc}[/red]")
                raise SystemExit(1)
    else:
        configured_default_target = cfg.github.pr_repo.strip()
        if configured_default_target:
            try:
                normalized_analysis_repo = normalize_repo(configured_default_target)
            except ValueError as exc:
                console.print(f"[red]Invalid configured default repo:[/red] {exc}")
                raise SystemExit(1)
            console.print(f"[dim]Analyzing {normalized_analysis_repo['clone_url']}[/dim]")
        else:
            console.print("[red]No repo specified.[/red]")
            console.print("Usage: reposition run <repo>")
            console.print("   or: reposition run https://github.com/owner/repo")
            raise SystemExit(1)

    if normalized_analysis_repo is not None:
        analysis_repo_input = normalized_analysis_repo["clone_url"]
    else:
        assert repo is not None
        analysis_repo_input = repo

    chosen_pr_repo_input = pr_repo or cfg.github.pr_repo.strip() or (
        normalized_analysis_repo["owner_repo"] if normalized_analysis_repo else ""
    )

    if chosen_pr_repo_input:
        try:
            normalized_pr_repo = normalize_repo(chosen_pr_repo_input)
        except ValueError as exc:
            console.print(f"[red]Invalid PR target repo:[/red] {exc}")
            raise SystemExit(1)
        os.environ["GITHUB_REPO"] = normalized_pr_repo["owner_repo"]
        os.environ["GITHUB_PR_REPO"] = normalized_pr_repo["owner_repo"]

    try:
        resolved_repo_path = resolve_repo_path(
            repo_path=analysis_repo_input,
            clone_dir=clone_dir,
            default_clone_root=default_clone_root,
        )
    except RuntimeError as exc:
        console.print(f"[red]Repository resolution error:[/red] {exc}")
        raise SystemExit(1)

    if normalized_analysis_repo is not None:
        console.print(f"[bold]Clone destination:[/bold] {resolved_repo_path}")

    if not _ensure_provider_dependency(provider):
        console.print("[red]Configuration error:[/red] unable to install provider SDK.")
        raise SystemExit(1)

    # Preflight: verify LLM provider + API key before starting the pipeline
    from reposition.llm_client import get_llm

    try:
        get_llm("fast")
        get_llm("heavy")
    except (ValueError, EnvironmentError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise SystemExit(1)

    if dry_run:
        # Install SIGINT handler so Ctrl+C cancels asyncio tasks immediately.
        signal.signal(signal.SIGINT, _handle_sigint)
        try:
            asyncio.run(_run_dry(resolved_repo_path))
        finally:
            # Restore default SIGINT handling so post-run behavior is normal.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
    else:
        # Install SIGINT handler so Ctrl+C cancels asyncio tasks immediately.
        signal.signal(signal.SIGINT, _handle_sigint)
        try:
            asyncio.run(_run_full(resolved_repo_path))
        finally:
            # Restore default SIGINT handling so post-run behavior is normal.
            signal.signal(signal.SIGINT, signal.SIG_DFL)


@cli.command()
def setup() -> None:
    """Run an interactive one-time setup wizard."""
    from reposition.config import get_env_path

    project_root = Path(__file__).resolve().parent
    env_path = get_env_path()

    if _env_has_values(env_path):
        overwrite = input("A .env file already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            return

    print("Which LLM provider do you want to use?")
    print("  1. Gemini  (free tier - recommended for first run)")
    print("  2. OpenAI")
    print("  3. Anthropic")
    print("  4. Groq    (fastest, free tier)")
    print("")

    while True:
        choice_raw = input("Enter 1-4: ").strip()
        if choice_raw in ("1", "2", "3", "4"):
            provider_choice = int(choice_raw)
            break
        print("Please enter a number from 1 to 4.")

    provider_info = _PROVIDER_OPTIONS[provider_choice]
    provider_slug = provider_info["slug"]
    provider_name = provider_info["name"].lower()
    provider_env_key = provider_info["env_key"]

    print(f"Get your key at: {provider_info['url']}")

    llm_api_key = _prompt_secret(f"Paste your {provider_name} API key: ")
    e2b_api_key = _prompt_secret("Paste your E2B API key (free at e2b.dev/dashboard): ")
    github_token = _prompt_secret("Paste your GitHub token (github.com/settings/tokens, repo scope): ")

    env_contents = (
        f"REPOSITION_LLM_PROVIDER={provider_slug}\n"
        f"{provider_env_key}={llm_api_key}\n"
        f"E2B_API_KEY={e2b_api_key}\n"
        f"GITHUB_TOKEN={github_token}\n"
    )
    env_path.write_text(env_contents, encoding="utf-8")
    console.print(f"[dim]Config saved to: {env_path}[/dim]")

    os.environ["REPOSITION_LLM_PROVIDER"] = provider_slug
    os.environ[provider_env_key] = llm_api_key
    os.environ["E2B_API_KEY"] = e2b_api_key
    os.environ["GITHUB_TOKEN"] = github_token

    if not _ensure_provider_dependency(provider_slug):
        print("")
        print("[FAIL] Setup incomplete. Fix the issues above")
        print("       and run 'reposition setup' again.")
        return

    print("")
    print("Verifying configuration...")
    print("")

    from reposition import llm_client
    from reposition.llm_client import call_llm, get_llm, PROVIDER_DEFAULTS

    llm_client._api_key_warning_shown = True

    def _strip_fences(text: str) -> str:
        raw = (text or "").strip()
        if raw.startswith("```") and raw.endswith("```"):
            body = raw.strip("`").strip()
            if body.lower().startswith("json"):
                body = body[4:].lstrip()
            return body.strip()
        return raw

    def _api_key_ok(chosen: str) -> tuple[bool, str]:
        if chosen == "gemini":
            if os.environ.get("GOOGLE_API_KEY", "").strip():
                return True, "GOOGLE_API_KEY"
            if os.environ.get("GEMINI_API_KEY", "").strip():
                return True, "GEMINI_API_KEY"
            return False, "GEMINI_API_KEY or GOOGLE_API_KEY"
        key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY"}.get(chosen, "")
        return (bool(os.environ.get(key, "").strip()), key or "API key")

    def _model_name(llm: object, fallback: str) -> str:
        for attr in ("model_name", "model"):
            value = getattr(llm, attr, None)
            if isinstance(value, str) and value:
                return value
        return fallback

    chosen = provider_slug
    print(f"Testing provider: {chosen}")

    check1_ok, key_label = _api_key_ok(chosen)
    if check1_ok:
        print(f"[PASS] CHECK 1 - API key present ({key_label})")
    else:
        print(f"[FAIL] CHECK 1 - API key present ({key_label} missing)")

    check2_ok = False
    check3_ok = False
    check4_ok = False
    check5_ok = False
    token_usage_4: dict = {}

    if check1_ok:
        default_fast = PROVIDER_DEFAULTS[chosen]["fast"]
        try:
            llm_fast = get_llm("fast")
            fast_model = _model_name(llm_fast, default_fast)
            text, _ = call_llm(llm_fast, "You are a test bot.", "Respond with exactly: PONG")
            if "PONG" in str(text).upper():
                check2_ok = True
                print(f"[PASS] CHECK 2 - Fast model connectivity ({fast_model})")
            else:
                print(f"[FAIL] CHECK 2 - Fast model connectivity ({fast_model})")
                print(f"Response did not contain PONG: {text!r}")
        except Exception as exc:
            print(f"[FAIL] CHECK 2 - Fast model connectivity ({default_fast})")
            print(f"Exception: {exc}")
    else:
        print("[FAIL] CHECK 2 - Fast model connectivity (skipped: API key missing)")

    if check1_ok:
        default_heavy = PROVIDER_DEFAULTS[chosen]["heavy"]
        try:
            llm_heavy = get_llm("heavy")
            heavy_model = _model_name(llm_heavy, default_heavy)
            text, _ = call_llm(llm_heavy, "You are a test bot.", "Respond with exactly: PONG")
            if "PONG" in str(text).upper():
                check3_ok = True
                print(f"[PASS] CHECK 3 - Heavy model connectivity ({heavy_model})")
            else:
                print(f"[FAIL] CHECK 3 - Heavy model connectivity ({heavy_model})")
                print(f"Response did not contain PONG: {text!r}")
        except Exception as exc:
            print(f"[FAIL] CHECK 3 - Heavy model connectivity ({default_heavy})")
            print(f"Exception: {exc}")
    else:
        print("[FAIL] CHECK 3 - Heavy model connectivity (skipped: API key missing)")

    if check1_ok:
        try:
            system = "You are a JSON API. Respond with valid JSON only. No markdown, no extra text."
            user = "Return: {\"status\": \"ok\"}"
            text, token_usage_4 = call_llm(get_llm("fast"), system, user)
            parsed = json.loads(_strip_fences(str(text)))
            if isinstance(parsed, dict) and parsed.get("status") == "ok":
                check4_ok = True
                print("[PASS] CHECK 4 - JSON output reliability")
            else:
                print("[FAIL] CHECK 4 - JSON output reliability")
                print(f"Raw response failed validation: {text}")
        except Exception:
            print("[FAIL] CHECK 4 - JSON output reliability")
            print(f"Raw response failed to parse: {locals().get('text', '')}")
    else:
        print("[FAIL] CHECK 4 - JSON output reliability (skipped: API key missing)")

    if token_usage_4 and len(token_usage_4.keys()) > 0:
        check5_ok = True
        print("[PASS] CHECK 5 - Token usage reporting")
    else:
        print("[WARN] CHECK 5 - Token usage reporting")
        print("Token usage not reported by this provider -- tracer token counts will show 0")

    all_passed = check1_ok and check2_ok and check3_ok and check4_ok

    if all_passed:
        print("")
        print("[OK] Setup complete.")
        print("")
        print("Run your first analysis:")
        print("  reposition run https://github.com/you/your-repo --dry-run")
        print("")
        print("To open PRs on a fork instead of the target repo:")
        print("  reposition run https://github.com/someone/repo --pr-repo you/your-fork")
        return

    print("")
    print("[FAIL] Setup incomplete. Fix the issues above")
    print("       and run 'reposition setup' again.")


async def _run_full(repo_path: str) -> None:
    from reposition.config import get_config
    from reposition.graph import run_pipeline

    cfg = get_config()

    console.print(f"[bold]Starting Reposition pipeline for:[/bold] {repo_path}\n")
    stream = run_pipeline(repo_path)
    started = time.monotonic()
    final_state: dict[str, Any] | None = None
    try:
        final_state = await _run_with_live(stream, max_retries=cfg.coder.max_retries, dry_run=False)
        _print_plain_summary(final_state, time.monotonic() - started, dry_run=False)
    except asyncio.CancelledError:
        console.print()
        console.print("[yellow]Run interrupted.[/yellow]")
        rid = final_state.get("run_id") if final_state else "(unknown)"
        console.print(f"[dim]Resume with: reposition resume {rid}[/dim]")
        sys.exit(0)
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Run interrupted.[/yellow]")
        rid = final_state.get("run_id") if final_state else "(unknown)"
        console.print(f"[dim]Resume with: reposition resume {rid}[/dim]")
        sys.exit(0)
    except Exception:
        console.print(f"[red]{traceback.format_exc()}[/red]")
        raise SystemExit(1)


async def _run_dry(repo_path: str) -> None:
    """Run only Scanner -> Analyzers -> Planner and print work packages."""
    from reposition.agents.planner import planner_agent
    from reposition.agents.scanner import scanner_agent
    from reposition.config import get_config
    from reposition.graph import run_analyzers_parallel
    from reposition.observability.tracer import RunTracer
    from reposition.sandbox import E2BSandboxManager
    from reposition.state import make_initial_state

    cfg = get_config()
    state = make_initial_state(repo_path)
    run_id = state.get("run_id")

    trace_dir = Path(".traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    state["trace_path"] = str(trace_dir / f"{state['run_id']}.jsonl")
    RunTracer(state["run_id"], state["trace_path"])

    display_state = _new_display_state(dry_run=True)
    # Ensure display knows this is a dry run before any pipeline execution begins.
    display_state["is_dry_run"] = True
    display_state["dry_run"] = True
    _set_stage_status(display_state, "scanner", "running")
    _set_activity(display_state, "Scanner", "Uploading repo to E2B sandbox...")
    _set_stat(display_state, "Scanner", "RUNNING", "Scanning repository...")

    if run_id:
        console.print(f"[dim]Run ID:  {run_id}[/dim]")
        console.print(f"[dim]Status:  reposition status {run_id}[/dim]")
        console.print()

    console.print(f"[bold]Starting Reposition dry run for:[/bold] {repo_path}\n")
    started = time.monotonic()

    console.print()
    display = TerminalDisplay(console)
    pipeline_start_time = started
    stop_timer = threading.Event()

    def _timer_loop() -> None:
        while not stop_timer.is_set():
            stop_timer.wait(timeout=1.0)
            if stop_timer.is_set():
                break
            try:
                dynamic = build_display_lines(display_state, pipeline_start_time)["dynamic"]
                display.update_lines({1: dynamic[1], 3: dynamic[3]})
            except Exception:
                pass

    timer_thread = threading.Thread(target=_timer_loop, daemon=True)
    timer_thread.start()

    interrupted = False
    try:
        try:
            scanner_update = await scanner_agent(state)
            state.update(scanner_update)
            _apply_event_update(display_state, "scanner", state, scanner_update, cfg.coder.max_retries)
            result = build_display_lines(display_state, pipeline_start_time)
            display.full_render(result["lines"])

            analyzer_update = await run_analyzers_parallel(state)
            state.update(analyzer_update)
            _apply_event_update(display_state, "analyzers", state, analyzer_update, cfg.coder.max_retries)
            result = build_display_lines(display_state, pipeline_start_time)
            display.full_render(result["lines"])

            planner_update = await planner_agent(state)
            state.update(planner_update)
            _apply_event_update(display_state, "planner", state, planner_update, cfg.coder.max_retries)
            result = build_display_lines(display_state, pipeline_start_time)
            display.full_render(result["lines"])
        except KeyboardInterrupt:
            interrupted = True
    except asyncio.CancelledError:
        interrupted = True
    except Exception:
        console.print(f"[red]{traceback.format_exc()}[/red]")
        raise SystemExit(1)
    finally:
        stop_timer.set()
        timer_thread.join(timeout=2.0)
        display.clear()

    if interrupted:
        console.print()
        console.print("[yellow]Run interrupted.[/yellow]")
        if run_id:
            console.print(f"[dim]Resume with: reposition resume {run_id}[/dim]")
        return

    _print_plain_summary(state, time.monotonic() - started, dry_run=True)

    packages = state.get("work_packages", [])
    console.print(f"\n[bold]{len(packages)} work package(s) generated:[/bold]\n")
    console.print_json(json.dumps(packages, indent=2))

    sandbox_id = state.get("e2b_sandbox_id")
    if sandbox_id:
        try:
            mgr = E2BSandboxManager()
            await mgr.close_sandbox(sandbox_id)
        except Exception:
            pass


@cli.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Resume a pipeline from its last checkpoint."""
    # Install SIGINT handler so Ctrl+C cancels asyncio tasks immediately.
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        asyncio.run(_resume(run_id))
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)


async def _resume(run_id: str) -> None:
    from reposition.config import get_config
    from reposition.graph import resume_pipeline

    cfg = get_config()

    console.print(f"[bold]Resuming run:[/bold] {run_id}\n")
    try:
        stream = resume_pipeline(run_id)
        await _run_with_live(stream, max_retries=cfg.coder.max_retries, dry_run=False)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    except asyncio.CancelledError:
        console.print()
        console.print("[yellow]Run interrupted.[/yellow]")
        console.print(f"[dim]Resume with: reposition resume {run_id}[/dim]")
        sys.exit(0)
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Run interrupted.[/yellow]")
        console.print(f"[dim]Resume with: reposition resume {run_id}[/dim]")
        sys.exit(0)
    except Exception:
        console.print(f"[red]{traceback.format_exc()}[/red]")
        raise SystemExit(1)


@cli.command()
@click.argument("run_id")
def status(run_id: str) -> None:
    """Show the status of a pipeline run from its trace file."""
    trace_path = Path(".traces") / f"{run_id}.jsonl"
    if not trace_path.exists():
        console.print(f"[red]No trace file found for run {run_id}[/red]")
        sys.exit(1)

    records: list[dict] = []
    with trace_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if not records:
        console.print("[yellow]Trace file is empty.[/yellow]")
        return

    table = Table(title=f"Trace: {run_id}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Timestamp")
    table.add_column("Agent", style="bold")
    table.add_column("Decision")
    table.add_column("Tokens", justify="right")

    for i, rec in enumerate(records, 1):
        ts = rec.get("timestamp", "")[:19]
        agent = rec.get("agent_name", "")
        decision = rec.get("decision", "")
        usage = rec.get("token_usage")
        tokens = str(usage.get("total_tokens", "")) if usage else "-"

        if "error" in decision.lower() or "fail" in decision.lower() or "abort" in decision.lower():
            decision_text = Text(decision, style="red")
        elif "complete" in decision.lower() or "pass" in decision.lower():
            decision_text = Text(decision, style="green")
        else:
            decision_text = Text(decision)

        table.add_row(str(i), ts, agent, decision_text, tokens)

    console.print(table)

    from reposition.observability.tracer import RunTracer

    tracer = RunTracer(run_id, str(trace_path))
    summary = tracer.summary()
    console.print()
    console.print(f"  Total agents run:    {summary['total_agents_run']}")
    console.print(f"  Total tokens used:   {summary['total_tokens_used']}")
    console.print(f"  Packages attempted:  {summary['packages_attempted']}")
    console.print(f"  Packages passed:     [green]{summary['packages_passed']}[/green]")
    console.print(f"  Packages failed:     [red]{summary['packages_failed']}[/red]")
    console.print()


if __name__ == "__main__":
    cli()

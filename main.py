"""Reposition CLI - entry point for the pipeline."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
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


def _new_display_state(dry_run: bool) -> dict[str, Any]:
    now = time.monotonic()
    return {
        "dry_run": dry_run,
        "start_time": now,
        "current_activity": "[cyan]>[/cyan] Scanner    Initializing pipeline...",
        "stages": {name: {"status": "waiting", "retry": 0} for name in _STAGE_ORDER},
        "analyzer_statuses": {"security": "waiting", "refactor": "waiting", "coverage": "waiting"},
        "stage_times": {name: {"start": None, "end": None} for name in _STAGE_ORDER},
        "stats": {agent: {"status": "-", "detail": "-"} for agent in _STATS_ORDER},
        "summary_ready": False,
        "summary_lines": [],
    }


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
    if node_name == "scanner":
        _set_stage_status(display_state, "scanner", "complete")
        _set_stage_status(display_state, "analyzers", "running")

        manifest = merged_state.get("manifest") or {}
        total_files = manifest.get("total_files", 0)
        excluded = len(merged_state.get("excluded_files", []))
        _set_stat(display_state, "Scanner", "OK", f"{total_files} files, {excluded} secret excluded")
        _set_activity(display_state, "Analyzers", "Running parallel analyzer calls...")
        _set_stat(display_state, "Security", "RUNNING", "Analyzing authentication boundaries...")
        _set_stat(display_state, "Refactor", "RUNNING", "Scanning for SRP violations...")
        _set_stat(display_state, "Coverage", "RUNNING", "Checking uncovered execution paths...")
        for key in ("security", "refactor", "coverage"):
            display_state["analyzer_statuses"][key] = "running"
        return

    if node_name == "analyzers":
        _set_stage_status(display_state, "analyzers", "complete")
        _set_stage_status(display_state, "planner", "running")

        statuses = merged_state.get("analyzer_statuses", {})
        for key in ("security", "refactor", "coverage"):
            display_state["analyzer_statuses"][key] = _status_from_analyzer(statuses.get(key, "COMPLETE"))

        security_report = merged_state.get("security_report", [])
        refactor_report = merged_state.get("refactor_report", [])
        coverage_report = merged_state.get("coverage_report", [])

        _set_stat(display_state, "Security", _status_from_analyzer(statuses.get("security", "COMPLETE")), _security_detail(security_report))
        _set_stat(display_state, "Refactor", _status_from_analyzer(statuses.get("refactor", "COMPLETE")), f"{len(refactor_report)} findings")
        _set_stat(display_state, "Coverage", _status_from_analyzer(statuses.get("coverage", "COMPLETE")), f"{len(coverage_report)} uncovered paths")
        total_findings = len(security_report) + len(refactor_report) + len(coverage_report)
        _set_activity(display_state, "Planner", f"Deduplicating {total_findings} findings into work packages...")
        _set_stat(display_state, "Planner", "RUNNING", "Synthesizing work packages...")
        return

    if node_name == "planner":
        _set_stage_status(display_state, "planner", "complete")
        _set_stage_status(display_state, "coder", "running")

        packages = merged_state.get("work_packages", [])
        planned = len(packages)
        top = packages[0].get("priority_label", "N/A") if packages else "N/A"
        _set_stat(display_state, "Planner", "OK", f"{planned} work packages ({top} first)")
        if planned:
            first_file = (packages[0].get("files_to_modify") or ["-"])[0]
            _set_activity(display_state, "Coder", f"Package 1/{planned} -- Patching {first_file}...")
            _set_stat(display_state, "Coder", "RUNNING", f"Package 1/{planned} -- {first_file}")
        return

    if node_name == "coder":
        total = len(merged_state.get("work_packages", []))
        current = min(int(merged_state.get("current_package_index", 0)) + 1, max(total, 1))
        current_pkg = None
        packages = merged_state.get("work_packages", [])
        idx = int(merged_state.get("current_package_index", 0))
        if 0 <= idx < len(packages):
            current_pkg = packages[idx]

        first_file = (current_pkg.get("files_to_modify") or ["-"])[0] if current_pkg else "-"
        _set_stage_status(display_state, "coder", "complete")
        _set_stage_status(display_state, "validator", "running")
        _set_stat(display_state, "Coder", "OK", f"Package {current}/{max(total, 1)} -- {first_file}")
        _set_stat(display_state, "Validator", "RUNNING", "Running tests in sandbox...")
        _set_activity(display_state, "Validator", "Running tests in sandbox...")
        return

    if node_name == "validator":
        results = merged_state.get("package_results", [])
        verdict = results[-1].get("status") if results else "UNKNOWN"
        total = len(merged_state.get("work_packages", []))
        current = min(int(merged_state.get("current_package_index", 0)) + 1, max(total, 1))

        if verdict in ("PASS", "PASS_NO_TESTS"):
            _set_stage_status(display_state, "validator", "complete")
            _set_stat(display_state, "Validator", "OK", f"{verdict} -- package {current}/{max(total, 1)}")
            _set_activity(display_state, "Validator", f"{verdict} -- committing package {current}...")
            return

        retry_count = int(merged_state.get("retry_count", 0)) + 1
        if verdict in ("FAIL_COMPILE", "FAIL_TEST") and retry_count <= max_retries:
            _set_stage_status(display_state, "validator", "failed")
            _set_stage_status(display_state, "coder", "retrying", retry=retry_count)
            _set_stat(display_state, "Validator", "FAIL", f"{verdict} -- retrying")
            _set_stat(display_state, "Coder", "RUNNING", f"Retry {retry_count}/{max_retries} for package {current}")
            _set_activity(display_state, "Coder", f"Retry {retry_count}/{max_retries} -- regenerating package {current}...")
        else:
            _set_stage_status(display_state, "validator", "failed")
            _set_stage_status(display_state, "coder", "failed")
            _set_stat(display_state, "Validator", "FAIL", str(verdict))
            _set_stat(display_state, "Coder", "FAIL", f"Retries exhausted for package {current}")
            _set_activity(display_state, "Validator", f"{verdict} -- retries exhausted")
        return

    if node_name in ("advance_package", "abort_package"):
        total = len(merged_state.get("work_packages", []))
        idx = int(merged_state.get("current_package_index", 0))
        if node_name == "abort_package":
            _set_stat(display_state, "Validator", "FAIL", "ABORTED -- max retries exhausted")
        if idx < total:
            _set_stage_status(display_state, "validator", "complete")
            _set_stage_status(display_state, "coder", "running")
            next_file = (merged_state.get("work_packages", [])[idx].get("files_to_modify") or ["-"])[0]
            _set_stat(display_state, "Coder", "RUNNING", f"Package {idx + 1}/{total} -- {next_file}")
            _set_activity(display_state, "Coder", f"Package {idx + 1}/{total} -- Patching {next_file}...")
        else:
            _set_stage_status(display_state, "pr_agent", "running")
            _set_stat(display_state, "PR Agent", "RUNNING", "Generating commit messages...")
            _set_activity(display_state, "PR Agent", "Generating commit messages...")
        return

    if node_name == "pr_agent":
        _set_stage_status(display_state, "pr_agent", "complete")
        pr_number = merged_state.get("pr_number")
        finding = f"PR #{pr_number}" if pr_number else "PR #unknown"
        _set_stat(display_state, "PR Agent", "OK", finding)
        _set_activity(display_state, "PR Agent", "Opening pull request... complete")


async def _run_with_live(stream, max_retries: int, dry_run: bool = False) -> dict[str, Any]:
    display_state = _new_display_state(dry_run)
    _set_stage_status(display_state, "scanner", "running")
    _set_activity(display_state, "Scanner", "Uploading repo to E2B sandbox...")
    _set_stat(display_state, "Scanner", "RUNNING", "Scanning repository...")

    final_state: dict[str, Any] = {}
    interrupted = False

    done = asyncio.Event()

    async def _consume_stream() -> None:
        nonlocal interrupted
        try:
            async for event in stream:
                for node_name, update in event.items():
                    if not isinstance(update, dict):
                        continue
                    final_state.update(update)
                    _apply_event_update(display_state, node_name, final_state, update, max_retries)
        except KeyboardInterrupt:
            interrupted = True
        finally:
            done.set()

    consumer = asyncio.create_task(_consume_stream())

    with Live(_build_renderables(display_state), console=console, refresh_per_second=2) as live:
        while not done.is_set():
            live.update(_build_renderables(display_state))
            await asyncio.sleep(0.5)

        await consumer

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
        console.print()
        console.print("[bold cyan]Run complete[/bold cyan]")
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

    console.print()
    console.print("[bold cyan]Run complete[/bold cyan]")
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
        console.print(LOGO)
        _logo_shown = True


@cli.command()
@click.argument("repo_path")
@click.option("--config", "config_path", default=None, help="Path to config YAML.")
@click.option("--dry-run", is_flag=True, help="Run Scanner + Analyzers + Planner only.")
@click.option(
    "--clone-dir",
    default=None,
    help="Where to clone the repo (default: ~/.reposition/repos/).",
)
def run(repo_path: str, config_path: str | None, dry_run: bool, clone_dir: str | None) -> None:
    """Run the Reposition pipeline on a repository."""
    if config_path:
        from reposition.config import load_config
        import reposition.config as _cfg_mod

        _cfg_mod._singleton = load_config(config_path)

    from reposition.config import get_config
    from reposition.graph import resolve_repo_path

    cfg = get_config()
    default_clone_root = cfg.github.clone_dir

    try:
        resolved_repo_path = resolve_repo_path(
            repo_path=repo_path,
            clone_dir=clone_dir,
            default_clone_root=default_clone_root,
        )
    except RuntimeError as exc:
        console.print(f"[red]Repository resolution error:[/red] {exc}")
        raise SystemExit(1)

    if repo_path.startswith("https://github.com/") or repo_path.startswith("git@github.com:"):
        console.print(f"[bold]Clone destination:[/bold] {resolved_repo_path}")

    # Preflight: verify LLM provider + API key before starting the pipeline
    from reposition.llm_client import get_llm

    try:
        get_llm("fast")
        get_llm("heavy")
    except (ValueError, EnvironmentError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise SystemExit(1)

    if dry_run:
        try:
            asyncio.run(_run_dry(resolved_repo_path))
        except KeyboardInterrupt:
            console.print("Run interrupted")
    else:
        try:
            asyncio.run(_run_full(resolved_repo_path))
        except KeyboardInterrupt:
            console.print("Run interrupted")


async def _run_full(repo_path: str) -> None:
    from reposition.config import get_config
    from reposition.graph import run_pipeline

    cfg = get_config()

    console.print(f"[bold]Starting Reposition pipeline for:[/bold] {repo_path}\n")
    stream = run_pipeline(repo_path)
    started = time.monotonic()
    try:
        final_state = await _run_with_live(stream, max_retries=cfg.coder.max_retries, dry_run=False)
        _print_plain_summary(final_state, time.monotonic() - started, dry_run=False)
    except KeyboardInterrupt:
        console.print("Run interrupted")
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

    trace_dir = Path(".traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    state["trace_path"] = str(trace_dir / f"{state['run_id']}.jsonl")
    RunTracer(state["run_id"], state["trace_path"])

    display_state = _new_display_state(dry_run=True)
    _set_stage_status(display_state, "scanner", "running")
    _set_activity(display_state, "Scanner", "Uploading repo to E2B sandbox...")
    _set_stat(display_state, "Scanner", "RUNNING", "Scanning repository...")

    console.print(f"[bold]Starting Reposition dry run for:[/bold] {repo_path}\n")
    started = time.monotonic()

    interrupted = False
    try:
        with Live(_build_renderables(display_state), console=console, refresh_per_second=2) as live:
            try:
                scanner_update = await scanner_agent(state)
                state.update(scanner_update)
                _apply_event_update(display_state, "scanner", state, scanner_update, cfg.coder.max_retries)
                live.update(_build_renderables(display_state))

                analyzer_update = await run_analyzers_parallel(state)
                state.update(analyzer_update)
                _apply_event_update(display_state, "analyzers", state, analyzer_update, cfg.coder.max_retries)
                live.update(_build_renderables(display_state))

                planner_update = await planner_agent(state)
                state.update(planner_update)
                _apply_event_update(display_state, "planner", state, planner_update, cfg.coder.max_retries)
            except KeyboardInterrupt:
                interrupted = True
    except Exception:
        console.print(f"[red]{traceback.format_exc()}[/red]")
        raise SystemExit(1)

    if interrupted:
        console.print("Run interrupted")

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
    asyncio.run(_resume(run_id))


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
    except KeyboardInterrupt:
        console.print("Run interrupted")
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

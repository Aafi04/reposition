"""Reposition CLI - entry point for the pipeline."""

from __future__ import annotations

import asyncio
import json
import getpass
import importlib.util
import os
import subprocess
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
        try:
            asyncio.run(_run_dry(resolved_repo_path))
        except KeyboardInterrupt:
            console.print("Run interrupted")
    else:
        try:
            asyncio.run(_run_full(resolved_repo_path))
        except KeyboardInterrupt:
            console.print("Run interrupted")


@cli.command()
def setup() -> None:
    """Run an interactive one-time setup wizard."""
    project_root = Path(__file__).resolve().parent
    env_path = project_root / ".env"

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

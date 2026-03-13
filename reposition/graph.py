"""LangGraph graph construction for the Reposition pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from reposition.agents.coder import coder_agent
from reposition.agents.coverage_analyzer import coverage_analyzer_agent
from reposition.agents.planner import planner_agent
from reposition.agents.pr_agent import pr_agent
from reposition.agents.refactor_analyzer import refactor_analyzer_agent
from reposition.agents.scanner import scanner_agent
from reposition.agents.security_analyzer import security_analyzer_agent
from reposition.agents.validator import validator_agent
from reposition.config import get_config
from reposition.observability.tracer import RunTracer
from reposition.sandbox import E2BSandboxManager
from reposition.state import RepositionState, make_initial_state
from reposition.tools.github_tools import normalize_repo


def _is_github_url(repo_path: str) -> bool:
    try:
        normalize_repo(repo_path)
        return True
    except ValueError:
        return False


def _repo_name_from_url(url: str) -> str:
    return normalize_repo(url)["repo"]


def resolve_repo_path(
    repo_path: str,
    clone_dir: str | None = None,
    default_clone_root: str = "~/.reposition/repos",
) -> str:
    """Resolve repo_path to a local directory, cloning/pulling if a GitHub URL is provided."""
    if not _is_github_url(repo_path):
        return str(Path(repo_path).expanduser().resolve())

    normalized = normalize_repo(repo_path)
    clone_url = normalized["clone_url"]

    if clone_dir:
        destination = Path(clone_dir).expanduser()
    else:
        destination = Path(default_clone_root).expanduser() / _repo_name_from_url(clone_url)

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        if destination.exists():
            if not (destination / ".git").exists():
                raise RuntimeError(
                    f"Destination exists but is not a git repo: {destination}. "
                    "Please delete this directory manually and retry."
                )

            repo = Repo(str(destination))
            repo.remotes.origin.pull()
        else:
            Repo.clone_from(clone_url, str(destination))
    except InvalidGitRepositoryError as exc:
        raise RuntimeError(
            f"Destination exists but is not a git repo: {destination}. "
            "Please delete this directory manually and retry."
        ) from exc
    except GitCommandError as exc:
        msg = str(exc)
        lower = msg.lower()
        if "repository not found" in lower or "not found" in lower:
            raise RuntimeError(
                f"Repository not found: {clone_url}. Verify the URL and try again."
            ) from exc
        if "permission denied" in lower or "authentication failed" in lower or "access denied" in lower:
            raise RuntimeError(
                "No permission to clone/pull this repository. "
                "If it is private, verify your credentials and that GITHUB_TOKEN has access."
            ) from exc
        raise RuntimeError(f"Git operation failed for {clone_url}: {msg}") from exc

    return str(destination.resolve())


# ── node functions ──────────────────────────────────────────────────────


async def run_analyzers_parallel(state: RepositionState) -> dict:
    """Run all three analyzer agents concurrently with per-agent timeouts.

    Each analyzer that times out gets an empty report and ``TIMED_OUT`` status.
    Results are merged into a single dict suitable as a LangGraph state update.
    """
    cfg = get_config()
    timeout = cfg.analyzers.timeout_seconds

    async def _run_with_timeout(
        coro,
        report_key: str,
        status_key: str,
    ) -> dict:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            return {
                report_key: [],
                "analyzer_statuses": {
                    **state["analyzer_statuses"],
                    status_key: "TIMED_OUT",
                },
            }

    security_task = _run_with_timeout(
        security_analyzer_agent(state), "security_report", "security",
    )
    refactor_task = _run_with_timeout(
        refactor_analyzer_agent(state), "refactor_report", "refactor",
    )
    coverage_task = _run_with_timeout(
        coverage_analyzer_agent(state), "coverage_report", "coverage",
    )

    results = await asyncio.gather(security_task, refactor_task, coverage_task)

    # Merge all result dicts; analyzer_statuses are merged key-by-key.
    merged: dict = {}
    merged_statuses: dict[str, str] = dict(state["analyzer_statuses"])
    for result in results:
        statuses = result.pop("analyzer_statuses", {})
        merged_statuses.update(statuses)
        merged.update(result)
    merged["analyzer_statuses"] = merged_statuses

    return merged


async def _run_single_package_with_retries(
    work_package: dict,
    state: RepositionState,
    retry_counters: dict[str, int],
    max_retries: int,
    package_index: int,
) -> dict:
    """Run coder+validator for one package with isolated retries."""
    package_id = work_package["id"]
    last_result: dict | None = None

    while True:
        try:
            package_state: RepositionState = {
                **state,
                "current_package_index": package_index,
                "retry_count": retry_counters[package_id],
                "package_results": [last_result] if last_result else [],
                "current_patch": None,
            }

            coder_update = await coder_agent(package_state)
            package_state = {**package_state, **coder_update}

            validator_update = await validator_agent(package_state)
            package_result_list = validator_update.get("package_results", [])
            if not package_result_list:
                return {
                    "package_id": package_id,
                    "status": "ABORTED",
                    "verdict_detail": "validator returned no package_result",
                }

            last_result = package_result_list[-1]
            status = last_result.get("status")

            if status in ("PASS", "PASS_NO_TESTS"):
                return last_result

            if status in ("FAIL_COMPILE", "FAIL_TEST") and retry_counters[package_id] < max_retries:
                retry_counters[package_id] += 1
                continue

            if status in ("FAIL_COMPILE", "FAIL_TEST"):
                return {
                    "package_id": package_id,
                    "status": "ABORTED",
                    "verdict_detail": "max retries exhausted",
                }

            return last_result
        except Exception as exc:
            return {
                "package_id": package_id,
                "status": "ABORTED",
                "verdict_detail": f"package execution error: {exc}",
            }


async def run_package_batch(packages: list[dict], state: RepositionState) -> list[dict]:
    """Run a batch of work packages concurrently and return package results."""
    cfg = get_config()
    package_index_by_id = {pkg["id"]: idx for idx, pkg in enumerate(state["work_packages"])}
    retry_counters: dict[str, int] = {pkg["id"]: 0 for pkg in packages}

    tasks = [
        _run_single_package_with_retries(
            work_package=pkg,
            state=state,
            retry_counters=retry_counters,
            max_retries=cfg.coder.max_retries,
            package_index=package_index_by_id[pkg["id"]],
        )
        for pkg in packages
    ]

    return await asyncio.gather(*tasks)


async def package_scheduler_node(state: RepositionState) -> dict:
    """Schedule non-conflicting packages and execute a concurrent batch."""
    cfg = get_config()
    max_concurrent = max(1, cfg.planner.max_concurrent_packages)
    work_packages = state["work_packages"]
    work_package_by_id = {pkg["id"]: pkg for pkg in work_packages}

    completed_ids = {res.get("package_id") for res in state["package_results"]}
    active_ids = [pkg_id for pkg_id in state["active_package_ids"] if pkg_id in work_package_by_id]
    pending_ids = [
        pkg["id"]
        for pkg in work_packages
        if pkg["id"] not in completed_ids and pkg["id"] not in active_ids
    ]

    available_slots = max(0, max_concurrent - len(active_ids))

    active_files: set[str] = set()
    for pkg_id in active_ids:
        active_files.update(work_package_by_id[pkg_id].get("files_to_modify", []))

    batch: list[dict] = []
    batch_files: set[str] = set()
    if available_slots > 0:
        for pkg in work_packages:
            pkg_id = pkg["id"]
            if pkg_id not in pending_ids:
                continue
            pkg_files = set(pkg.get("files_to_modify", []))
            if pkg_files.intersection(active_files) or pkg_files.intersection(batch_files):
                continue
            batch.append(pkg)
            batch_files.update(pkg_files)
            if len(batch) >= available_slots:
                break

    if not batch:
        tracer = RunTracer(state["run_id"], state["trace_path"])
        if not active_ids:
            tracer.log(
                agent_name="package_scheduler",
                decision="all_packages_complete",
                output={"completed": len(completed_ids), "total": len(work_packages)},
            )
            return {
                "active_package_ids": [],
                "pending_package_ids": [],
            }
        tracer.log(
            agent_name="package_scheduler",
            decision="waiting_for_active_packages",
            output={"active_package_ids": active_ids},
        )
        return {
            "active_package_ids": active_ids,
            "pending_package_ids": pending_ids,
        }

    batch_ids = [pkg["id"] for pkg in batch]
    active_ids = [*active_ids, *batch_ids]

    tracer = RunTracer(state["run_id"], state["trace_path"])
    tracer.log(
        agent_name="package_scheduler",
        decision="batch_started",
        output={"batch_package_ids": batch_ids},
    )

    batch_results = await run_package_batch(batch, {**state, "active_package_ids": active_ids})

    merged_by_id: dict[str, dict] = {
        result["package_id"]: result
        for result in state["package_results"]
        if result.get("package_id")
    }
    for result in batch_results:
        if result.get("package_id"):
            merged_by_id[result["package_id"]] = result

    remaining_active_ids = [pkg_id for pkg_id in active_ids if pkg_id not in batch_ids]
    merged_results = [
        merged_by_id[pkg["id"]]
        for pkg in work_packages
        if pkg["id"] in merged_by_id
    ]
    completed_after_batch = {res.get("package_id") for res in merged_results}
    pending_after_batch = [
        pkg["id"]
        for pkg in work_packages
        if pkg["id"] not in completed_after_batch and pkg["id"] not in remaining_active_ids
    ]

    tracer.log(
        agent_name="package_scheduler",
        decision="batch_complete",
        output={
            "batch_package_ids": batch_ids,
            "completed_count": len(completed_after_batch),
            "pending_count": len(pending_after_batch),
        },
    )

    return {
        "package_results": merged_results,
        "active_package_ids": remaining_active_ids,
        "pending_package_ids": pending_after_batch,
        "current_patch": None,
        "retry_count": 0,
    }


async def wait_node(state: RepositionState) -> dict:
    """Tiny backoff when scheduler has active work in flight."""
    await asyncio.sleep(0.05)
    return {}


def route_after_package_scheduler(state: RepositionState) -> str:
    """Route after each scheduler cycle."""
    if not state["pending_package_ids"] and not state["active_package_ids"]:
        return "pr_agent"
    if state["active_package_ids"] and not state["pending_package_ids"]:
        return "wait_node"
    return "package_scheduler"


def route_after_planner(state: RepositionState) -> str:
    """Route after planner based on dry-run mode."""
    if state.get("dry_run", False):
        return "end"
    return "package_scheduler"


# ── graph builder ───────────────────────────────────────────────────────


def build_graph(checkpointer: AsyncSqliteSaver) -> StateGraph:
    """Construct and compile the full Reposition StateGraph."""
    builder = StateGraph(RepositionState)

    # Nodes
    builder.add_node("scanner", scanner_agent)
    builder.add_node("analyzers", run_analyzers_parallel)
    builder.add_node("planner", planner_agent)
    builder.add_node("package_scheduler", package_scheduler_node)
    builder.add_node("wait_node", wait_node)
    builder.add_node("pr_agent", pr_agent)

    # Edges: linear pipeline
    builder.add_edge(START, "scanner")
    builder.add_edge("scanner", "analyzers")
    builder.add_edge("analyzers", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "package_scheduler": "package_scheduler",
            "end": END,
        },
    )

    builder.add_conditional_edges(
        "package_scheduler",
        route_after_package_scheduler,
        {
            "package_scheduler": "package_scheduler",
            "wait_node": "wait_node",
            "pr_agent": "pr_agent",
        },
    )

    builder.add_edge("wait_node", "package_scheduler")

    # PR agent → END
    builder.add_edge("pr_agent", END)

    return builder.compile(checkpointer=checkpointer)


# ── public API ──────────────────────────────────────────────────────────


async def run_pipeline(
    repo_path: str,
    config_override: dict | None = None,
    dry_run: bool = False,
) -> AsyncIterator[dict]:
    """Run the full Reposition pipeline, yielding state updates as they arrive.

    Parameters
    ----------
    repo_path:
        Local path or GitHub URL of the target repository.
    config_override:
        Optional dict of config overrides (currently unused — reserved).

    Yields
    ------
    dict
        Partial state updates from each graph node.
    """
    if _is_github_url(repo_path):
        cfg = get_config()
        repo_path = resolve_repo_path(repo_path, default_clone_root=cfg.github.clone_dir)

    state = make_initial_state(repo_path)
    state["dry_run"] = dry_run
    run_id = state["run_id"]

    # Set up trace path
    trace_dir = Path(".traces")
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = str(trace_dir / f"{run_id}.jsonl")
    state["trace_path"] = trace_path

    # Initialise tracer
    RunTracer(run_id, trace_path)

    # Set up checkpointer
    checkpoint_dir = Path(".checkpoints")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(checkpoint_dir / f"{run_id}.db")

    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": state["thread_id"]}}
        sandbox_id: str | None = None

        try:
            async for event in graph.astream(state, config=config):
                # event is {node_name: state_update_dict}
                for node_name, update in event.items():
                    if isinstance(update, dict) and "e2b_sandbox_id" in update:
                        sandbox_id = update["e2b_sandbox_id"]
                yield event
        finally:
            if sandbox_id:
                try:
                    sandbox_mgr = E2BSandboxManager()
                    await sandbox_mgr.close_sandbox(sandbox_id)
                except Exception:
                    pass


async def resume_pipeline(run_id: str) -> AsyncIterator[dict]:
    """Resume a pipeline from its last checkpoint.

    Parameters
    ----------
    run_id:
        The UUID of the run to resume.

    Yields
    ------
    dict
        Partial state updates from each graph node.
    """
    checkpoint_dir = Path(".checkpoints")
    db_path = str(checkpoint_dir / f"{run_id}.db")

    if not Path(db_path).exists():
        raise FileNotFoundError(f"No checkpoint found for run {run_id}")

    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = build_graph(checkpointer)
        config = {"configurable": {"thread_id": run_id}}
        sandbox_id: str | None = None

        try:
            async for event in graph.astream(None, config=config):
                for node_name, update in event.items():
                    if isinstance(update, dict) and "e2b_sandbox_id" in update:
                        sandbox_id = update["e2b_sandbox_id"]
                yield event
        finally:
            if sandbox_id:
                try:
                    sandbox_mgr = E2BSandboxManager()
                    await sandbox_mgr.close_sandbox(sandbox_id)
                except Exception:
                    pass

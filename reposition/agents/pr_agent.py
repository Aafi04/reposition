"""PR agent – opens or updates a pull request with all passing work packages."""

from __future__ import annotations

import json
import os
import re
import time

from reposition.config import get_config
from reposition.llm_client import call_llm, get_llm
from reposition.observability.tracer import RunTracer
from reposition.sandbox import E2BSandboxManager
from reposition.state import RepositionState
from reposition.tools.github_tools import GitHubClient

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

PR_DESCRIPTION_SYSTEM_PROMPT = """\
Generate a pull request description in Markdown.

Rules:
- No hyperbolic language ("dramatically", "massively", "revolutionizes", "significantly")
- Every claim must be traceable to a specific file change
- Structure exactly:
  ## Summary
  (2-3 sentences describing what changed and why)
  ## Changes
  | File | Change | Source |
  |------|--------|--------|
  (one row per modified file, Source = [Security], [Refactor], or [Tests])
  ## Testing
  (what the validator ran and what passed)
- If any packages were ABORTED, add a ## Known Limitations section listing them
"""

MCI_SYSTEM_PROMPT = (
    "You are a PR consistency checker. Respond ONLY with valid JSON."
)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


async def pr_agent(state: RepositionState) -> dict:
    """LangGraph node: push changes and open a pull request."""
    cfg = get_config()
    tracer = RunTracer(state["run_id"], state["trace_path"])
    sandbox = E2BSandboxManager()
    sandbox_id = state["e2b_sandbox_id"]
    assert sandbox_id is not None

    llm = get_llm("fast")

    # ── Step 1 — Determine branch ────────────────────────────────────
    branch_name = f"reposition/{int(time.time())}-{state['run_id'][:8]}"

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    gh = GitHubClient(github_token=github_token, repo_full_name=github_repo)

    existing = gh.find_existing_reposition_pr()
    if existing is not None:
        branch_name = existing["head_branch"]

    try:
        all_packages = state["work_packages"]
        results_by_id = {r["package_id"]: r for r in state["package_results"]}

        passing_packages: list[dict] = []
        aborted_packages: list[dict] = []
        for pkg in all_packages:
            result = results_by_id.get(pkg["id"])
            if result and result["status"] in ("PASS", "PASS_NO_TESTS"):
                passing_packages.append(pkg)
            elif result and result["status"] == "ABORTED":
                aborted_packages.append(pkg)

        tracer.log("pr_agent", "started", {"package_count": len(passing_packages)})

        # ── Step 2 — Generate commit messages (single batched call) ──
        tracer.log("pr_agent", "generating_commits", {})
        commit_messages: list[str] = []
        if passing_packages:
            user_message = json.dumps(
                {
                    "task": (
                        "Generate one semantic commit message per work package. "
                        "Return a JSON array of strings, one per package, in the same order. "
                        "Max 72 chars each. Format: type(scope): description"
                    ),
                    "packages": passing_packages,
                }
            )
            text, _ = call_llm(llm, MCI_SYSTEM_PROMPT, user_message)
            raw = _strip_fences(text)
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                commit_messages = [str(msg).strip() for msg in parsed]

        if len(commit_messages) != len(passing_packages):
            # Fallback guarantees one message per package and stable ordering.
            commit_messages = [
                f"chore(reposition): apply {pkg['id']}"[:72]
                for pkg in passing_packages
            ]

        if not commit_messages:
            commit_messages = ["reposition: automated improvements"]

        # ── Step 3 — Generate PR description ─────────────────────────
        tracer.log("pr_agent", "generating_description", {})
        pr_desc_user = json.dumps(
            {
                "passing_packages": passing_packages,
                "aborted_packages": aborted_packages,
                "commit_messages": commit_messages,
            }
        )

        pr_desc_text, _ = call_llm(llm, PR_DESCRIPTION_SYSTEM_PROMPT, pr_desc_user)
        pr_description = pr_desc_text.strip()

        # ── Step 4 — MCI Check (max 2 attempts) ──────────────────────
        modified_files_info: list[dict] = []
        for pkg in passing_packages:
            for f in pkg.get("files_to_modify", []):
                modified_files_info.append(
                    {
                        "file": f,
                        "work_package_id": pkg["id"],
                        "description": pkg.get("issue_description", ""),
                    }
                )

        for attempt in range(2):
            mci_user = (
                f"PR DESCRIPTION:\n{pr_description}\n\n"
                f"FILES CHANGED:\n{json.dumps(modified_files_info, indent=2)}\n\n"
                "Does every claim in the PR description map to a specific file change listed above?\n"
                'Respond ONLY with: {"consistent": true/false, "inconsistencies": ["..."]}'
            )
            mci_text, _ = call_llm(llm, MCI_SYSTEM_PROMPT, mci_user)
            mci_raw = _strip_fences(mci_text)
            try:
                mci_result = json.loads(mci_raw)
            except json.JSONDecodeError:
                break

            if mci_result.get("consistent", True):
                break

            if attempt < 1:
                inconsistencies = mci_result.get("inconsistencies", [])
                regen_user = (
                    f"Previous PR description had these inconsistencies:\n"
                    f"{json.dumps(inconsistencies)}\n\n"
                    f"Original data:\n{pr_desc_user}\n\n"
                    "Regenerate a corrected PR description that fixes these issues."
                )
                regen_text, _ = call_llm(llm, PR_DESCRIPTION_SYSTEM_PROMPT, regen_user)
                pr_description = regen_text.strip()

        # ── Step 5 — Push and open PR ────────────────────────────────
        tracer.log("pr_agent", "pushing_files", {})
        gh.create_branch(branch_name, cfg.github.base_branch)

        await gh.push_files_from_sandbox(
            sandbox_manager=sandbox,
            sandbox_id=sandbox_id,
            branch_name=branch_name,
            commit_message=commit_messages[0],
        )

        diff_stats = gh.get_diff_stats(branch_name, cfg.github.base_branch)
        files_changed = diff_stats["files_changed"]
        total_lines = diff_stats["lines_added"] + diff_stats["lines_deleted"]

        draft = (
            files_changed > cfg.pr_agent.max_diff_files
            or total_lines > cfg.pr_agent.max_diff_lines
        )

        pr_title = commit_messages[0][:72]

        tracer.log("pr_agent", "opening_pr", {})
        pr_result = gh.create_pull_request(
            title=pr_title,
            body=pr_description,
            head=branch_name,
            base=cfg.github.base_branch,
            draft=draft,
        )

        if draft:
            gh.add_pr_comment(
                pr_result["number"],
                f"\u26a0\ufe0f Large diff detected ({files_changed} files, "
                f"{total_lines} lines). Review carefully before merging.",
            )

        trace_summary = tracer.summary()
        gh.add_pr_comment(
            pr_result["number"],
            (
                "**Reposition Trace Summary**\n\n"
                f"- Agents run: {trace_summary['total_agents_run']}\n"
                f"- Total tokens: {trace_summary['total_tokens_used']}\n"
                f"- Packages attempted: {trace_summary['packages_attempted']}\n"
                f"- Packages passed: {trace_summary['packages_passed']}\n"
                f"- Packages failed: {trace_summary['packages_failed']}\n"
            ),
        )

        if pr_result.get("already_existed"):
            tracer.log(
                "pr_agent",
                "pr_already_existed",
                {"pr_url": pr_result["html_url"]},
            )
        else:
            tracer.log(
                "pr_agent",
                "pr_created",
                {"pr_url": pr_result["html_url"]},
            )

        tracer.log(
            agent_name="pr_agent",
            decision="pr_complete",
            output={
                "pr_number": pr_result["number"],
                "branch": branch_name,
                "draft": draft,
                "files_changed": files_changed,
                "already_existed": bool(pr_result.get("already_existed")),
            },
        )

        return {
            "pr_url": pr_result["html_url"],
            "pr_number": pr_result["number"],
            "pr_branch": branch_name,
        }
    except Exception as exc:
        tracer.log(
            agent_name="pr_agent",
            decision="failed",
            output={"error": repr(exc)},
        )
        raise

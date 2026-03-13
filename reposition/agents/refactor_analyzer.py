"""Refactor analyzer agent – identifies structural code quality issues via Claude."""

from __future__ import annotations

import json
import re
from pathlib import Path

from reposition.config import get_config
from reposition.llm_client import call_llm, get_llm
from reposition.observability.tracer import RunTracer
from reposition.state import RepositionState
from reposition.tools.file_ranker import rank_files_for_refactor

REFACTOR_SYSTEM_PROMPT = """\
You are a structural code quality engine. Analyze the repository for refactoring opportunities.

Output ONLY a valid JSON array. Each element must be:
{
  "type": "DUPLICATION|SRP_VIOLATION|HIGH_COMPLEXITY|GOD_CLASS|LONG_METHOD",
  "severity": "HIGH|MEDIUM|LOW",
  "files": ["relative/path"],
  "description": "One sentence: what the structural problem is",
  "expected_improvement": "One sentence: measurable outcome of the fix"
}

CRITICAL CONSTRAINT: Every proposed refactor must preserve exact input/output behavior
and all public API signatures. If a fix would require changing any exported function
signature, method name, or module interface — DO NOT include it.

If no refactor opportunities found, return [].
"""

_REQUIRED_KEYS = {"type", "severity", "files", "description", "expected_improvement"}
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _build_user_message(state: RepositionState) -> str:
    cfg = get_config()
    estimated = len(json.dumps(state["manifest"])) // 4 if state["manifest"] else 0
    manifest = (
        state["manifest_compressed"]
        if estimated > cfg.scanner.max_manifest_tokens and state["manifest_compressed"]
        else state["manifest"]
    )
    manifest_message = (
        "Repository manifest:\n"
        + json.dumps(manifest, indent=2)
    )

    selected_files = rank_files_for_refactor(manifest if isinstance(manifest, dict) else {})
    file_meta = {
        entry.get("path"): entry
        for entry in (manifest.get("files", []) if isinstance(manifest, dict) else [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    repo_root = Path(state["repo_path"])

    prompt_files: list[dict] = []
    for rel_path in selected_files:
        entry = file_meta.get(rel_path, {"path": rel_path})
        payload = {
            "path": rel_path,
            "language": entry.get("language"),
            "line_count": entry.get("line_count"),
            "is_entry_point": entry.get("is_entry_point", False),
            "imports": entry.get("imports", []),
            "declarations": entry.get("declarations", []),
            "mode": "full_content",
        }
        try:
            payload["content"] = (repo_root / rel_path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            payload["content"] = ""
        prompt_files.append(payload)

    return (
        manifest_message
        + "\n\nRanked refactor-relevant files:\n"
        + json.dumps(prompt_files, indent=2)
        + "\n\nAnalyze the above repository for refactoring opportunities."
    )


async def refactor_analyzer_agent(state: RepositionState) -> dict:
    """LangGraph node: produce a structural refactor report."""
    tracer = RunTracer(state["run_id"], state["trace_path"])
    try:
        llm = get_llm("heavy", max_tokens=4096)
        user_message = _build_user_message(state)

        raw_text, token_usage = call_llm(llm, REFACTOR_SYSTEM_PROMPT, user_message)

        cleaned = _strip_fences(raw_text)
        items: list[dict] = json.loads(cleaned)

        valid_items = [
            item for item in items
            if isinstance(item, dict) and _REQUIRED_KEYS.issubset(item.keys())
        ]

        tracer.log(
            agent_name="refactor_analyzer",
            decision="analysis_complete",
            output={"findings_count": len(valid_items)},
            token_usage=token_usage,
        )

        return {
            "refactor_report": valid_items,
            "analyzer_statuses": {**state["analyzer_statuses"], "refactor": "COMPLETE"},
        }

    except Exception:
        tracer.log(
            agent_name="refactor_analyzer",
            decision="analysis_error",
            output={},
        )
        return {
            "refactor_report": [],
            "analyzer_statuses": {**state["analyzer_statuses"], "refactor": "ERROR"},
        }

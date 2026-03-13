"""Security analyzer agent – identifies vulnerabilities via Claude."""

from __future__ import annotations

import json
import re
from pathlib import Path

from reposition.config import get_config
from reposition.llm_client import call_llm, get_llm
from reposition.observability.tracer import RunTracer
from reposition.state import RepositionState
from reposition.tools.file_ranker import rank_files_for_security

SECURITY_SYSTEM_PROMPT = """\
You are a security analysis engine. You receive a repository manifest and file contents.
Your task: identify every security vulnerability with surgical precision.

Output ONLY a valid JSON array. Each element must be:
{
  "cwe_id": "CWE-XXX",
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "file": "relative/path/to/file",
  "line_range": [start_line, end_line],
  "description": "One sentence: what is vulnerable and why",
  "remediation": "One sentence: exact code change required"
}

Rules:
- Only report vulnerabilities with evidence from the provided code
- Never hallucinate line numbers — if uncertain, set line_range to null
- CWE IDs must be real and relevant (CWE-79 for XSS, CWE-89 for SQLi, etc.)
- Do not include informational findings — minimum severity is LOW
- If no vulnerabilities found, return an empty array []
"""

_REQUIRED_KEYS = {"cwe_id", "severity", "file", "description", "remediation"}
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences wrapping a JSON payload."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _build_user_message(state: RepositionState) -> str:
    """Build the user prompt from the manifest and relevant file contents."""
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

    selected_files = rank_files_for_security(manifest if isinstance(manifest, dict) else {})
    file_meta = {
        entry.get("path"): entry
        for entry in (manifest.get("files", []) if isinstance(manifest, dict) else [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    repo_root = Path(state["repo_path"])

    prompt_files: list[dict] = []
    for idx, rel_path in enumerate(selected_files):
        entry = file_meta.get(rel_path, {"path": rel_path})
        file_payload = {
            "path": rel_path,
            "language": entry.get("language"),
            "line_count": entry.get("line_count"),
            "is_entry_point": entry.get("is_entry_point", False),
            "imports": entry.get("imports", []),
            "declarations": entry.get("declarations", []),
            "mode": "full_content" if idx < 10 else "ast_summary",
        }
        if idx < 10:
            try:
                file_payload["content"] = (repo_root / rel_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                file_payload["content"] = ""
        prompt_files.append(file_payload)

    return (
        manifest_message
        + "\n\nRanked security-relevant files (top 10 full content; 11-20 AST summary):\n"
        + json.dumps(prompt_files, indent=2)
        + "\n\nAnalyze the above repository for security vulnerabilities."
    )


async def security_analyzer_agent(state: RepositionState) -> dict:
    """LangGraph node: produce a security vulnerability report."""
    tracer = RunTracer(state["run_id"], state["trace_path"])
    try:
        llm = get_llm("heavy", max_tokens=4096)
        user_message = _build_user_message(state)

        raw_text, token_usage = call_llm(llm, SECURITY_SYSTEM_PROMPT, user_message)

        cleaned = _strip_fences(raw_text)
        items: list[dict] = json.loads(cleaned)

        valid_items = [
            item for item in items
            if isinstance(item, dict) and _REQUIRED_KEYS.issubset(item.keys())
        ]

        tracer.log(
            agent_name="security_analyzer",
            decision="analysis_complete",
            output={"findings_count": len(valid_items)},
            token_usage=token_usage,
        )

        return {
            "security_report": valid_items,
            "analyzer_statuses": {**state["analyzer_statuses"], "security": "COMPLETE"},
        }

    except Exception:
        tracer.log(
            agent_name="security_analyzer",
            decision="analysis_error",
            output={},
        )
        return {
            "security_report": [],
            "analyzer_statuses": {**state["analyzer_statuses"], "security": "ERROR"},
        }

"""Coder agent – produces patches for individual work packages."""

from __future__ import annotations

import json
import re

from reposition.config import get_config
from reposition.llm_client import call_llm, get_llm
from reposition.observability.tracer import RunTracer
from reposition.sandbox import E2BSandboxManager
from reposition.state import RepositionState
from reposition.tools.patch_utils import (
    extract_modified_files,
    is_unified_diff,
    validate_diff_syntax,
)

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_FILE_HEADER_RE = re.compile(r"^=== FILE:\s*(.+?)\s*===$", re.MULTILINE)


# ── style inference ──────────────────────────────────────────────────────

def _infer_style(samples: list[str]) -> str:
    """Build a short style-context string from code samples."""
    indent_tabs = 0
    indent_spaces = 0
    single_quotes = 0
    double_quotes = 0
    snake = 0
    camel = 0

    for src in samples:
        for line in src.splitlines():
            stripped = line.lstrip()
            if not stripped:
                continue
            leading = line[: len(line) - len(stripped)]
            if "\t" in leading:
                indent_tabs += 1
            elif "    " in leading:
                indent_spaces += 1
            elif "  " in leading:
                indent_spaces += 1
        single_quotes += src.count("'")
        double_quotes += src.count('"')
        snake += len(re.findall(r"\b[a-z]+_[a-z]+\b", src))
        camel += len(re.findall(r"\b[a-z]+[A-Z][a-z]+\b", src))

    indent = "tabs" if indent_tabs > indent_spaces else "spaces (4-wide)"
    quotes = "single quotes" if single_quotes > double_quotes else "double quotes"
    naming = "snake_case" if snake >= camel else "camelCase"

    return (
        f"- Indentation: {indent}\n"
        f"- String quotes: {quotes}\n"
        f"- Naming convention: {naming}\n"
    )


# ── prompt templates ─────────────────────────────────────────────────────

def _fresh_system_prompt(style_context: str, threshold: int) -> str:
    return (
        "You are a precision code modification engine. You apply surgical fixes.\n\n"
        "STYLE RULES — follow these exactly, extracted from the codebase:\n"
        f"{style_context}\n"
        "OUTPUT FORMAT:\n"
        f"- For files with <= {threshold} lines: output the COMPLETE modified file content,\n"
        "  preceded by a header line: === FILE: relative/path/to/file ===\n"
        f"- For files with > {threshold} lines: output a unified diff in git diff format\n"
        "  (--- a/filename, +++ b/filename, @@ context hunks)\n\n"
        "HARD CONSTRAINTS:\n"
        "- Zero new external dependencies\n"
        "- Zero changes to public API signatures\n"
        "- Zero changes to code unrelated to the work package\n"
        "- Style must exactly match the provided samples\n"
    )


def _retry_system_prompt(last_verdict: str, last_stack_trace: str) -> str:
    return (
        "Your previous patch failed validation. Fix ONLY this specific error:\n\n"
        f"FAILURE TYPE: {last_verdict}\n"
        "ERROR OUTPUT:\n"
        f"{last_stack_trace}\n\n"
        "Do not change any code unrelated to this error.\n"
        "Apply the same output format and constraints as the original attempt.\n"
    )


# ── output parsing ───────────────────────────────────────────────────────

def _parse_output(text: str) -> dict[str, str]:
    """Parse the LLM response into ``{filename: content_or_diff}``."""
    result: dict[str, str] = {}

    # Try full-file format first (=== FILE: ... ===)
    headers = list(_FILE_HEADER_RE.finditer(text))
    if headers:
        for i, match in enumerate(headers):
            path = match.group(1)
            start = match.end()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            result[path] = text[start:end].strip()
        return result

    # Otherwise treat as unified diff
    if is_unified_diff(text):
        files = extract_modified_files(text)
        if files:
            # Store full diff keyed to each file (whole diff)
            for f in files:
                result[f] = text
            return result

    # Fallback: return entire text under a placeholder key
    if text.strip():
        result["__raw__"] = text.strip()
    return result


# ── agent ────────────────────────────────────────────────────────────────

async def coder_agent(state: RepositionState) -> dict:
    """LangGraph node: generate a code patch for the current work package."""
    cfg = get_config()
    tracer = RunTracer(state["run_id"], state["trace_path"])
    sandbox = E2BSandboxManager()
    sandbox_id = state["e2b_sandbox_id"]
    assert sandbox_id is not None, "Sandbox must be initialised before coder runs"

    work_package = state["work_packages"][state["current_package_index"]]
    files_to_modify: list[str] = work_package["files_to_modify"]

    # ── 1. Read current file contents from sandbox ───────────────────
    file_contents: dict[str, str] = {}
    for rel_path in files_to_modify:
        remote = f"/home/user/repo/{rel_path}"
        content = await sandbox.read_file(sandbox_id, remote)
        file_contents[rel_path] = content

    # ── 2. Gather style samples ──────────────────────────────────────
    manifest = state["manifest"] or {}
    manifest_files = manifest.get("files", [])
    modify_set = set(files_to_modify)

    # Determine the language of files being modified
    target_languages: set[str] = set()
    for mf in manifest_files:
        if mf["path"] in modify_set:
            target_languages.add(mf.get("language", "unknown"))

    samples: list[str] = []
    for mf in manifest_files:
        if len(samples) >= 3:
            break
        if mf["path"] in modify_set:
            continue
        if mf.get("language", "unknown") in target_languages and mf.get("full_content_available"):
            remote = f"/home/user/repo/{mf['path']}"
            try:
                sample_content = await sandbox.read_file(sandbox_id, remote)
                samples.append(sample_content)
            except Exception:
                continue

    style_context = _infer_style(samples) if samples else "- Follow existing project conventions\n"

    # ── 3. Choose system prompt ──────────────────────────────────────
    retry_count = state["retry_count"]
    if retry_count > 0 and state["package_results"]:
        last_result = state["package_results"][-1]
        system_prompt = _retry_system_prompt(
            last_verdict=last_result.get("status", "UNKNOWN"),
            last_stack_trace=last_result.get("verdict_detail", ""),
        )
    else:
        system_prompt = _fresh_system_prompt(style_context, cfg.coder.full_file_threshold_lines)

    # ── 4. Build user message ────────────────────────────────────────
    user_parts: list[str] = [
        f"Work package: {json.dumps(work_package, indent=2)}\n",
        "Current file contents:\n",
    ]
    for path, content in file_contents.items():
        user_parts.append(f"=== {path} ===\n{content}\n")

    user_message = "\n".join(user_parts)

    # ── 5. Call LLM ──────────────────────────────────────────────────
    llm = get_llm("fast", max_tokens=8192)
    raw_text, token_usage = call_llm(llm, system_prompt, user_message)
    parsed = _parse_output(raw_text)

    # ── 6. Validate diffs ────────────────────────────────────────────
    for filename, content in list(parsed.items()):
        if is_unified_diff(content):
            valid, err = validate_diff_syntax(content)
            if not valid:
                # One more LLM call to fix the malformed diff
                fix_llm = get_llm("fast", max_tokens=4096)
                fixed_text, _ = call_llm(
                    fix_llm,
                    "Fix the following malformed unified diff. Output ONLY the corrected diff.",
                    f"Error: {err}\n\nDiff:\n{content}",
                )
                parsed[filename] = fixed_text.strip()

    # ── 7. Determine new retry_count ─────────────────────────────────
    new_retry_count = retry_count
    if state["package_results"]:
        last = state["package_results"][-1]
        if last.get("status", "").startswith("FAIL"):
            new_retry_count = retry_count + 1
        else:
            new_retry_count = 0
    else:
        new_retry_count = 0

    # ── 8. Trace ─────────────────────────────────────────────────────

    tracer.log(
        agent_name="coder",
        decision="patch_generated",
        output={
            "work_package_id": work_package.get("id"),
            "files_modified": list(parsed.keys()),
            "is_retry": retry_count > 0,
        },
        token_usage=token_usage,
    )

    return {
        "current_patch": json.dumps(parsed),
        "retry_count": new_retry_count,
    }

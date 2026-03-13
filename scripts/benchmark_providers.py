"""Benchmark provider compatibility and quality for Reposition security analysis."""

from __future__ import annotations

import json
import os
import re
import time

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

import reposition.config as config_module
from reposition.agents.security_analyzer import SECURITY_SYSTEM_PROMPT
from reposition.llm_client import PROVIDER_DEFAULTS, call_llm, get_llm

TEST_SNIPPET = """
import sqlite3
import os

SECRET_KEY = "hardcoded_secret_abc123"

def get_user(username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE username = '" \
            + username + "'"
    cursor.execute(query)
    return cursor.fetchone()

def admin_panel(request):
    data = request.get("data", "")
    cursor.execute("DELETE FROM logs WHERE id = " \
                   + data)
    return {"deleted": True}

def process_payment(amount, card_number):
    log = open("payments.log", "a")
    log.write(f"Card: {card_number}, Amount: {amount}")
    return True
"""

_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "groq": ("GROQ_API_KEY",),
}

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _set_provider(provider: str) -> None:
    os.environ["REPOSITION_LLM_PROVIDER"] = provider
    config_module._singleton = None


def _provider_has_key(provider: str) -> tuple[bool, str]:
    keys = _PROVIDER_KEYS[provider]
    for key in keys:
        if os.environ.get(key, "").strip():
            return True, key
    if provider == "gemini":
        return False, "GEMINI_API_KEY or GOOGLE_API_KEY"
    return False, keys[0]


def _model_name(llm: object, fallback: str) -> str:
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return fallback


def _vuln_score(vulns_found: int) -> str:
    if vulns_found >= 3:
        return "3/3"
    if vulns_found == 2:
        return "2/3"
    return "1/3 or less"


def main() -> None:
    load_dotenv()
    console = Console()

    table = Table(title="Provider Security Benchmark")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Time", justify="right")
    table.add_column("Valid JSON", justify="center")
    table.add_column("Vulns", justify="center")

    provider_order = ["gemini", "openai", "anthropic", "groq"]
    results: list[dict] = []

    for provider in provider_order:
        has_key, key_label = _provider_has_key(provider)
        if not has_key:
            console.print(f"[skipped] {provider} -- {key_label} not set")
            continue

        _set_provider(provider)
        default_model = PROVIDER_DEFAULTS[provider]["heavy"]
        model_name = default_model

        start = time.time()
        valid_json = False
        vulns_found = 0

        try:
            llm = get_llm("heavy")
            model_name = _model_name(llm, default_model)
            text, token_usage = call_llm(
                llm,
                SECURITY_SYSTEM_PROMPT,
                f"Analyze this code:\n{TEST_SNIPPET}",
            )
            elapsed = time.time() - start

            try:
                parsed = json.loads(_strip_fences(text))
                valid_json = True
                if isinstance(parsed, list):
                    vulns_found = len(parsed)
                elif isinstance(parsed, dict):
                    vulns_found = 1
            except Exception:
                valid_json = False
                vulns_found = 0

        except Exception as exc:
            elapsed = time.time() - start
            text = ""
            token_usage = {}
            console.print(f"[error] {provider} -- request failed: {exc}")

        score = _vuln_score(vulns_found)
        table.add_row(
            provider,
            model_name,
            f"{elapsed:.1f}s",
            "YES" if valid_json else "NO",
            score,
        )

        results.append(
            {
                "provider": provider,
                "model": model_name,
                "elapsed": elapsed,
                "valid_json": valid_json,
                "vulns_found": vulns_found,
                "score": score,
                "token_usage": token_usage,
                "raw": text,
            }
        )

    console.print(table)

    if not results:
        console.print("[FAIL] No providers had API keys set")
        return

    if len(results) == 1:
        only = results[0]["provider"]
        console.print(f"[OK] {only} -- set other API keys to compare")
        return

    full_coverage = [r for r in results if r["vulns_found"] >= 3]
    if full_coverage:
        recommended = min(full_coverage, key=lambda r: r["elapsed"])
        console.print(
            f"[RECOMMENDED] {recommended['provider']} -- fastest with full coverage"
        )
    else:
        console.print("[WARN] No provider found all 3 vulnerabilities in this run")


if __name__ == "__main__":
    main()

"""Provider compatibility check script for Reposition."""

from __future__ import annotations

import json
import os
import re
import sys

import click
from dotenv import load_dotenv

import reposition.config as config_module
from reposition.llm_client import PROVIDER_DEFAULTS, call_llm, get_llm

_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}

_PROVIDER_HELP_URL: dict[str, str] = {
    "anthropic": "console.anthropic.com",
    "openai": "platform.openai.com/api-keys",
    "gemini": "aistudio.google.com/app/apikey",
    "groq": "console.groq.com/keys",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _resolve_api_key(provider: str) -> tuple[bool, str]:
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


def _set_provider(provider: str) -> None:
    os.environ["REPOSITION_LLM_PROVIDER"] = provider
    config_module._singleton = None


@click.command()
@click.option(
    "--provider",
    type=click.Choice(sorted(PROVIDER_DEFAULTS.keys()), case_sensitive=False),
    default=None,
    help="Provider to test (anthropic, openai, gemini, groq).",
)
def main(provider: str | None) -> None:
    """Run compatibility checks for a single configured provider."""
    load_dotenv()

    chosen = (provider or os.environ.get("REPOSITION_LLM_PROVIDER", "")).strip().lower()
    if not chosen:
        print("[FAIL] No provider selected")
        print("Set REPOSITION_LLM_PROVIDER or pass --provider")
        sys.exit(1)

    if chosen not in PROVIDER_DEFAULTS:
        valid = ", ".join(sorted(PROVIDER_DEFAULTS))
        print(f"[FAIL] Unknown provider '{chosen}'")
        print(f"Valid providers: {valid}")
        sys.exit(1)

    _set_provider(chosen)

    check1_ok = False
    check2_ok = False
    check3_ok = False
    check4_ok = False
    check5_ok = False

    print(f"Testing provider: {chosen}")

    key_ok, key_label = _resolve_api_key(chosen)
    if key_ok:
        check1_ok = True
        print(f"[PASS] CHECK 1 - API key present ({key_label})")
    else:
        help_url = _PROVIDER_HELP_URL[chosen]
        print(f"[FAIL] CHECK 1 - API key present ({key_label} missing)")
        print(f"Set {key_label}. Get your key at: {help_url}")

    if check1_ok:
        default_fast = PROVIDER_DEFAULTS[chosen]["fast"]
        try:
            llm_fast = get_llm("fast")
            fast_model = _model_name(llm_fast, default_fast)
            text, _ = call_llm(llm_fast, "You are a test bot.", "Respond with exactly: PONG")
            if "PONG" in text.upper():
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
            if "PONG" in text.upper():
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

    check4_token_usage: dict = {}
    if check1_ok:
        try:
            system = (
                "You are a JSON API. Respond with valid JSON only. "
                "No markdown, no extra text."
            )
            user = "Return: {\"status\": \"ok\"}"
            text, check4_token_usage = call_llm(get_llm("fast"), system, user)
            parsed = json.loads(_strip_fences(text))
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

    if check4_token_usage and len(check4_token_usage.keys()) > 0:
        check5_ok = True
        print("[PASS] CHECK 5 - Token usage reporting")
    else:
        print("[WARN] CHECK 5 - Token usage reporting")
        print("Token usage not reported by this provider -- tracer token counts will show 0")

    if check1_ok and check2_ok and check3_ok and check4_ok:
        print(f"[OK] {chosen} is fully compatible")
        print("Run: reposition run <repo_path>")
        sys.exit(0)

    print(f"[FAIL] {chosen} has compatibility issues")
    print("Fix the above before running the pipeline")
    sys.exit(1)


if __name__ == "__main__":
    main()

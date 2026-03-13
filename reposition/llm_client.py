"""Multi-provider LLM abstraction layer for Reposition."""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from reposition.config import get_config

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {
        "heavy": "claude-opus-4-6",
        "fast": "claude-sonnet-4-6",
    },
    "openai": {
        "heavy": "gpt-4o",
        "fast": "gpt-4o-mini",
    },
    "gemini": {
        "heavy": "gemini-2.5-pro",
        "fast": "gemini-2.5-flash",
    },
    "groq": {
        "heavy": "llama-3.3-70b-versatile",
        "fast": "llama-3.1-8b-instant",
    },
}

_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

_KEY_HELP_URL: dict[str, str] = {
    "anthropic": "console.anthropic.com",
    "openai": "platform.openai.com/api-keys",
    "gemini": "aistudio.google.com/app/apikey",
    "groq": "console.groq.com/keys",
}

_VALID_PROVIDERS = set(PROVIDER_DEFAULTS)
_api_key_warning_shown = False


def get_llm(role: str, *, max_tokens: int = 8192) -> Any:
    """Return a LangChain ``BaseChatModel`` for the given *role*.

    Parameters
    ----------
    role:
        ``"heavy"`` or ``"fast"``.
    max_tokens:
        Maximum tokens for the response (used by Anthropic provider).
    """
    cfg = get_config().llm
    provider = cfg.provider.lower()

    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. "
            f"Valid providers: {', '.join(sorted(_VALID_PROVIDERS))}"
        )

    # Resolve model name
    override = cfg.heavy_model if role == "heavy" else cfg.fast_model
    model_name = override or PROVIDER_DEFAULTS[provider][role]

    # Check API key
    env_var = _API_KEY_ENV[provider]
    api_key = os.environ.get(env_var)

    if provider == "gemini":
        global _api_key_warning_shown
        google_api_key = os.environ.get("GOOGLE_API_KEY")
        gemini_api_key = os.environ.get("GEMINI_API_KEY")

        if google_api_key and gemini_api_key and not _api_key_warning_shown:
            print("Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.")
            _api_key_warning_shown = True
            # Prevent downstream client libraries from emitting this warning repeatedly.
            os.environ.pop("GEMINI_API_KEY", None)

        if google_api_key:
            api_key = google_api_key

    if not api_key:
        raise EnvironmentError(
            f"Missing environment variable {env_var}. "
            f"Get your API key at: {_KEY_HELP_URL[provider]}"
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, api_key=api_key, max_tokens=max_tokens)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name, api_key=api_key)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key)

    # groq
    from langchain_groq import ChatGroq
    return ChatGroq(model=model_name, api_key=api_key)


def call_llm(
    llm: Any,
    system_prompt: str,
    user_message: str,
) -> tuple[str, dict]:
    """Invoke *llm* with a system + user message pair.

    Returns
    -------
    tuple[str, dict]
        ``(response_text, token_usage)`` where *token_usage* is extracted
        from ``response.usage_metadata`` if available, else ``{}``.
    """
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ])

    text = response.content if isinstance(response.content, str) else str(response.content)

    token_usage: dict = {}
    meta = getattr(response, "usage_metadata", None)
    if meta and isinstance(meta, dict):
        token_usage = {
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "total_tokens": meta.get("total_tokens", 0),
        }

    return text, token_usage

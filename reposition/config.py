"""Configuration loader for Reposition."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_DEFAULT_CONFIG_FILENAME = "reposition.config.yaml"


# ── nested config sections ──────────────────────────────────────────────


@dataclass
class ScannerConfig:
    max_manifest_tokens: int = 50_000
    large_file_threshold_lines: int = 500


@dataclass
class AnalyzersConfig:
    timeout_seconds: int = 120


@dataclass
class PlannerConfig:
    max_work_packages_per_run: int = 10
    max_files_per_package: int = 3
    max_lines_per_package: int = 200
    max_concurrent_packages: int = 3


@dataclass
class CoderConfig:
    full_file_threshold_lines: int = 100
    max_retries: int = 3


@dataclass
class ValidatorConfig:
    test_timeout_seconds: int = 300


@dataclass
class PrAgentConfig:
    max_diff_files: int = 20
    max_diff_lines: int = 500


@dataclass
class GithubConfig:
    base_branch: str = "main"
    clone_dir: str = "~/.reposition/repos"
    pr_repo: str = ""


@dataclass
class LLMConfig:
    provider: str = "anthropic"  # "anthropic" | "openai" | "gemini" | "groq"
    heavy_model: str | None = None  # Used by Planner, Security Analyzer, Refactor Analyzer
    fast_model: str | None = None   # Used by Coverage Analyzer, Coder, Validator, PR Agent


# ── top-level config ────────────────────────────────────────────────────


@dataclass
class Config:
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    analyzers: AnalyzersConfig = field(default_factory=AnalyzersConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    pr_agent: PrAgentConfig = field(default_factory=PrAgentConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


# ── internal helpers ────────────────────────────────────────────────────

_SECTION_TYPES: dict[str, type] = {
    "scanner": ScannerConfig,
    "analyzers": AnalyzersConfig,
    "planner": PlannerConfig,
    "coder": CoderConfig,
    "validator": ValidatorConfig,
    "pr_agent": PrAgentConfig,
    "github": GithubConfig,
    "llm": LLMConfig,
}


def get_config_dir() -> Path:
    """Return the canonical user-level config directory (~/.reposition)."""
    config_dir = Path.home() / ".reposition"
    config_dir.mkdir(exist_ok=True)
    return config_dir


def get_env_path() -> Path:
    """Return the path to the user-level .env file."""
    return get_config_dir() / ".env"


def _coerce(value: str, target_type: type) -> Any:
    """Coerce a string env-var value to the target field type."""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply_env_overrides(cfg: Config) -> None:
    """Apply ``REPOSITION_<SECTION>_<FIELD>`` env-var overrides in-place."""
    prefix = "REPOSITION_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        parts = env_key[len(prefix):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section_name, field_name = parts
        section_obj = getattr(cfg, section_name, None)
        if section_obj is None:
            continue
        # Verify the field actually exists on the dataclass
        field_names = {f.name for f in fields(section_obj)}
        if field_name not in field_names:
            continue
        current = getattr(section_obj, field_name)
        target_type = type(current) if current is not None else str
        setattr(section_obj, field_name, _coerce(env_val, target_type))


# ── public API ──────────────────────────────────────────────────────────


def load_config(path: str | None = None) -> Config:
    """Load configuration from a YAML file, then apply env-var overrides.

    Parameters
    ----------
    path:
        Explicit path to a YAML config file.  When *None* the loader
        looks for ``reposition.config.yaml`` in the current working
        directory.

    Returns
    -------
    Config
        A fully-populated configuration dataclass.
    """
    raw: dict[str, Any] = {}
    config_path = Path(path).resolve() if path else (Path.cwd() / _DEFAULT_CONFIG_FILENAME)

    # Always hydrate process env from the user-level .env first,
    # then allow a local .env in the current working directory to override.
    load_dotenv(get_env_path(), override=False)
    load_dotenv(Path.cwd() / ".env", override=True)

    if config_path.is_file():
        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    sections: dict[str, Any] = {}
    for section_name, section_cls in _SECTION_TYPES.items():
        section_data = raw.get(section_name, {})
        if isinstance(section_data, dict):
            sections[section_name] = section_cls(**section_data)
        else:
            sections[section_name] = section_cls()

    cfg = Config(**sections)
    _apply_env_overrides(cfg)

    # Standard environment variable for optional PR target repository.
    # Supports backward compatibility with older naming.
    env_pr_repo = os.environ.get("GITHUB_PR_REPO", "").strip() or os.environ.get("GITHUB_REPO", "").strip()
    if env_pr_repo:
        cfg.github.pr_repo = env_pr_repo

    return cfg


_singleton: Config | None = None


def get_config() -> Config:
    """Return the global `Config` singleton, creating it on first call."""
    global _singleton
    if _singleton is None:
        _singleton = load_config()
    return _singleton

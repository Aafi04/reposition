# Reposition

Reposition is an autonomous code-improvement agent that scans a repository for security vulnerabilities, code duplication, and missing test coverage, then generates fixes and opens a GitHub pull request — all without human intervention.

Demo recording: https://asciinema.org/a/o1NHF2A60DV9JQYe

Runtime: ~5-12 min on real repos. Use --dry-run to preview in ~2 min first.

## How it works

Reposition runs a six-stage pipeline orchestrated by **LangGraph**:
**Scanner** builds a repository manifest → three **Analyzers** (security, refactor, coverage) evaluate the codebase in parallel → **Planner** synthesises their reports into prioritised work packages → **Coder** generates patches and executes them inside an **E2B** cloud sandbox → **Validator** runs the project's own test suite to verify each patch → **PR Agent** composes a commit message and description, then opens a real **GitHub pull request** for review.

## Quickstart

### 1. Install

```bash
pip install -e .
```

### Windows users

If `reposition` command is not found after install, either:

a) Activate your venv first:

```powershell
.venv\Scripts\Activate.ps1
reposition --help
```

b) Or use the launcher directly:

```powershell
.\reposition.bat run <repo_url>
```

### 2. Set environment variables

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

| Variable                  | Purpose                                                                                                        |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `REPOSITION_LLM_PROVIDER` | LLM backend to use: `anthropic`, `openai`, `gemini`, or `groq`                                                 |
| `ANTHROPIC_API_KEY`       | Anthropic API key — [console.anthropic.com](https://console.anthropic.com)                                     |
| `OPENAI_API_KEY`          | OpenAI API key — [platform.openai.com/api-keys](https://platform.openai.com/api-keys)                          |
| `GEMINI_API_KEY`          | Google Gemini API key — [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)               |
| `GROQ_API_KEY`            | Groq API key — [console.groq.com/keys](https://console.groq.com/keys)                                          |
| `E2B_API_KEY`             | E2B sandbox key (free tier available) — [e2b.dev/dashboard](https://e2b.dev/dashboard)                         |
| `GITHUB_TOKEN`            | GitHub personal access token (`repo` scope) — [github.com/settings/tokens](https://github.com/settings/tokens) |
| `GITHUB_REPO`             | Target repository in `owner/repo` format                                                                       |

You only need the API key for the provider you choose.

### 3. Run

```bash
# Full pipeline
reposition run /path/to/your/repo

# Dry run — runs Scanner + Analyzers + Planner only, no code changes
reposition run /path/to/your/repo --dry-run

# Resume a failed or interrupted run
reposition resume <run_id>

# Check the status of a run
reposition status <run_id>
```

## Verify your setup

Before running against a real repo:

```bash
python scripts/test_provider.py
```

To compare providers (set multiple API keys first):

```bash
python scripts/benchmark_providers.py
```

## Provider compatibility

| Provider  | Status    | Notes                              |
| --------- | --------- | ---------------------------------- |
| Gemini    | Validated | Tested on 170-file real repo       |
| Anthropic | Untested  | Architecture compatible, needs run |
| OpenAI    | Untested  | Architecture compatible, needs run |
| Groq      | Beta      | Fast but may miss findings         |

Community: open a PR updating this table if you validate a provider.

## Runtime

Typical: 8-14 minutes depending on repo size and API response times. Use --dry-run to preview work
packages in ~2-3 minutes before committing to a full run.

## Configuration

All tuneable settings live in `reposition.config.yaml`. Key options:

| Setting                             | Default     | Description                                                     |
| ----------------------------------- | ----------- | --------------------------------------------------------------- |
| `analyzers.timeout_seconds`         | `120`       | Max seconds each analyzer may run                               |
| `planner.max_work_packages_per_run` | `10`        | Cap on work packages per run                                    |
| `validator.test_timeout_seconds`    | `300`       | Max seconds for the test suite                                  |
| `pr_agent.max_diff_lines`           | `500`       | Skip PR if the diff exceeds this                                |
| `pr_agent.max_diff_files`           | `20`        | Skip PR if too many files changed                               |
| `github.base_branch`                | `main`      | Branch the PR targets                                           |
| `llm.provider`                      | `anthropic` | LLM provider (`anthropic`, `openai`, `gemini`, `groq`)          |
| `llm.heavy_model`                   | `null`      | Override the "heavy" model name (uses provider default if null) |
| `llm.fast_model`                    | `null`      | Override the "fast" model name (uses provider default if null)  |

Any setting can also be overridden via environment variable using the pattern `REPOSITION_<SECTION>_<FIELD>` (e.g. `REPOSITION_PLANNER_MAX_WORK_PACKAGES_PER_RUN=5`).

## What it will and won't touch

**Won't:**

- Change public API signatures
- Add external dependencies
- Modify files it wasn't asked to modify

**Will:**

- Open a pull request on your repository — always review the diff before merging.

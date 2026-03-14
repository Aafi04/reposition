<div align="center">

<pre>
██████╗ ███████╗██████╗  ██████╗ ███████╗██╗████████╗██╗ ██████╗ ███╗   ██╗
██╔══██╗██╔════╝██╔══██╗██╔═══██╗██╔════╝██║╚══██╔══╝██║██╔═══██╗████╗  ██║
██████╔╝█████╗  ██████╔╝██║   ██║███████╗██║   ██║   ██║██║   ██║██╔██╗ ██║
██╔══██╗██╔══╝  ██╔═══╝ ██║   ██║╚════██║██║   ██║   ██║██║   ██║██║╚██╗██║
██║  ██║███████╗██║     ╚██████╔╝███████║██║   ██║   ██║╚██████╔╝██║ ╚████║
╚═╝  ╚═╝╚══════╝╚═╝      ╚═════╝ ╚══════╝╚═╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
</pre>

**Autonomous code improvement agent**

Point it at any repo. Walk away. Come back to a pull request.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-FF6B35?style=flat-square)](https://langchain-ai.github.io/langgraph/)
[![E2B](https://img.shields.io/badge/Sandbox-E2B-000000?style=flat-square)](https://e2b.dev)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=flat-square)](LICENSE)
[![Works With](https://img.shields.io/badge/Works%20With-Anthropic%20%7C%20OpenAI%20%7C%20Gemini%20%7C%20Groq-6366F1?style=flat-square)](#provider-compatibility)

[**Demo**](https://drive.google.com/file/d/1X_YMakt45OIlVOpMyTuDFWhTYuTWUwwq/view?usp=sharing) · [**Quickstart**](#quickstart) · [**How It Works**](#how-it-works) · [**Configuration**](#configuration)

</div>

---

## What is Reposition?

Reposition is a multi-agent system that autonomously audits a codebase, writes fixes, validates them against the real test suite inside an isolated cloud sandbox, and opens a GitHub pull request — without you touching a single line of code.

It is not a code completion tool. It is not a chatbot. It is an agent that does the work.

```
$ reposition run https://github.com/you/your-repo

  Scanner    OK  0:03   170 files scanned, 1 secret excluded
  Analyzers  OK  1:58   14 security findings, 6 refactor issues
  Planner    OK  2:23   9 work packages (CRITICAL_SECURITY first)
  Coder      OK  7:41   9/9 packages executed
  Validator  OK         PASS — all patches verified in sandbox
  PR Agent   OK         PR #4 opened

  Run complete — 8m 41s
  PR: https://github.com/you/your-repo/pull/4
```

---

## How It Works

Reposition runs an 8-agent pipeline orchestrated by **LangGraph** with full checkpointing, parallel execution, and a self-healing retry loop:

```
                          ┌─────────────────────────────────┐
                          │         Parallel Analyzers      │
                          │  Security · Refactor · Coverage │
                          └─────────────────────────────────┘
                                          │
 Scanner ──► Analyzers ──► Planner ──► Coder ──► Validator ──► PR Agent
                                          │          │
                                          └── retry ─┘ (up to 3x)
```

| Agent                 | Model | Role                                                          |
| --------------------- | ----- | ------------------------------------------------------------- |
| **Scanner**           | Fast  | Builds repo manifest, detects secrets, identifies test runner |
| **Security Analyzer** | Heavy | Finds vulnerabilities with CWE classifications                |
| **Refactor Analyzer** | Heavy | Detects duplication, SRP violations, cyclomatic complexity    |
| **Coverage Analyzer** | Fast  | Maps untested critical paths                                  |
| **Planner**           | Heavy | Synthesises findings into prioritised work packages           |
| **Coder**             | Fast  | Generates patches that match the repo's native style          |
| **Validator**         | Fast  | Runs the real test suite inside an E2B sandbox                |
| **PR Agent**          | Fast  | Writes commit messages and opens the pull request             |

Every patch is validated inside an **E2B cloud sandbox** before it touches your repo. If tests fail, the Coder retries automatically. If it cannot fix the issue in 3 attempts, it skips that package and moves on — it never ships broken code.

Priority order is always: `CRITICAL_SECURITY → HIGH_SECURITY → BUILD_RUNTIME → HIGH_TECH_DEBT → MISSING_TESTS`

---

## Quickstart

### macOS and Linux

```bash
git clone https://github.com/Aafi04/reposition
cd reposition
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
reposition setup
reposition run https://github.com/you/your-repo --dry-run
```

> **Ubuntu/Debian:** If venv creation fails, first run:
> `sudo apt install python3-venv python3-pip -y`
> Then retry from `python3 -m venv .venv`.

### Windows

```powershell
git clone https://github.com/Aafi04/reposition
cd reposition
python -m venv .venv
Start-Sleep -Seconds 2
.venv\Scripts\activate
Start-Sleep -Seconds 2
pip install -e .
reposition setup
reposition run https://github.com/you/your-repo --dry-run
```

> First install takes 3–5 minutes while dependencies
> download. WSL users: clone to `~/` instead of
> `/mnt/c/` for faster install times.

### What `reposition setup` does

The setup wizard runs once and configures everything:

1. Asks which LLM provider you want to use
2. Prints the exact URL to get your API key
3. Collects your API key, E2B key, and GitHub token
   (all input is masked — keys never appear on screen)
4. Writes your `.env` file automatically
5. Runs 5 verification checks to confirm everything
   works before you run against a real repo

After setup, you never need to touch a config file.

### Getting your keys

| Key          | Where to get it                                                               | Free tier?           |
| ------------ | ----------------------------------------------------------------------------- | -------------------- |
| LLM API key  | Depends on provider — setup tells you the URL                                 | Gemini yes, Groq yes |
| E2B_API_KEY  | [e2b.dev/dashboard](https://e2b.dev/dashboard)                                | Yes                  |
| GITHUB_TOKEN | [github.com/settings/tokens](https://github.com/settings/tokens) (repo scope) | Yes                  |

You only need **one** LLM API key — whichever
provider you choose during setup.

### Running Reposition

```bash
# Preview what will change — no code written (~3-5 min)
reposition run https://github.com/you/your-repo --dry-run

# Full run — scans, fixes, validates, opens PR (~8-14 min)
reposition run https://github.com/you/your-repo

# All repo formats are accepted
reposition run owner/repo
reposition run https://github.com/owner/repo
reposition run git@github.com:owner/repo.git

# Open PR on a different repo (e.g. your fork)
reposition run https://github.com/owner/repo --pr-repo you/fork

# Resume an interrupted run
reposition resume <run_id>

# Check run status (run ID shown at pipeline start)
reposition status <run_id>
```

---

## Provider Compatibility

Reposition works with whichever LLM provider you already have. Bring your own key.

| Provider      | Status        | Speed   | Notes                                          |
| ------------- | ------------- | ------- | ---------------------------------------------- |
| **Gemini**    | ✅ Validated  | Fast    | Tested end-to-end on multiple real repos       |
| **OpenAI**    | 🔬 Compatible | Fast    | Architecture validated, community runs welcome |
| **Anthropic** | 🔬 Compatible | Medium  | Architecture validated, community runs welcome |
| **Groq**      | ⚗️ Beta       | Fastest | Free tier — smaller models may miss findings   |

Internally, Reposition uses a **heavy** model for agents requiring deep architectural reasoning (Planner, Security Analyzer, Refactor Analyzer) and a **fast** model for high-volume tasks (Scanner, Coder, Validator, PR Agent).

Default model assignments per provider:

| Provider  | Heavy model             | Fast model           |
| --------- | ----------------------- | -------------------- |
| Anthropic | claude-opus-4-6         | claude-sonnet-4-6    |
| OpenAI    | gpt-4o                  | gpt-4o-mini          |
| Gemini    | gemini-2.5-pro          | gemini-2.5-flash     |
| Groq      | llama-3.3-70b-versatile | llama-3.1-8b-instant |

You can override either model in `reposition.config.yaml` or via env vars.

**Tested a provider not listed here?** Open a PR updating this table.

To benchmark all providers you have keys for:

```bash
python scripts/benchmark_providers.py
```

---

## Configuration

All tuneable settings live in `reposition.config.yaml` at the repo root.

```yaml
llm:
  provider: gemini # anthropic | openai | gemini | groq
  heavy_model: null # override heavy model name (null = use default)
  fast_model: null # override fast model name (null = use default)

scanner:
  max_manifest_tokens: 50000
  large_file_threshold_lines: 500

analyzers:
  timeout_seconds: 120 # per-analyzer timeout for parallel execution

planner:
  max_work_packages_per_run: 10
  max_files_per_package: 3
  max_lines_per_package: 200
  max_concurrent_packages: 3

coder:
  full_file_threshold_lines: 100
  max_retries: 3

validator:
  test_timeout_seconds: 300

pr_agent:
  max_diff_files: 20
  max_diff_lines: 500

github:
  base_branch: main
```

Every setting can also be overridden via environment variable:

```
REPOSITION_PLANNER_MAX_WORK_PACKAGES_PER_RUN=5
REPOSITION_ANALYZERS_TIMEOUT_SECONDS=180
```

---

## Runtime

| Run type | Typical time | What happens                                         |
| -------- | ------------ | ---------------------------------------------------- |
| Dry run  | 3–6 minutes  | Scanner + Analyzers + Planner only. No code changes. |
| Full run | 8–14 minutes | Complete pipeline. PR opened at end.                 |

Runtime depends on repo size, number of findings, and LLM provider API response times. Gemini 2.5 Flash and GPT-4o-mini are the fastest options. Larger repos with more work packages take proportionally longer.

---

## What Reposition Will and Won't Do

**Will:**

- Open a real GitHub pull request with your fixes
- Run your actual test suite before shipping any patch
- Abort patches that fail validation after 3 retries
- Exclude secrets and `.env` files from sandbox upload
- Produce a full audit trace at `.traces/<run_id>.jsonl`

**Won't:**

- Change public API signatures or method names
- Add new external dependencies to your project
- Merge the PR — you always review before merging
- Modify files outside the current work package scope
- Proceed if your API key or GitHub token is missing

---

## Architecture

```
reposition/
├── main.py                      # CLI entry point (click)
├── reposition/
│   ├── cli.py                   # CLI shim
│   ├── config.py                # Config loader (YAML + env overrides)
│   ├── state.py                 # LangGraph TypedDict state
│   ├── graph.py                 # Pipeline graph + parallel scheduler
│   ├── sandbox.py               # E2B sandbox lifecycle manager
│   ├── llm_client.py            # Provider-agnostic LLM abstraction
│   ├── agents/
│   │   ├── scanner.py
│   │   ├── security_analyzer.py
│   │   ├── refactor_analyzer.py
│   │   ├── coverage_analyzer.py
│   │   ├── planner.py
│   │   ├── coder.py
│   │   ├── validator.py
│   │   └── pr_agent.py
│   ├── tools/
│   │   ├── ast_parser.py        # Language-agnostic AST extraction
│   │   ├── file_ranker.py       # Smart file selection for analyzers
│   │   ├── github_tools.py      # PyGithub wrapper with rate-limit backoff
│   │   ├── patch_utils.py       # Unified diff apply / dry-run
│   │   ├── secret_scanner.py    # Secrets detection before E2B upload
│   │   └── test_runner_detector.py
│   └── observability/
│       └── tracer.py            # Structured JSONL trace logger
└── scripts/
    ├── test_provider.py         # Verify provider before full run
    ├── benchmark_providers.py   # Compare providers head-to-head
    └── smoke_test.py            # End-to-end dry-run test
```

---

## Contributing

Reposition is open source and early. Contributions welcome.

**Highest-value contributions right now:**

- Validate a provider and open a PR updating the compatibility table
- Test against a repo in a language not yet covered (Go, Rust, Ruby)
- Improve test runner detection for uncommon build systems
- Report issues with specific repo structures that cause failures

```bash
# Run the test suite
pytest tests/ -v

# Run the smoke test against the fixture repo (uses real API)
python scripts/smoke_test.py

# Verify your provider before contributing
python scripts/test_provider.py
```

---

<div align="center">

Built with [LangGraph](https://langchain-ai.github.io/langgraph/) · [E2B](https://e2b.dev) · [Anthropic](https://anthropic.com) · [PyGithub](https://pygithub.readthedocs.io) · and with love hehe

⭐ **[Star this repo](https://github.com/Aafi04/reposition)** if Reposition saved you time.

</div>

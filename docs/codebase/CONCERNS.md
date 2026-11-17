# Codebase Concerns

## Core Sections (Required)

### 1) Top Risks (Prioritized)

| Severity | Concern | Evidence | Impact | Suggested action |
|----------|---------|----------|--------|------------------|
| high | No test suite or CI/CD detected; CONTRIBUTION.md requires only `uv run ruff` + `uv run mypy src/` before PR | Scan output (no CI/CD, no testing configs); `CONTRIBUTION.md` §7; `TESTING.md` | Regressions in compose/apply/decompile go undetected; refactoring is unsafe | Add a test framework (e.g. pytest) and CI pipeline; see `README.md` roadmap ("Document CI/CD and testing setup") — [ASK USER #1] |
| high | `rpa-toolkit` installed from a git commit SHA; if the reference becomes unreachable, `uv sync`/installs break | `pyproject.toml` (`rpa-toolkit @ git+https://github.com/regiellis/rpa-toolkit.git@435f202e...`) | Dependency availability / build breakage | Pin to a released PyPI version or vendor the package — [ASK USER #2] |
| med | `NVIDIA_API_KEY` secret handling; key must never be committed | `AGENTS.md` §3 (`mise.local.toml` gitignored, may hold `NVIDIA_API_KEY`); `.gitignore` (`mise.local.toml`) | Credential leakage if committed | Continue keeping `mise.local.toml` out of commits; never add real keys to tracked files |
| med | `opencode.json` is auto-generated and overwritten by the agent CLI on each profile launch; manual edits are not durable | `AGENTS.md` §3; `src/agent/cli.py` `_write_project_config` | Lost manual configuration changes | Treat `opencode.json` as generated; edit only for local testing |
| med | Modelfile `PARAMETER num_ctx` must match `context_limit` in `src/agent/config.json` profile entries | `AGENTS.md` §5 ("Match `PARAMETER num_ctx` ... to the `context_limit`") | Context-window monitoring may be inaccurate | Keep Modelfiles and `config.json` in sync when adding/adjusting profiles |
| med | Game assets and extracted archives are large and gitignored (not reproducible from repo) | `.gitignore` (`src/renpy_game/*`); scan CODE METRICS (`archive.rpa`: ~937 MB; composite PNGs ~9 MB each) | Converter state not versioned; large untracked artifacts | Keep `src/renpy_game/*` out of commits; document that users supply their own games |
| med | High churn in `converter/compose/` core modules indicates fragile, actively changing logic | Scan HIGH-CHURN FILES: `config.py` (14), `cli.py` (12), `character_associator.py` (11), `composer.py` (11), `applier.py` (10), `parser.py` (9), `conditions.py` (9), `interactive_review.py` (8) over last 90 days | Regression risk in the most important subpackage | Keep changes to compose/ small and well-reviewed; add tests before major refactors |

### 2) Technical Debt

| Debt item | Why it exists | Where | Risk if ignored | Suggested fix |
|-----------|---------------|-------|-----------------|---------------|
| No automated tests | Testing/CI deliberately deferred | Whole repo (`TESTING.md`); `README.md` roadmark unchecked | Undetected regressions | Add pytest suite for compose/apply/decompile; add CI — [ASK USER #1] |
| Unchecked roadmap items (ComfyUI integration, example workflow JSON, additional Modelfiles, CI/CD/testing docs) | Features planned but not yet implemented | `README.md` 🚧 Roadmap | Scope creep / stale roadmap | Prioritize or remove roadmap items — [ASK USER #3] |

### 3) Security Concerns

| Risk | OWASP category (if applicable) | Evidence | Current mitigation | Gap |
|------|--------------------------------|----------|--------------------|-----|
| `NVIDIA_API_KEY` exposure | N/A (secret management) | `AGENTS.md` §3; `.gitignore` (`mise.local.toml`) | `mise.local.toml` is gitignored; key read from env only | No formal secret injection/rotation; none detected |
| No auth on local NVIDIA proxy | N/A | `src/agent/nvidia_proxy.py` (proxy binds `127.0.0.1:8000`; health endpoint exposes pid) | Proxy only listens on loopback | No additional auth; loopback-only is the boundary |

### 4) Performance and Scaling Concerns

| Concern | Evidence | Current symptom | Scaling risk | Suggested improvement |
|---------|----------|-----------------|-------------|-----------------------|
| Large `archive.rpa` (~937 MB) and composite PNGs (~9 MB each) | Scan CODE METRICS largest files | Disk usage; extract/decompile I/O | Large games slow down decompile/compose | None — inherent to asset size |
| ~36 RPM NVIDIA proxy rate limit | `src/agent/nvidia_proxy.py` `MIN_INTERVAL_SECONDS = 1.65`; `AGENTS.md` §5 | Throughput capped for NVIDIA profiles | NVIDIA-bound workflows are slow by design | Do not bypass or raise the rate limit without a reason (`AGENTS.md` §5) |

### 5) Fragile/High-Churn Areas

| Area | Why fragile | Churn signal | Safe change strategy |
|------|-------------|-------------|----------------------|
| `src/converter/compose/config.py` | GameConfig schema drives compose/apply; parsing of `config.json` exclusions and categories is complex | 14 changes in last 90 days (scan HIGH-CHURN) | Change with tests; validate `config.json` round-trip |
| `src/converter/compose/composer.py` | Core compose orchestration with condition-variant enumeration | 11 changes | Small PRs; review condition logic carefully |
| `src/converter/compose/applier.py` | Rewrites live `.rpy` scripts; idempotency depends on exact statement text match | 10 changes | Verify on a copy; use `--dry-run` / restore first |
| `src/converter/compose/cli.py` / `character_associator.py` / `interactive_review.py` | Interactive CLI paths with user input | 12 / 11 / 8 changes | Keep interactive flows optional; cover with tests |
| `mise.toml` | Central task definitions; changes affect all developer workflows | 19 changes (highest churn) | Coordinate task changes |

### 6) `[ASK USER]` Questions

1. Is there a test suite planned, and which test framework (e.g. pytest) and CI provider should be adopted? The `README.md` roadmap lists "Document CI/CD and testing setup" as unchecked, and `CONTRIBUTION.md` requires only ruff + mypy today.
2. Should `rpa-toolkit` remain a git commit-pinned dependency, or should it be moved to a released PyPI version / vendored to avoid install breakage if the git reference becomes unavailable?
3. Are NVIDIA-hosted profiles intended for production/conversion use, or is Ollama the primary path? This affects where to prioritize testing and documentation effort.
4. Should the generated `docs/codebase/` documentation (these files) be committed to the repository, or kept as agent-workspace artifacts only?

### 7) Evidence

- Scan output `.codebase-scan.txt`: HIGH-CHURN FILES, CODE METRICS, CI/CD PIPELINES, PERFORMANCE & TESTING sections
- `pyproject.toml` (rpa-toolkit git dependency; dev group = mypy, ruff)
- `CONTRIBUTION.md` (§7 required checks)
- `README.md` (🚧 Roadmap)
- `AGENTS.md` (§3 secrets; §5 rate limit, Modelfile/ctx matching, gitignore)
- `src/converter/compose/config.py`, `composer.py`, `applier.py`, `cli.py`, `character_associator.py`, `interactive_review.py`
- `src/agent/nvidia_proxy.py`, `src/agent/cli.py`, `src/agent/config.py`
- `.gitignore`, `.python-version`, `mise.toml`

## Extended Sections (Optional)

None added.

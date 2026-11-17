# Coding Conventions

## Core Sections (Required)

### 1) Naming Rules

| Item | Rule | Example | Evidence |
|------|------|---------|----------|
| Files | snake_case, lowercase, `.py` | `character_associator.py`, `nvidia_proxy.py`, `interactive_review.py`, `sync_exclusions_cli.py` | `src/converter/compose/`, `src/converter/decompile/`, `src/agent/` file listing (scan DIRECTORY TREE) |
| Directories | lowercase, module/layer-based | `compose/`, `decompile/`, `restore/`, `utility/`, `Modelfiles/` | `src/converter/` tree |
| Functions/methods | snake_case | `find_game_folder`, `load_config`, `compose`, `apply`, `restore_rpy_files`, `_process_layeredimage_uses_from_index` | `src/converter/decompile/discovery.py`, `compose/applier.py` |
| Types/interfaces/dataclasses | PascalCase, typically frozen dataclasses | `GameConfig`, `CompositeManifest`, `CompositeRecord`, `Layer`, `UsageLocation`, `DefinitionLocation`, `AdoptionReport`, `RateLimiter`, `Profile` | `src/converter/compose/models.py`, `config.py`; `src/agent/config.py` |
| Constants/env vars | UPPER_SNAKE_CASE for module constants | `PROXY_HOST`, `PROXY_PORT`, `MIN_INTERVAL_SECONDS`, `CANONICAL_CATEGORIES`, `DEFAULT_CATEGORY_FOLDERS`, `NVIDIA_API_KEY` (env var) | `src/agent/nvidia_proxy.py`, `constants.py`; `src/converter/compose/config.py`; `AGENTS.md` §3 |
| Modelfiles | lowercase names under `src/agent/Modelfiles/` | `qwen3vl128`, `qwen3vl16`, `qwen3vl32`, `qwen3vl64` | `src/agent/Modelfiles/` directory tree |

### 2) Formatting and Linting

- Formatter: ruff (configured via `pyproject.toml` `[dependency-groups] dev`; no `.ruff.toml`/`ruff.toml` found at root — ruff defaults / pyproject config).
- Linter: ruff (same); type checker: mypy (`mypy>=2.4.1` dev dependency).
- Most relevant enforced rules: `uv run ruff` and `uv run mypy src/` are the required pre-PR checks (`CONTRIBUTING.md` §7); both must pass with zero errors/issues.
- Run commands: `uv run ruff`; `uv run mypy src/` (also `mise` tasks exist for agent/qwen/pipeline/etc., but no dedicated lint/type task is defined in `mise.toml`).

### 3) Import and Module Conventions

- Import grouping/order: relative imports within a package (`from .config import GameConfig`, `from .errors import ConfigError`); absolute package imports from the repo root for cross-package use (`from converter.compose.config import load_config`, `from converter.decompile.decompile import ...`).
- Alias vs relative import policy: relative imports preferred inside a package; no TypeScript-style path aliases (Python project; no `tsconfig.json` `paths`).
- Public exports/barrel policy: `__all__` used in `src/converter/__init__.py` (`["tqdm"]`), `src/converter/compose/models.py` (`__all__`), `src/converter/decompile/decompile.py` (`__all__` re-exporting the public API); `converter.compose.__init__` and `converter.restore.__init__` export public APIs.

### 4) Error and Logging Conventions

- Error strategy by layer:
  - Agent: `ConfigError` raised for missing/invalid profiles or project config; CLI logs errors and returns non-zero exit codes (`src/agent/config.py`, `cli.py`).
  - Compose: `ConfigError`, `ApplyError`, `ManifestError` in `src/converter/compose/errors.py`; apply pass skips unusable records rather than crashing, and records skips in the adoption report (`applier.py`).
  - Restore: `RestoreError` in `src/converter/restore/errors.py`; restore functions return `dict[str, str]` status per file (`"restored"`, `"would_restore"`, `"skipped: ..."`) instead of raising for individual file failures (`restore.py`).
  - Broad `except Exception` (noqa BLE001) is used in several places for defensive per-item continuation (`pipeline.py` `_restore_compose_game`, `restore.py`).
- Logging style and required context fields: `logging.getLogger(__name__)` module loggers; CLI uses `logging.basicConfig(level=logging.INFO, format="%(message)s")`; httpx logger suppressed to WARNING (`src/agent/cli.py` `main()`).
- Sensitive-data redaction rules: no explicit redaction rule found; `NVIDIA_API_KEY` is never written to `opencode.json` (proxy uses `apiKey: "local-proxy"`; the real key stays in the environment) — `src/agent/cli.py` `_write_project_config`.

### 5) Testing Conventions

- Test file naming/location rule: none — no test files, test directory, or test config detected (see `TESTING.md`).
- Mocking strategy norm: none established.
- Coverage expectation: none established.

### 6) Evidence

- `src/converter/compose/models.py` (naming, `__all__`, frozen dataclasses)
- `src/converter/compose/config.py` (constants, `GameConfig`)
- `src/agent/nvidia_proxy.py`, `src/agent/constants.py` (naming)
- `src/converter/compose/errors.py`, `src/converter/restore/errors.py`, `src/agent/config.py` (exceptions)
- `src/agent/cli.py` (logging setup, `opencode.json` handling)
- `CONTRIBUTING.md` §7 (required ruff + mypy checks)
- `.gitignore` (caches excluded from conventions evidence)

## Extended Sections (Optional)

Add only for large or inconsistent codebases:
- Language-specific strictness options: `[TODO]` — no `pyproject.toml` `[tool.ruff]` or `[tool.mypy]` section was found at root; ruff/mypy behavior relies on defaults/inline config.

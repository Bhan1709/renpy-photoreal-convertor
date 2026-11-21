# Testing Patterns

## Core Sections (Required)

### 1) Test Stack and Commands

- Primary test framework: pytest (`pyproject.toml`).
- Assertion/mocking tools: pytest assertions, `tmp_path`, `monkeypatch`, and
  Typer's `CliRunner`.
- Commands:

```bash
mise run test          # run the complete suite
uv run pytest          # equivalent direct command
uv run pytest tests/test_pipeline.py
```

CI runs the same suite after Ruff and mypy.

### 2) Test Layout

- Tests live under `tests/`.
- Files are named `test_<area>.py`.
- Shared helpers live in `tests/conftest.py`.
- Production code remains under `src/`.

| Test file | Coverage |
|-----------|----------|
| `tests/test_characterization.py` | Baseline behavior retained across the Phase 1 refactor |
| `tests/test_decompile_config.py` | User-setting preservation and atomic config generation |
| `tests/test_compose_config.py` | Category normalization, optional fields, and archive-root derivation |
| `tests/test_decompile_flow.py` | Canonical staging, dry-run behavior, and failure handling |
| `tests/test_discovery.py` | Source discovery and staged-output exclusion |
| `tests/test_extract.py` | Archive extraction failure reporting and path-traversal rejection |
| `tests/test_native_workspace.py` | Native workspace mirroring of configured archive roots |
| `tests/test_native_protocol.py` | Native candidate schema, pagination-counter, and CFG walk-state validation |
| `tests/test_pipeline.py` | Pipeline delegation, mode exclusivity, and dry-run behavior |
| `tests/test_phase2_manifest_transaction.py` | Atomic report publication, explicit paths, relative category references, and split-manifest consumers |
| `tests/test_phase2_native_identity.py` | Native content-hash/condition-state identity, unresolved errors, and CFG truncation state |
| `tests/test_staging.py` | Isolated staging and prior-extraction exclusion |
| `tests/test_cli.py` | CLI exit codes and root/subcommand option propagation |

### 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|-------|----------|----------------|-------|
| Unit | Yes | Config normalization, structured results, CLI helpers | Fast, isolated tests |
| Integration | Partial | Decompile preparation and pipeline boundaries | Heavy native/game operations are mocked |
| E2E | No | Full native Ren'Py compose/apply flow | A tiny synthetic Ren'Py fixture remains to be added |

### 4) Mocking and Isolation Strategy

- `tmp_path` provides isolated game and config directories.
- `monkeypatch` replaces discovery, extraction, decompilation, config loading,
  and compose boundaries without touching real game assets.
- Typer commands are exercised through `CliRunner`, including nonzero exit
  codes and option propagation.
- Tests must not read or write `src/renpy_game/` game assets.

### 5) Coverage and Quality Signals

- Coverage tooling and thresholds are not configured yet.
- CI quality gates: pytest, Ruff, and mypy.
- Known gaps: an end-to-end native Ren'Py render, interactive review, live-script
  apply, and real archive restore still need dedicated regression coverage.

### 6) Evidence

- `pyproject.toml`: pytest dependency and pytest configuration.
- `mise.toml`: `mise run test` task.
- `.github/workflows/ci.yml`: test step in CI.
- `tests/`: current automated test suite.

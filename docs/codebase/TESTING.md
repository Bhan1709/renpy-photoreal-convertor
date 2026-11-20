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
uv run pytest tests/test_characterization.py
```

CI runs the same suite after Ruff and mypy.

### 2) Test Layout

- Tests live under `tests/`.
- Files are named `test_<area>.py`.
- Shared helpers live in `tests/conftest.py`.
- Production code remains under `src/`.

| Test file | Coverage |
|-----------|----------|
| `tests/test_characterization.py` | Baseline discovery, staging, config, flow, and compose-only behavior |

### 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|-------|----------|----------------|-------|
| Unit | Partial | Discovery, staging, and configuration boundaries | Fast, isolated tests |
| Integration | No | Full decompile/compose workflows | Added in the next implementation phase |
| E2E | No | Full native Ren'Py compose/apply flow | A tiny synthetic Ren'Py fixture remains to be added |

### 4) Mocking and Isolation Strategy

- `tmp_path` provides isolated game and config directories.
- `monkeypatch` replaces discovery and compose boundaries without touching real
  game assets.
- Tests must not read or write `src/renpy_game/` game assets.

### 5) Coverage and Quality Signals

- Coverage tooling and thresholds are not configured yet.
- CI quality gates: pytest, Ruff, and mypy.
- Known gaps: native Ren'Py rendering, review, apply, restore, and real archive
  integration still need dedicated regression coverage.

### 6) Evidence

- `pyproject.toml`: pytest dependency and pytest configuration.
- `mise.toml`: `mise run test` task.
- `.github/workflows/ci.yml`: test step in CI.
- `tests/test_characterization.py`: baseline characterization suite.

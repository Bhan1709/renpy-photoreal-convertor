# Testing Patterns

## Core Sections (Required)

### 1) Test Stack and Commands

- Primary test framework: none detected ([TODO] — no test runner, test config, or test files found).
- Assertion/mocking tools: none detected ([TODO]).
- Commands:

```bash
[run all tests]      [TODO] — no test command defined
[run unit tests]     [TODO]
[run integration/e2e tests]  [TODO]
[run coverage]       [TODO]
```

### 2) Test Layout

- Test file placement pattern: none — no `tests/`, `__tests__/`, `spec/`, or co-located `test_*.py` files detected in the scan (production TODOs excluded; scan searched source extensions only).
- Naming convention: [TODO].
- Setup files and where they run: [TODO].

### 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|-------|----------|----------------|-------|
| Unit | [TODO] | [modules/services] | No test files detected |
| Integration | [TODO] | [API/data boundaries] | No test files detected |
| E2E | [TODO] | [user flows] | No test files detected |

### 4) Mocking and Isolation Strategy

- Main mocking approach: [TODO] — no mocking library or pattern detected.
- Isolation guarantees: [TODO].
- Common failure mode in tests: [TODO].

### 5) Coverage and Quality Signals

- Coverage tool + threshold: [TODO] — no coverage config detected.
- Current reported coverage: [TODO].
- Known gaps/flaky areas: entire test suite is absent — every module (`decompile`, `compose`, `restore`, `agent`, `pipeline`) is untested by automated tests.

### 6) Evidence

- Scan output: `PERFORMANCE & TESTING` — "No performance testing configs detected."; `CI/CD PIPELINES` — "No CI/CD pipelines detected."
- `CONTRIBUTING.md` §7: required pre-PR checks are `uv run ruff` and `uv run mypy src/` (lint + type check only; no test command).
- `README.md` 🚧 Roadmap: "Document CI/CD and testing setup" is listed as unchecked — confirms testing/CI is intentionally deferred.
- `pyproject.toml`: dev dependency group contains only `mypy` and `ruff`; no test framework (e.g. pytest) is listed.

## Extended Sections (Optional)

Add only when needed:
- Historical flaky test catalog: [TODO] — no tests exist yet.

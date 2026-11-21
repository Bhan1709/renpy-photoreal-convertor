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
| `tests/test_native_workspace.py` | Native workspace mirroring of configured archive roots, independent asset copies, linked-member rejection, setup-failure cleanup, and idempotent cleanup |
| `tests/test_native_protocol.py` | Native candidate schema, safe render references, pagination-counter, CFG walk-state, schema-2 JSONL completeness, trailing/mixed record, worker identity, and error-envelope validation |
| `tests/test_pipeline.py` | Pipeline delegation, mode exclusivity, and dry-run behavior |
| `tests/test_phase2_manifest_transaction.py` | Atomic report publication, explicit paths, relative category references, reserved support-path rejection, and split-manifest consumers |
| `tests/test_phase2_native_identity.py` | Native content-hash/condition-state identity, unresolved errors, and CFG truncation state |
| `tests/test_phase2_bundle_reliability.py` | Native result isolation, batch identity checks, contextual diagnostics, bundle locks, digests, atomic review transactions, owner-authenticated report-journal recovery, reserved support-path rejection, alias-safe peer exclusion, and interrupt-safe rollback |
| `tests/test_phase2_path_safety.py` | Manifest/archive path traversal rejection, resolved containment, generated-image-root restrictions, apply/restore safety, and destructive-root boundaries |
| `tests/test_phase2_review_rejection.py` | Deferred rejected-asset/record finalization, report-file deletion prevention, symlinked-destination rejection, and failed-review rollback behavior |
| `tests/test_phase3_native_optimization.py` | Global state/variant budgets, condition-variant priority, discovery fingerprint/cache reuse and invalidation, mode aliases/defaults, persistent-worker identity/nonces/stale cleanup/fallback, credential-free child environments, deterministic one-shot retry, PNG CAS/hard-link publication, shared `.rpy`/`*_ren.py` retention and replacement, v2 apply preflight, and published-bundle-aware stale pruning |
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
  worker startup, one-shot child execution, and compose boundaries without
  touching real game assets.
- Selected pure functions from the injected Ren'Py runtime are parsed from its
  ``.rpy`` source and executed in an isolated namespace, allowing budget,
  condition-assignment, cache-signature, and cache round-trip behavior to be
  tested without booting a graphical runtime.
- Typer commands are exercised through `CliRunner`, including nonzero exit
  codes and option propagation.
- Tests must not read or write `src/renpy_game/` game assets.

### 5) Coverage and Quality Signals

- Coverage tooling and thresholds are not configured yet.
- CI quality gates: pytest, Ruff, and mypy.
- Known gaps: an end-to-end native Ren'Py render against a bundled runtime,
  interactive review, live-script apply, and real archive restore still need
  dedicated regression coverage.

### 6) Evidence

- `pyproject.toml`: pytest dependency and pytest configuration.
- `mise.toml`: `mise run test` task.
- `.github/workflows/ci.yml`: test step in CI.
- `tests/`: current automated test suite.

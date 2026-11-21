# Architecture

## Core Sections (Required)

### 1) Architectural Style

- Primary style: modular, config-driven pipeline with two top-level modules (`agent` and `converter`).
- Why this classification: source tree is split into `src/agent/` (OpenCode orchestration) and `src/converter/` (Ren'Py conversion toolkit with `decompile`/`compose`/`restore` subpackages); the canonical converter stages run through `src/converter/decompile/flow.py` and `src/converter/pipeline.py`, sharing the newly generated `src/converter/config.json` (game config).
- Primary constraints:
  - No `--game` argument: all stages are config-driven via `src/converter/config.json` (produced by decompile).
  - Game assets live under `src/renpy_game/` (gitignored); the consolidated work folder `src/renpy_game/game/` is regenerated on each decompile run.
  - NVIDIA proxy rate limit ~36 RPM must be respected (stay below NVIDIA's 40 RPM quota).

### 2) System Flow

```text
user places game under src/renpy_game/<game>/
        |
        v
decompile (src/converter/decompile/):
  find_game_folder() -> stage .rpa/.rpyc/.rpy -> extract .rpa alongside ->
  decompile compiled scripts -> scan image categories ->
  write src/converter/config.json
        |
        v
compose (src/converter/compose/):
  index_scripts() -> create isolated native workspace ->
  enumerate/render states with the bundled Ren'Py runtime ->
  publish per-category manifests + manifest_index.json atomically under the configured report_dir
        |
        v
apply (src/converter/compose/applier.py):
  rewrite static show/scene statements to composite tags ->
  emit ConditionSwitch wrappers for dynamic state ->
  declare composites in game/composite_images.rpy -> write adoption report
        |
        v
restore (src/converter/restore/):
  revert modified .rpy files from archive.rpa (scoped by manifest or all .rpy)
```

Describe the flow in 4-6 steps using file-backed evidence:
1. `src/converter/decompile/discovery.py` `find_game_folder()` locates the first `game` folder under `src/renpy_game` (excluding the consolidated `src/renpy_game/game` output).
2. `src/converter/decompile/flow.py` `prepare_game()` runs find → stage → extract → decompile → detect categories → write `config.json` without modifying the source game; `process_game()` adds the optional interactive compose follow-up.
3. `src/converter/pipeline.py` `run_pipeline()` delegates to `prepare_game()`, loads the config written by that same run, and invokes compose (apply remains deferred for review).
4. `src/converter/compose/compose_pass/orchestrate.py` `Composer.compose()` delegates to the native Ren'Py stage, which indexes scripts, creates an isolated workspace, enumerates runtime states, renders through the bundled Ren'Py runtime, and publishes the split manifests in one report-directory-locked, journaled transaction. The versioned index stores relative category references and required per-category SHA-256 digests, and is replaced last as the commit marker. Every index sharing the report directory also shares one durable journal. A report-scoped owner registry authenticates the participant index, allowing a peer to recover an interrupted publication without trusting journal metadata alone. The hidden journal and owner-registry paths are reserved from index and owner registration, including resolved aliases. Recovery peer exclusions also use resolved path identity, so a symlink or junction cannot make one participant authorize another participant's index; same-participant scopes retain both lexical and parent-resolved index paths for crash recovery. Malformed legacy journal names are ignored. Bundle readers recover an interrupted journal and verify the digests before consuming category data. Recovery is commit-aware: if every staged digest is already present, the journal is discarded without rolling the completed bundle back.
5. `src/converter/compose/apply_pass/applier.py` `Applier.apply()` loads manifests, rewrites `show`/`scene` statements to composite tags, emits `ConditionSwitch` wrappers for dynamic state, writes `game/composite_images.rpy` declarations and an adoption report; idempotent (refuses to touch a line whose text no longer matches the recorded statement). Apply, review, and restore use bundle locks, while apply and restore also share a game-script mutation lock.
6. `src/converter/restore/restore.py` provides archive-scoped and source-tree restoration paths; the compose CLI currently uses source-tree restoration. Restored scripts are replaced atomically.

### 3) Layer/Module Responsibilities

| Layer or module | Owns | Must not own | Evidence |
|-----------------|------|--------------|----------|
| `src/agent/` | Profile loading/validation, CLI launch, NVIDIA proxy, conversation monitor, `opencode.json` writing | Ren'Py decompile/compose/restore | `src/agent/README.md`; `src/agent/cli.py` |
| `src/agent/config.py` | Load/validate bundled model profiles from `src/agent/config.json` (provider, model_name, context_limit) | Network I/O, OpenCode launch | `src/agent/config.py` |
| `src/agent/nvidia_proxy.py` | FastAPI reverse proxy to `integrate.api.nvidia.com`, ~36 RPM rate limiter, retry on 429 | Profile selection, Ollama, conversation monitoring | `src/agent/nvidia_proxy.py` |
| `src/agent/monitor.py` | Context-window monitor; saves Markdown backups to `logs/conversations/` near context limit | Proxy, profile loading | `AGENTS.md` §5; `src/agent/cli.py` (`ConversationMonitor`) |
| `src/converter/pipeline.py` | End-to-end staged decompile → compose orchestration | Image compositing internals, proxy | `src/converter/pipeline.py` |
| `src/converter/decompile/` | Game-folder discovery, `.rpa` extraction, `.rpyc` decompilation, image-category scan, `config.json` generation | Compositing, script rewriting | `src/converter/decompile/README.md` |
| `src/converter/compose/` | Script parsing, path resolution, condition evaluation, compositing, manifests, apply, declarations | Game discovery, `.rpa` extraction, OpenCode | `src/converter/compose/README.md` |
| `src/converter/utility/manifest_bundle.py` | Report-wide bundle locking, game-script locking, owner authentication, reserved-path validation, and journal recovery | Manifest business semantics and digest calculation | `src/converter/utility/manifest_bundle.py` |
| `src/converter/utility/safe_paths.py` | Portable path syntax validation and resolved live-root containment | Manifest category-reference semantics | `src/converter/utility/safe_paths.py` |
| `src/converter/restore/` | Revert `.rpy` from `archive.rpa`; undo decompile | Creating composites | `src/converter/restore/restore.py` |
| `src/converter/config.json` (generated) | GameConfig: game folder, rpy_files, extracted archives, image categories | Runtime state (written once by decompile) | `src/converter/config.json`; `src/converter/compose/config.py` |

### 4) Reused Patterns

| Pattern | Where found | Why it exists |
|---------|-------------|---------------|
| Dependency injection via explicit `GameConfig` | `converter.compose.config.GameConfig`, passed to `Composer`, `Applier`, `Resolver`, `Compositor`, `CharacterAssociator` | No module-level mutable global state; thread-safe and unit-testable (`src/converter/compose/README.md` design principles) |
| Frozen typed domain models | `src/converter/compose/models.py` (`Layer`, `CompositeRecord`, `CompositeManifest`, `AdoptionRecord`, `AdoptionReport`) with `replace()`/`field_replace` for immutable updates | Explicit, self-documenting data; immutable updates |
| Strategy pattern | `converter.compose.compositing.Compositor` (CPU/PIL `CompositorStrategy` protocol) | Compositing backend interchangeable |
| Custom exception hierarchy | `src/converter/compose/errors.py` (`ConfigError`, `ApplyError`, `ManifestError`); `src/converter/restore/errors.py` (`RestoreError`); `src/agent/config.py` (`ConfigError`) | Structured error handling per layer |
| Single-responsibility modules | One concern per module (`parsing`, `resolution`, `conditions`, `compositing`, `association`, `records`, `compose_pass`, `apply_pass`, `manifest`, `regexp`, `review`) | `src/converter/compose/README.md` design principles |
| Rate-limiting reverse proxy | `src/agent/nvidia_proxy.py` (`RateLimiter`, ~36 RPM, 429 retry) | Stay under NVIDIA 40 RPM quota (`AGENTS.md` §5) |
| Context-window monitor + Markdown backups | `src/agent/monitor.py` (`ConversationMonitor`, backups under `logs/conversations/`) | Preserve conversation state near context limit (`AGENTS.md` §5) |

### 5) Known Architectural Risks

- Automated tests and CI now cover orchestration/config boundaries, manifest transactions, bundle locking/journal recovery, native identity/state adaptation, and split-manifest consumers, but native rendering and interactive review still lack end-to-end coverage (see `TESTING.md`).
- `rpa-toolkit` is a git dependency pinned to a commit SHA (`pyproject.toml`); if the git reference becomes unavailable, installs break.
- `opencode.json` is auto-generated and overwritten by the agent CLI on each profile launch; manual edits are not durable (`AGENTS.md` §3; `src/agent/cli.py`).
- Modelfile `PARAMETER num_ctx` must match `context_limit` in `src/agent/config.json` profile entries, or context-window monitoring may be inaccurate (`AGENTS.md` §5).
- `src/renpy_game/*` is gitignored (except `.gitkeep`); game assets and extracted archives are never committed, so the converter state is not reproducible from the repo alone (`AGENTS.md` §5; `.gitignore`).

### 6) Evidence

- `src/converter/pipeline.py`
- `src/converter/compose/compose_pass/orchestrate.py`
- `src/converter/compose/apply_pass/applier.py`
- `src/converter/compose/models.py`
- `src/converter/compose/README.md` (design principles, module responsibility division)
- `src/converter/decompile/README.md`
- `src/converter/restore/restore.py`
- `src/agent/nvidia_proxy.py`, `src/agent/cli.py`, `src/agent/config.py`, `src/agent/monitor.py`
- `AGENTS.md` (§3, §5)
- `CONTRIBUTING.md`

## Extended Sections (Optional)

None added — repository complexity does not justify optional sections.

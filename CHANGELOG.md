# Changelog

All notable changes to the Ren'Py Photoreal VN Converter are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Compose: durable native discovery, pixel-render, and byte-level PNG CAS caches under `<output_dir>/.compose-native-cache/`, with fingerprint-based discovery reuse and safe invalidation.
- Compose: optional persistent native worker with workspace-local job/result isolation, schema-2 identity validation, and automatic one-shot fallback.
- Compose: configurable global native state and optional-variant budgets, with mandatory CFG overflow reported explicitly and optional variants truncated deterministically.
- `docs/codebase/` — codebase knowledge documentation (stack, structure, architecture, conventions, integrations, testing, concerns)
- `.github/copilot-instructions.md` — pointer to `AGENTS.md`
- `.github/agents/spec-conformance.agent.md` and `.github/agents/blast-radius.agent.md` — reviewer agents
- `.github/workflows/ci.yml` — ruff + mypy checks on pull requests and pushes
- `.github/ISSUE_TEMPLATE/` — bug report and feature request forms
- `.github/PULL_REQUEST_TEMPLATE.md` — pull request template

### Changed

- Compose: native pages now stream as complete schema-2 JSONL (header, candidates, footer) and are validated through a disk-backed spool before any candidate is adapted, reducing host memory use while retaining pagination, CFG, diagnostics, and batch-identity checks.
- Compose: native workspaces now copy scripts and assets independently and reject linked archive members, so staged Ren'Py code cannot write through a shared inode into the live game tree.
- Compose: native child processes now receive an explicit platform/locale environment instead of inheriting host API keys, tokens, Python import paths, and unrelated Ren'Py settings.
- Compose: native discovery now builds one image-tag/AST map per child, hoists condition assignments per layered image, prioritizes condition variants before synthetic inventory, and adapts candidates one batch at a time.
- Compose: rendered PNGs are published through an atomic hard-link-then-move or copy-then-move path while preserving shared render/CAS sources; the native pixel `content_hash` remains manifest identity and encoded-byte SHA-256 identifies CAS objects.
- Compose: successful compose/apply publications now prune only unreferenced regular PNGs in valid generated-image layouts, after reloading and digest-verifying the published manifest bundle and retaining live-script references.
- Compose: split manifest bundles now publish atomically with relative category references, versioned SHA-256 integrity metadata, report-directory-wide locking, one report-scoped durable crash-recovery journal, a report-scoped authenticated owner registry, and single-transaction interactive review updates.
- Compose: removed the exclusion-pattern workflow. Script/image exclusion prompts, syncing, and filtering are gone; compose stages and scans every live script in the game folder, so deleting a script from that folder is all that is needed. Generated `config.json` no longer contains an `exclusion` block.

### Fixed

- Compose: native streaming now rejects incomplete, trailing, mixed legacy/streaming, malformed, wrong-coordinate, and wrong-worker-identity result files before candidate adaptation; error envelopes carry the same validated identity, and native mode aliases and omitted/empty compatibility defaults are normalized consistently.
- Compose: persistent-worker submissions now use unique counter/nonce tokens, stale queue/result files are cleared on worker restart, stale completion markers cannot satisfy a newer job, and progress files must remain inside the disposable workspace.
- Compose: stale-image pruning and global generated-path replacement now share one live-script boundary that includes Ren'Py `*_ren.py` translation modules; apply now rejects a downgraded bundle before live mutation, and post-commit pruning failures are reported without implying that the published bundle was rolled back.
- Compose: native cache and PNG publication now reject linked cache paths, verify cached pixel/byte identity, validate PNG signatures and dimensions, preserve existing targets on copy failure, and keep shared render/CAS sources intact.
- Compose: report-journal recovery now authenticates outside owner indexes through a report-scoped registry; journal metadata alone can no longer expand the set of files a recovery may modify. Legacy sibling journals also cannot reinterpret the hidden shared journal or owner registry as recoverable indexes, and resolved path aliases cannot bypass peer-index exclusions. Same-participant recovery still accepts canonical targets through a parent-directory alias, while malformed legacy journal names are ignored instead of aborting recovery.
- Compose/restore: the hidden journal and owner-registry paths are reserved from manifest-index and owner registration, including resolved aliases; publishers and consumers reject those collisions before reading or mutating the bundle.
- Compose/restore: manifest-derived live script/composite paths, archive members, and pending-state paths now reject absolute, drive-qualified, parent-traversing, and symlink-escaping targets before reading, replacing, moving, or deleting files. Generated-image references are restricted to generated image roots and cannot delete report or rejected-item files. Legacy absolute category references remain readable, but cannot authorize recovery writes outside the report scope.
- Compose: atomic bundle rollback now records each potentially replaced target before replacement, so asynchronous interruption cannot leave a mixed bundle without recovery state.
- Compose: rejected scene images and speaker records are finalized only after the review bundle commits, so a failed transaction cannot remove an image still referenced by the old manifest or leave a false rejection record.
- Compose: native result files are isolated inside disposable workspaces, stale results are cleared before every attempt, and returned batch identity and render references are validated before use.
- Compose: native condition and render diagnostics now retain candidate, image, and usage context and are deduplicated by contextual identity.
- Agent launcher: `Ctrl+C` no longer leaves the terminal (or VS Code) unresponsive when OpenCode's TUI ignores it or dies in raw console mode. The launcher now waits interruptibly, stops the whole OpenCode process tree, restores the console input mode, and handles `Ctrl+Break` as a forced interrupt.
- Compose: native rendering now shows one progress bar for all batches, using the actual total reported by the Ren'Py runtime instead of `?`, with a spinner while the runtime enumerates states. Batch status updates in place on the bar instead of printing a new line per batch.
- Compose: native batches now render slices of one frozen state list, so the total stays constant and states cannot be skipped or duplicated across batches. The cache interns shared usage lists so it stays small and loads quickly.
- CLI progress bars: the bar now repaints only when its data changes, so external terminal output (such as mise's interrupt notice) cannot leave a duplicate bar behind. The compose bar also clears itself on close and prints one final summary on success.

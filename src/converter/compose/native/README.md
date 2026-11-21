# Native compose stage (compose v2)

The native stage extracts and renders composites **inside the game's own Ren'Py
runtime** instead of re-implementing image composition in Python. It replaces
only the extraction/rendering part of the compose pass: the CLI, review, apply,
and restore workflows, and the split-manifest contract are unchanged.

The wire protocol remains `renpy-compose-native-v2`, schema version `2`. The
published manifest remains bundle version `2`, with stable category filenames
and per-category SHA-256 digests.

## How it works

1. **Discover** the bundled Ren'Py runtime next to the game
   (`renpy/` + `lib/` + launcher), including its bundled Python interpreter.
2. **Stage a disposable workspace** next to the game root. Configured
   `.rpy`/`_ren.py` scripts and extracted assets are copied as independent
   regular files, so a write by the game's own Python code cannot modify the
   live tree through a shared inode. Symbolic-link and junction archive members
   are rejected rather than followed.
3. **Fingerprint discovery inputs** using the live script contents and names,
   runtime version, normalized modes, game defaults, global budgets, protocol
   metadata, and discovery-cache format. The disposable workspace path is
   deliberately excluded, so an unchanged game can reuse its durable cache on
   a later run.
4. **Reuse the durable discovery cache** when its signature and fingerprint
   match. The cache stores the frozen state inventory, CFG walk state, layered
   image metadata, image definitions, characters, state counts, and budget
   metadata. A mismatched, malformed, or incomplete cache is ignored and
   rediscovered.
5. **Discover inside the child**. The injected command builds one image-tag to
   AST-node map per child process, walks compiled labels while applying
   `show`/`scene` statements, discovers `LayeredImage` objects and
   `ADVCharacter` instances from runtime registries, and expands condition
   variants. Condition assignments are computed once per layered image and
   reused for its discovered attribute combinations.
6. **Enforce global budgets before rendering**. Mandatory CFG states fail
   explicitly if they exceed `native_max_total_states`. Optional condition and
   synthetic variants are truncated deterministically when either global state
   or variant budget is reached. Condition variants are reserved before the
   much larger synthetic `layered_all` inventory.
7. **Render through the real displayables** with the software renderer. Pixels
   are hashed as canonical RGBA plus dimensions; that native `content_hash` is
   the manifest identity. Encoded PNG bytes are stored separately in a
   byte-level SHA-256 content-addressed store (CAS), so the manifest identity
   and the cache-object identity remain distinct.
8. **Stream one schema-2 JSONL batch** as exactly one header record, zero or
   more candidate records, and one footer record. The child writes to a
   temporary sibling and atomically replaces the result only after the footer
   is durable. The host validates the complete stream through a disk-backed
   candidate spool before forwarding any candidate to adaptation.
9. **Adapt one batch at a time**. Raw candidates are not accumulated across
   pages. Each validated candidate is deduplicated by normalized layer
   semantics plus native content hash and condition state, then its PNG is
   published by an atomic hard-link-then-move (or copy-then-move fallback).
   Shared render-store and CAS sources are never consumed.
10. **Publish the split manifest bundle** through `ManifestWriter` in one
    locked, journaled transaction. The native compose stage is the initial
    manifest publisher; review and apply may subsequently update that bundle.
11. **Prune stale generated images only after successful publication**. The
    pruner reloads and digest-verifies the published bundle, validates every
    manifest reference, retains live-script references conservatively, and
    removes only unreferenced PNGs in valid `static/<family>/...` and
    `dynamic_review/<family>/...` locations. Report/rejected roots, cache
    artifacts, non-PNG files, symlinks, and non-generated layouts are not
    pruning candidates.

## Modes

| Canonical mode | Meaning |
|----------------|---------|
| `cfg` | Every `show`/`scene` state reachable from the compiled labels (mandatory baseline). |
| `layered_all` | Additional synthetic attribute combinations per layered image, sampled evenly across attribute groups. |
| `conditions` | Variable-driven variants for layered-image conditions. |

`cfg` is always included. Omitted or empty mode settings retain the legacy
all-modes compatibility default (`cfg`, `layered_all`, `conditions`). Legacy
aliases remain accepted:

| Legacy name | Canonical expansion |
|-------------|--------------------|
| `all` | `cfg`, `layered_all`, `conditions` |
| `condition` | `cfg`, `conditions` |
| `layered` | `cfg`, `layered_all` |
| `declaration` | `cfg` |
| `screen` | `cfg` |

Results are merged deterministically; condition output contains only dynamic
entries (deterministic `const` states are omitted).

## Configuration

The compose config accepts these native performance and retention settings:

- `compose_modes` — canonical modes or supported legacy aliases described
  above.
- `native_max_total_states` — global cap for the complete state inventory
  (default `100000`). Mandatory CFG overflow is an explicit failure.
- `native_max_total_variants` — global cap for optional condition/synthetic
  variants (default `250000`). Optional inventory is truncated
  deterministically.
- `native_persistent_worker` — reuse one Ren'Py worker across render pages
  when supported (default `true`). The host falls back to one-shot children if
  the worker cannot start or fails.
- `native_cache_enabled` — retain discovery, render, and PNG CAS artifacts
  between compose runs (default `true`).

These user-controlled fields survive a decompile rerun.

## Persistent artifacts

When caching is enabled, artifacts live under:

```text
<output_dir>/.compose-native-cache/
├── discovery.json     # frozen discovery inventory and metadata
├── renders/           # native pixel content_hash -> PNG
└── png-cas/           # encoded-byte SHA-256 -> PNG
```

The cache is outside `static/` and `dynamic_review/`, so stale-output pruning
never treats it as generated game output. Symlink or junction aliases are
rejected for cache directories and objects. Existing render PNGs are verified
against their native pixel hash before reuse; corrupt CAS objects are never
published or silently overwritten.

## Persistent worker and fallback

The preferred execution path starts one `composite_export_worker` process and
submits workspace-local jobs. Every job has a queue token containing a
submission counter and random nonce, and carries the logical native batch
identity (`session_id`, `job_id`, `attempt`). Result files and completion
markers are isolated per submission. Stale queue/result files are cleared when
the worker starts, stale markers cannot satisfy a newer submission, and the
completed stream's identity is validated by the host.

Native children receive an explicit platform/locale environment rather than
the converter's full environment. Host API keys, tokens, Python import paths,
and unrelated `RENPY_*` settings are not inherited.

If worker startup or execution is unavailable, the same logical batch is
retried through a one-shot `composite_export` child. One-shot renderer
failures retry from the same state offset with a halved batch size, down to the
configured floor. Deterministic limit and protocol failures are not hidden by
the fallback.

## Safety limits (`constants.py`)

- `MAX_SYNTHETIC_ATTRIBUTE_STATES` — soft cap of synthetic states per layered
  image (`1_024`).
- `MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE` — condition variants per layered
  image (`64`).
- `MAX_RENDER_STATES_PER_BATCH` — render cap per child page (`512`).
- `MIN_RENDER_STATES_PER_BATCH` — lower bound for deterministic one-shot
  retry batches (`128`).
- `MAX_TOTAL_RENDER_STATES` / `MAX_TOTAL_VARIANTS` — global state and optional
  variant caps (`100000` / `250000`).
- `FREE_MEMORY_RENDER_INTERVAL` — `renpy.exports.free_memory()` is called every
  250 renders and at the end of every batch.
- `MAX_CFG_PROCESSED_NODES` / `MAX_CFG_CALL_DEPTH` — runaway-loop guards for
  the label walk. If the node budget truncates the walk, the manifest records
  `cfg_truncated: true` and the processed-node count.

## Rendering and publication details

- The software renderer is forced (`config.gl2 = False`, preferences/session
  renderer `sw`); captures use `renpy.display.render.render()` plus
  `renpy.display.draw.render_to_texture()`.
- Deduplication hashes **canonical RGBA** pixels plus dimensions (`SHA-256`).
  SDL byte order is normalized before hashing, with a per-pixel fallback.
- Render-store writes are staged with a `.png` suffix and atomically replaced,
  so pygame selects the PNG encoder and a crash cannot expose a partial cache
  object.
- A state that renders to 0×0 (every layer hidden) is reported as an *empty*
  render, not an error, and simply produces no composite.
- Layer provenance comes from the `LayeredImage` layers selected by the
  attributes and applied condition variables. Condition-evaluation failures
  are reported in `unresolved_errors` and carried into the manifest instead of
  being silently treated as false.
- PNG publication validates the PNG signature/IHDR and expected dimensions,
  rejects unsafe or corrupt sources, and replaces the target atomically. A
  failed publication leaves the previous target intact.

## Public API

```python
from converter.compose.native.stage import run_composite_stage

index_path = run_composite_stage(
    vn_root,            # Ren'Py runtime root (renpy/, lib/, game/)
    output_dir,         # receives composites/ and reports/
    ["cfg", "conditions"],
)
# -> configured manifest_index_path (default: <output_dir>/reports/manifest_index.json)
```

`Composer.compose()` calls the same code path with the game's `GameConfig`.

## Requirements

- The game must ship its Ren'Py runtime (the decompile config's `source_path`).
- Ren'Py 7.8.2 and 8.3.2 are supported baselines; the injected script stays
  Python 2/3 compatible.
- The original game tree is never modified by staged files: scripts and
  assets are copied independently, linked archive members are rejected, and
  the workspace is removed when the stage finishes.

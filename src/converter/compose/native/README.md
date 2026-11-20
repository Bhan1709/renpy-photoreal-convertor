# Native compose stage (compose v2)

The native stage extracts and renders composites **inside the game's own Ren'Py
runtime** instead of re-implementing image composition in Python. It replaces
only the extraction/rendering part of the compose pass: the CLI, review, apply,
and restore workflows, and the split-manifest contract are unchanged.

## How it works

1. **Discover** the bundled Ren'Py runtime next to the game
   (`renpy/` + `lib/` + launcher), including its bundled Python interpreter.
2. **Stage a disposable workspace** next to the game root:
   the configured `.rpy`/`_ren.py` scripts are *copied* (so Ren'Py may compile
   them without touching the live game) and the extracted asset trees are
   mirrored with hard links where the filesystem allows it.
3. **Inject** `runtime/composite_export.rpy` as `game/_composite_export.rpy`.
   It registers a `composite_export` command and is removed with the workspace.
4. **Run headlessly**: `RENPY_RENDERER=sw`, `SDL_VIDEODRIVER=dummy`,
   `SDL_AUDIODRIVER=dummy`, `PYGAME_SDL2_AVOID_GL=1`, saves redirected into the
   workspace, `config.save`/`config.save_persistent` disabled.
5. **Extract** inside the child: the injected command starts the display
   (software renderer), walks every compiled label of the CFG applying
   `show`/`scene` statements in source order (tracking sticky attributes,
   entering branch bodies, inlining called labels), discovers `LayeredImage`
   objects and `ADVCharacter` instances from the runtime registries, expands
   condition variants, and renders every state through the real displayables.
6. **Report** one JSON result per batch (`RENPY_COMPOSITE_CONFIG` /
   `RENPY_COMPOSITE_OUTPUT`), validated by `protocol.py`.
7. **Adapt** (host side, `stage.py`): batches are paged until
   `batch.next_offset` is `None`, candidates are deduplicated by their
   normalised layer semantics, rendered PNGs are copied out of the workspace,
   and everything is turned into the existing `CompositeRecord` /
   `CompositeManifest` objects before `ManifestWriter` writes the split
   manifests. Screen-stack composites (Pass 3) are still produced statically and
   merged into the same manifest.

The child always exits through `run_command()`, which writes an error result
and terminates with a non-zero status on failure, so a half-finished batch is
never mistaken for success.

## Modes

| Mode | Meaning |
|------|---------|
| `cfg` | Every `show`/`scene` state reachable from the compiled labels (mandatory). |
| `layered_all` | Additional synthetic attribute combinations per layered image, sampled evenly across attribute groups. |
| `conditions` | Variable-driven variants for layered-image conditions. |

Results are merged deterministically; condition output contains only dynamic
entries (deterministic `const` states are omitted).

## Safety limits (`constants.py`)

- `MAX_SYNTHETIC_ATTRIBUTE_STATES` — soft cap of synthetic states per layered
  image (`1_024`); CFG states are never capped away.
- `MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE` — condition variants per layered
  image (`64`).
- `MAX_RENDER_STATES_PER_BATCH` — render cap per subprocess (`4_096`) with
  sequential batch pagination and merged manifests. A child that dies mid-batch
  is retried from the same offset with a halved sub-batch
  (`MIN_RENDER_STATES_PER_BATCH`).
- `FREE_MEMORY_RENDER_INTERVAL` — `renpy.exports.free_memory()` is called every
  250 renders and at the end of every batch.
- `MAX_CFG_PROCESSED_NODES` / `MAX_CFG_CALL_DEPTH` — runaway-loop guards for the
  label walk.

## Rendering details

- The software renderer is forced (`config.gl2 = False`, preferences/session
  renderer `sw`); captures use `renpy.display.render.render()` plus
  `renpy.display.draw.render_to_texture()`.
- Deduplication hashes **canonical RGBA** pixels plus the dimensions
  (`SHA-256`). Surfaces arrive in whatever byte order SDL picked (BGRA on
  Windows), so the R/B planes are swapped with a C-speed slice assignment
  before hashing; the per-pixel `get_at` path remains as a fallback.
- A state that renders to 0×0 (every layer hidden) is reported as an *empty*
  render, not an error, and simply produces no composite.
- Layer provenance comes from the `LayeredImage` layers that the selected
  attributes and the applied condition variables actually pick; conditions that
  fail to evaluate are reported as unresolved so the record is flagged
  `dynamic_review`, exactly as the static pipeline did.

## Public API

```python
from converter.compose.native.stage import run_composite_stage

index_path = run_composite_stage(
    vn_root,            # Ren'Py runtime root (renpy/, lib/, game/)
    output_dir,         # receives composites/ and reports/
    ["cfg", "layered_all", "conditions"],
)
# -> <output_dir>/reports/manifest_index.json
```

`Composer.compose()` calls the same code path with the game's `GameConfig`.

## Requirements

- The game must ship its Ren'Py runtime (the decompile config's `source_path`).
- Ren'Py 7.8.2 and 8.3.2 are supported baselines; the injected script stays
  Python 2/3 compatible.
- The original game tree is never modified: scripts are copied, assets are
  hard-linked, and the workspace is removed when the stage finishes.

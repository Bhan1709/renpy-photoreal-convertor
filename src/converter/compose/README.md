# src\converter\compose\ - Ren'Py Composite Compose Subpackage

## Overview

This subpackage turns a Ren'Py game's 2D assets into reviewable, photorealistic-ready
composites and adopts them back into the game's live scripts.

Every Ren'Py game is modelled as four canonical component categories:

| Category | Meaning           | Default folder (sample game) |
|----------|-------------------|------------------------------|
| ``bg``   | Backgrounds       | ``bgs``                      |
| ``char`` | Characters / sprites | ``chars``                  |
| ``scene``| Scenes / CGs      | ``xxx``                      |
| ``gui``  | User interface    | ``gui``                      |

The mapping from canonical category to on-disk folder is **game-specific** and is
captured in a :class:`GameConfig`.  Nothing about a particular game is hard-coded
in the compose or apply logic.

The flow is:

1. **Compose** (v2, native): extraction and rendering run **inside the game's
   own Ren'Py runtime** (see ``native/README.md``).  The bundled runtime is
   launched headlessly, every compiled label is walked for ``show``/``scene``
   states (including ``show screen``), layered images and image declarations
   are resolved by the real displayables, and every state is rendered natively
   through the software renderer.  Results are adapted into the same
   ``CompositeRecord``/``CompositeManifest`` objects and written as per-category
   manifests under ``<output_dir>/reports/``.
2. **Apply** (`applier.py`): reads the manifest, rewrites ``show``/``scene``
   statements in live scripts to point at generated composite tags (or emits
   runtime ``ConditionSwitch`` wrappers when state is dynamic), declares every
   generated composite in ``game/composite_images.rpy``, and writes an adoption
   report.

All modes are config-driven via ``src/converter/config.json`` (produced by the
decompile subpackage).  No ``--game`` lookup is needed.

---

## Module responsibility division

| Module | Responsibility |
|--------|---------------|
| ``config.py`` | :class:`GameConfig` dataclass + :func:`load_config` JSON loader |
| ``models.py`` | Typed domain models (Layer, CompositeRecord, CompositeManifest, AdoptionReport, …) |
| ``errors.py`` | Custom exception hierarchy |
| ``regexp.py`` | Compiled regular expressions shared across the subpackage |
| ``parsing/`` | Ren'Py script parsing (blocks, layeredimage, screen, image declarations, single-pass indexer) |
| ``resolution/`` | Asset path resolution (aliases, archive lookup) |
| ``conditions/`` | Static condition evaluation, call-state reconstruction, interprocedural label states |
| ``compositing/`` | Image compositing (CPU via PIL, strategy pattern) |
| ``association/`` | Character inference for XXX (scene/adult) composites |
| ``compose_pass/`` | Compose pass orchestration (delegates extraction to the native stage) |
| ``native/`` | Native Ren'Py compose stage: runtime discovery, disposable workspace, headless batches, record adaptation (see ``native/README.md``) |
| ``records/`` | Composite record construction, enrichment, and deduplication |
| ``apply_pass/`` | Apply pass orchestration (script rewriting, declarations, report) |
| ``manifest/`` | Manifest read/write (per-category + index) |
| ``review/`` | Interactive review of scene character associations + unmapped speakers |
| ``review_instructions.py`` | Reusable manual fallback guidance for both review phases |
| ``cli.py`` | Command-line entry point |
| ``__main__.py`` | ``python -m converter.compose`` support |

The ``restore`` capability (revert modified ``.rpy`` files from ``archive.rpa``)
lives in the sibling ``converter/restore`` subpackage, not inside compose.

---

## Configuration

A :class:`GameConfig` describes one game.  It is normally **not** written by hand:
it is loaded from the decompile-generated ``src/converter/config.json`` via
:func:`converter.compose.config.load_config`:

```json
{
  "schema": 1,
  "game_folder": "src\\renpy_game\\CampMourningWood-0.25.0.1-pc\\game",
  "source_game": "CampMourningWood-0.25.0.1-pc",
  "source_path": "src\\renpy_game\\CampMourningWood-0.25.0.1-pc",
  "rpy_files": ["cabins.rpy", "camp.rpy", "script.rpy", "xxx.rpy", "..."],
  "ren_py_files": [],
  "extracted_archives": [
    { "archive": "archive.rpa", "extracted_folder": "archive" }
  ],
  "image_categories": {
    "classification_method": "folder_based",
    "image_categories": {
      "bg": "archive/bgs",
      "char": "archive/chars",
      "gui": "archive/gui",
      "scene": "archive/xxx"
    }
  },
  "compose_modes": ["cfg", "layered_all", "conditions"]
}
```

Paths in ``config.json`` are relative to the project root (the directory
containing ``src/``). ``game_folder`` is resolved against the project root,
and ``archive_dir`` is derived from the ``extracted_archives`` recorded by
decompile (falling back to ``game_folder / archive``). Output locations may be
stored in the JSON; otherwise they default to
``<game_root>/composites/...`` via :meth:`GameConfig.resolved_paths`.
Category folders and path-alias sources/targets must remain relative to their
intended asset roots; absolute, drive-qualified, or parent-traversing values
are rejected before compose starts.

### :class:`GameConfig` fields

- ``name`` — human-readable game identifier (from ``source_game`` in config.json).
- ``game_root`` — the Ren'Py ``\game`` folder (contains the ``.rpy`` scripts).
- ``archive_dir`` — primary extracted asset archive, derived from the
  decompile config's ``extracted_archives`` metadata.
- ``archive_dirs`` — every extracted asset archive; the first is the primary
  archive represented by ``archive_dir``.
- ``source_path`` — bundled Ren'Py runtime used by the native compose stage.
- ``categories`` — canonical category -> asset folder name (``bg``, ``char``,
  ``scene``, ``gui``).  Defaults: ``{"bg": "bgs", "char": "chars",
  "scene": "xxx", "gui": "gui"}``.
- ``non_character_folders`` — subfolders under the char dir that aren't characters.
- ``path_aliases`` — asset-path redirects applied when resolving sources.
- ``game_defaults`` — default Ren'Py variable values for static condition eval
  (e.g. ``{"month": 6, "day": 1}``).
- ``rpy_files`` / ``ren_py_files`` — scripts recorded by decompile for reference.
  Compose scans every live script in the game folder, so deleting a script from
  that folder is enough to drop it from the pass.
- ``compose_modes`` — optional subset of the native compose modes (``cfg``,
  ``layered_all``, ``conditions``) read from config.json.  Omitted (or empty)
  runs every mode; large games can set ``"compose_modes": ["cfg"]`` to keep a
  compose pass fast.
- ``output_dir`` — where composite PNGs are written (default:
  ``<game_root>/composites``).
- ``report_dir`` — where split manifests, the character catalog, and pending
  review data are written (default: ``<output_dir>/reports``).
- ``manifest_index_path`` — exact split-manifest index path (default:
  ``<report_dir>/manifest_index.json``).
- ``declaration_path`` / ``report_path`` — apply-pass output locations
  (default to ``<game_root>/composite_images.rpy`` and
  ``<report_dir>/adoption_report.json``).
- ``manifest_path`` — legacy compatibility setting; new configurations should
  use ``report_dir`` and ``manifest_index_path``.

Use :meth:`GameConfig.resolved_paths` to get all resolved absolute output paths.

---

## Usage

### Via CLI

`python -m converter.compose` is a single interactive entry point. It opens
an action menu first. Choosing **Compose new composites** or **Clean output,
then compose** runs compose immediately. Every script present in the game
folder is staged into the native Ren'Py child, so remove unwanted scripts from
that folder before composing.

| Choice | Effect |
|--------|--------|
| Clean previous composite output | Removes `<game_root>/composites` |
| Compose new composites (game scripts stay unchanged) | Renders composites and manifests; apply remains deferred |
| Clean output, then compose | Removes previous output before composing |
| Review character and speaker mappings | Runs the interactive scene/speaker review |
| Apply reviewed composites to game scripts | Adopts composites into live `.rpy` scripts |
| Restore original game files (keep composites) | Restores the live game folder from source while keeping generated composites |
| Exit | Leaves the compose action unstarted |

After composing, the CLI prints the pending scene/speaker review details and
offers to run the interactive review now or quit. **Unmapped Speakers** appears
above Phase 1, and **Scenes missing character associations** appears above
Phase 2. Each review phase begins with its corresponding manual fallback
instructions. Interactive selection menus keep seven rows visible at a time.
During speaker review, the related composite list and character mapping choices
remain visible together: the composite pane is read-only, the character pane
supports arrow-key selection and type-to-filter, and **Tab** switches focus
without losing either pane's scroll position.
The character mapping menu can keep the speaker name, accept a custom name,
remove its dialogs, or select a known character.

Scene review uses the same persistent context-and-character workspace. The
composite path is shown as separate metadata, while **Defined at** heads the
numbered list of the definition and usage locations. **Space** toggles
character checkboxes and **Enter** confirms the highlighted character plus any
characters already toggled. Each scene offers three explicit outcomes:

- **Keep without character association** removes only its `review_pending.json`
  entry; the scene remains in `manifest_scene.json`.
- **Remove from downstream processing** removes its `manifest_scene.json` entry
  and moves the generated PNG into the rejected-output folder.
- **Select character(s)** appends each selected character to the matching
  scene's `characters` list in `manifest_scene.json`, preserves existing
  associations, and sets `character_association_status` to `associated`. The
  custom-character option resolves a known alias to its canonical character;
  an unknown name is appended as an unverified association. The scene entry is
  then removed from `review_pending.json`.

```bash
# Interactive menu
python -m converter.compose

# Skip the menu with an explicit action
python -m converter.compose clean            # remove previous compose output
python -m converter.compose compose          # compose only (no apply)
python -m converter.compose clean-compose    # clean output, then compose
python -m converter.compose review           # review scene character associations + unmapped speakers
python -m converter.compose apply            # adopt composites into live .rpy scripts
python -m converter.compose restore          # recover the live game folder from source, keep composites

# Options
--config PATH   # defaults to src/converter/config.json
--dry-run       # compose without writing PNGs/manifests (skips review + apply)
```

Non-interactive terminals (scripts, CI) skip prompts. The explicit `compose`
and `clean-compose` actions run the compose pass directly; no post-compose
review prompt is shown.

The full pipeline is also available through :mod:`converter.pipeline` (which
adds the decompile stage):

```bash
uv run python -m converter.pipeline                  # stage -> decompile -> compose (apply deferred)
uv run python -m converter.pipeline --compose-only   # compose only (skip decompile)
uv run python -m converter.pipeline --apply-only     # apply only (needs manifest)
uv run python -m converter.pipeline --dry-run        # plan only; no staging or writes
```

Restore-compose is a compose CLI action (single point of entry):

```bash
uv run python -m converter.compose restore           # restore game folder from source, keep composites
uv run python -m converter.compose restore --dry-run # preview without writing
```

Or via mise tasks:

```bash
mise run test             # automated test suite
mise run decompile        # decompile + extract, write config.json
mise run compose          # interactive compose
python -m converter.compose restore  # restore game folder from source, keep composites
```

### Via Python import

```python
from converter.compose.config import GameConfig, load_config
from converter.compose.compose_pass import Composer
from converter.compose.apply_pass import Applier

# 1. Load the decompile-generated config
config = load_config("src/converter/config.json")

# 2. Compose → manifest + PNGs (the compose pass publishes the manifest)
composer = Composer(config)
manifest = composer.compose(dry_run=False, clean_output=False)

# 3. Apply → adopt composites into live scripts
applier = Applier(config)
manifest, report = applier.apply()
```

### Via Python import (restore)

```python
from converter.restore import restore_compose_from_source

# Restore the live game folder from the original source, keeping composites.
results = restore_compose_from_source("src/converter/config.json")

# Dry run: preview what would be done without writing.
results = restore_compose_from_source("src/converter/config.json", dry_run=True)
```

---

## Files

```
compose/
├── __init__.py      # Package exports (public API facade)
├── __main__.py      # python -m converter.compose support
├── cli.py           # Command-line entry point
├── config.py        # GameConfig dataclass + load_config JSON loader
├── models.py        # Typed domain models
├── errors.py        # Custom exception hierarchy
├── regexp.py        # Compiled regular expressions
├── parsing/         # Ren'Py script parsing (blocks, layeredimage, screen, image declarations, indexer)
│   ├── __init__.py
│   ├── blocks.py    # ScriptBlock, parse_layered, parse_screen_stack
│   ├── declarations.py  # parse_image_declarations, ImageDeclaration
│   ├── dialog.py    # find_nearby_dialog, DialogueLine
│   ├── files.py     # files() script discovery
│   └── indexer.py   # ScriptIndex, index_scripts (single-pass)
├── resolution/      # Asset path resolution
│   ├── __init__.py
│   └── resolver.py  # Resolver
├── conditions/      # Static condition evaluation + call-state
│   ├── __init__.py
│   ├── eval.py      # eval_cond, condition_variables, layer condition selection
│   └── callstate.py # CallState, state_at_call, interprocedural label states
├── compositing/     # Image compositing (CPU via PIL)
│   ├── __init__.py
│   ├── strategy.py  # CompositorStrategy protocol
│   └── cpu.py       # CPUCompositor, Compositor
├── native/          # Native Ren'Py compose stage (see native/README.md)
│   ├── constants.py # Protocol + safety limits
│   ├── discovery.py # Bundled runtime discovery
│   ├── workspace.py # Disposable game workspace
│   ├── process.py   # Headless subprocess batches
│   ├── protocol.py  # Native result validation
│   ├── stage.py     # run_composite_stage + record adaptation
│   └── runtime/     # Injected composite_export.rpy
├── association/     # Character inference for XXX composites
│   ├── __init__.py
│   └── associator.py  # CharacterAssociator
├── records/         # Composite record construction + deduplication
│   ├── __init__.py
│   ├── record.py    # _emit_record, enrichment, normalization
│   └── emitter.py   # _CompositeEmitter
├── compose_pass/    # Compose pass orchestration (delegates to native stage)
│   ├── __init__.py  # Package exports (Composer)
│   └── orchestrate.py   # Composer class — delegates to native stage
├── apply_pass/      # Apply pass orchestration
│   ├── __init__.py
│   └── applier.py   # Applier class
├── manifest/        # Manifest read/write (per-category + index)
│   ├── __init__.py
│   └── writer.py    # ManifestWriter
├── review/          # Interactive review
│   ├── __init__.py
│   ├── cli.py       # chars-list, compose-review commands
│   └── interactive.py  # interactive review flow
└── README.md        # This file
```

Restore lives in the sibling package:

```
restore/
├── __init__.py      # Package exports (public API)
├── errors.py        # RestoreError
└── restore.py       # restore_rpy_files, restore_from_manifest, restore_decompile, find_archive
```

---

## Manifest layout

The compose pass does **not** write a single monolithic ``manifest.json``.
Under ``<output_dir>/reports/``:

- ``manifest_bg.json``   — composites in the ``bg`` family
- ``manifest_char.json`` — composites in the ``char`` family
- ``manifest_scene.json`` — composites in the ``scene`` family
- ``manifest_gui.json``  — composites in the ``gui`` family
- ``manifest_other.json`` — composites that don't fit a known family
- ``characters.json`` — canonical character names, generated folders, and speaker aliases
- ``manifest_index.json`` — global data (summary, dynamic_states, image_declarations,
  dynamic_declarations, unresolved, CFG walk state) plus relative references to
  each category file.

The complete report bundle is serialized first, staged in the destination
filesystem, and published through atomic replacements.  The index is replaced
last as the commit marker.  Category references are relative to the index
directory, and the apply, review, and restore consumers resolve those
references instead of reconstructing absolute filenames. If a legacy or
partially edited index omits a category reference, every consumer uses the
canonical filename in the configured `report_dir`.

Each native composite record retains its rendered ``content_hash`` and native
``condition_state``.  Both participate in record identity, so two condition
states cannot collapse merely because their layer semantics match.
Condition-evaluation failures and general native render failures are preserved
in ``unresolved``; a truncated CFG walk is preserved through ``cfg_processed``
and ``cfg_truncated``.

### Composite output layout

Generated PNGs keep a navigable folder structure under
``<output_dir>/<status>/<family>/``. The folder chain mirrors the game's own
asset tree, with two rules that keep the tree browsable:

- **Character composites gather in one folder per character.** Sprite asset
  folders nest by body part (``chars/emma/ArmL/``, ``chars/hazel/face/``), so
  grouping by those would scatter a character's composites. Every composite of
  a character lands in ``static/char/<character>/`` regardless of which sprite
  subfolders its layers came from, and shared prop layers
  (``chars/clothes/...``) never hijack the folder.
- **Other categories mirror the asset tree** below their category folder, as
  deep as a majority of the composite's layers share it:
  ``bgs/home/alchemy/`` -> ``static/bg/home/alchemy/``.

Filenames stay ``<tag>__<digest>.png`` so the apply pass can derive a unique
script tag per composite.

```
composites/
├── reports/            # split manifests + characters.json + review_pending.json
└── static/
    ├── bg/             # backgrounds (mirrors bgs/)
    │   ├── bgCandyMountain__9f2c….png
    │   └── home/bgHome__5a78….png
    ├── char/           # one folder per character
    │   ├── amber/am__6b7c….png
    │   └── player/you__f1d4….png
    ├── scene/          # scenes/CGs (mirrors xxx/)
    │   └── hazel/bed/sex/haXS1__8013….png
    └── gui/            # interface elements (mirrors gui/)
```

### Character catalog

``characters.json`` is seeded during a non-dry-run compose pass. Its entries
come from Ren'Py ``Character(...)`` declarations in the configured scripts plus
characters that only the native stage could discover in the runtime's
``ADVCharacter`` registry; statically-parsed declarations always win. The
existing canonicalization and alias rules are retained, and manually added
aliases are preserved for characters that still have a matching declaration.
Each ``folder`` value points to the generated static character-composite
directory relative to the game root, for example
``composites/static/char/amber``. When a declaration's image tag identifies an
emitted composite, the catalog uses that composite's generated folder; otherwise
it uses the canonical generated character path.

---
## Generalization notes

Compared with the original game-specific scripts, all of the following are now
driven by :class:`GameConfig` instead of hard-coded constants:

- Asset folder names per category (``chars``/``xxx``/``bgs``/``gui``).
- The path regex that recognizes asset paths.
- The character asset directory and non-character subfolders.
- Per-game path aliases (renamed sprite sheets).
- The ``characters.rpy`` location for ``*ResetDefault`` state tracking.
- Output, manifest, declaration, and report paths.
- Game-specific defaults (e.g. ``month``, ``day``).
- Speaker-to-folder mapping (``player_folder`` / ``player_aliases``) — no
  hard-coded player defaults; driven entirely by ``config.json`` or
  ``Character()`` declarations.
- The set of live scripts in the game folder (compose scans that folder directly).

Supporting multiple folders per category (rather than one-to-one) is a natural
extension: ``categories`` can map a category to a list of folders, and
``path_regex()`` / ``category_for_folder()`` would be updated accordingly.

---

## Design principles

- **No module-level mutable global state**: configuration is passed explicitly
  through constructors rather than bound via module-level setters.  This makes
  the code thread-safe and trivial to unit-test.
- **Typed domain models**: raw dicts are replaced with frozen dataclasses
  (:class:`Layer`, :class:`CompositeRecord`, :class:`CompositeManifest`, …)
  with explicit ``replace()`` for immutable updates.
- **Single responsibility**: each module owns exactly one concern (parsing,
  resolution, conditions, compositing, character association, orchestration,
  manifest I/O).
- **Strategy pattern**: compositing uses CPU (PIL) through a common
  :class:`CompositorStrategy` protocol.
- **Idempotent apply**: the apply pass refuses to touch a line whose text no
  longer matches the recorded statement, so a second run is a no-op.
- **Progress tracking**: the compose pass polls the child renderer's progress
  log while batches run, so a real-time progress bar advances as states are
  rendered instead of waiting for each batch to finish.
- **Intermediate cleanup**: the per-batch result JSON (`.native-result.json`)
  is removed from ``reports/`` after the native stage completes, so only the
  final per-category manifests and index remain.

---

## 🔧 Troubleshooting

| Issue | Solution |
|-------|----------|
| `config.json not found` | Run decompile first: `mise run decompile` or `python src/converter/decompile/decompile.py` |
| `No archive.rpa found` | Ensure the game's `.rpa` archives have been extracted by decompile |
| `No game folders found` | Place games under `src\renpy_game\<game>\<game>-pc\game\` |
| `ModuleNotFoundError: rpatool` | Run `uv add git+https://github.com/regiellis/rpa-toolkit.git@435f202e7cb9aeeaf6c006cf928d9661f8830e6e` |

---

*Generated for the renpy-photoreal-vn-converter project.*

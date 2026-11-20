"""Native Ren'Py compose stage: extraction, rendering, and record adaptation.

Responsibility: run the injected ``_composite_export`` command inside the
game's bundled Ren'Py runtime, page through its rendered batches, and adapt the
native protocol results into the existing
:class:`~converter.compose.models.CompositeRecord` /
:class:`~converter.compose.models.CompositeManifest` objects so the review,
apply, and manifest contracts stay unchanged.

Screen-stack composites (Pass 3 of the static pipeline) are still produced by
:mod:`converter.compose.compose_pass.passes` and merged into the same manifest,
so ``show screen`` adoption keeps working.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from cli import counted, info, loading, progress_bar

from ..association import CharacterAssociator, CharacterDeclaration
from ..compositing import Compositor
from ..config import DEFAULT_CATEGORY_FOLDERS, GameConfig
from ..errors import ConfigError, NativeProtocolError, NativeStageError
from ..models import CompositeManifest, CompositeRecord, Layer
from ..parsing import ScriptBlock, ScriptIndex, index_scripts
from ..records import (
    _build_base_record,
    _determine_family_and_path,
    _enrich_char_record,
    _enrich_scene_record,
    _loc_to_usage,
    _normalize_layers,
    _record_key,
)
from .constants import (
    DEFAULT_PROCESS_TIMEOUT_SECONDS,
    MAX_CFG_CALL_DEPTH,
    MAX_CFG_PROCESSED_NODES,
    MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE,
    MAX_RENDER_STATES_PER_BATCH,
    MAX_SYNTHETIC_ATTRIBUTE_STATES,
    MIN_RENDER_STATES_PER_BATCH,
    VALID_MODES,
)
from .discovery import discover_runtime
from .process import NativeExecutionError, run_native_batch
from .protocol import read_result
from .workspace import NativeWorkspace, create_workspace

# Dynamic declaration expressions are reported, never composited (matching the
# static Pass 2 behaviour).
_DYNAMIC_TOKENS = (
    "ConditionSwitch",
    "LiveComposite",
    "Function(",
    "DynamicDisplayable",
)

_DECLARATION_ROLE = "image_declaration_layer"

# ``Composite((w, h), ...)`` canvas size inside an image declaration.
_COMPOSITE_RE = re.compile(r"Composite\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)")


@dataclass(frozen=True)
class _DeclBlock:
    """Minimal :class:`ScriptBlock` stand-in for image declarations."""

    path: Path
    line: int


@dataclass(frozen=True)
class NativeStageResult:
    """Summary of one native compose stage run."""

    manifest_path: Path
    manifest: CompositeManifest
    batches: int
    states: int
    rendered: int
    failed_states: int
    empty_states: int
    layered_images: int
    characters: int


class _RecordSink:
    """Deduplicating record sink shared by native and screen-pass records."""

    def __init__(
        self,
        config: GameConfig,
        index: ScriptIndex,
        associator: CharacterAssociator,
        compositor: Compositor,
        *,
        dry_run: bool = False,
        render_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.index = index
        self.associator = associator
        self.compositor = compositor
        self.dry_run = dry_run
        self.render_dir = render_dir
        self.records: list[CompositeRecord] = []
        self.records_by_key: dict[tuple[str, str, str], CompositeRecord] = {}
        self.key_to_index: dict[tuple[str, str, str], int] = {}
        self.usages_processed = 0

    # ------------------------------------------------------------------ #
    # Static screen-pass compatibility (Pass 3 renders on the CPU)
    # ------------------------------------------------------------------ #

    def __call__(
        self,
        name: str,
        kind: str,
        layers: list[Layer],
        locations: list[dict],
        block: ScriptBlock,
    ) -> None:
        """Emit one statically-resolved composite (screen stacks)."""
        self.usages_processed += 1
        normalized = _normalize_layers(layers, self.config)
        if not normalized:
            return
        key = _record_key(kind, name, normalized)
        if key in self.records_by_key:
            self._merge(key, locations)
            return
        record = self._render_and_build(name, kind, normalized, locations, block, key[2])
        if record is None:
            return
        self._store(key, record)

    def _render_and_build(
        self,
        name: str,
        kind: str,
        normalized: list[Layer],
        locations: list[dict],
        block: ScriptBlock,
        digest: str,
    ) -> CompositeRecord | None:
        """Render a composite with the CPU compositor and build its record."""
        target, family, _sub, status, family2 = _determine_family_and_path(
            normalized, kind, name, self.config, digest
        )
        dimensions: tuple[int, int] | None = None
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                dimensions = _png_size(target)
            else:
                image = self.compositor.composite(normalized, canvas_size=None)
                if image is None:
                    return None
                dimensions = (int(image.size[0]), int(image.size[1]))
                image.save(target)
        record = self._build(
            name,
            kind,
            normalized,
            locations,
            block,
            target,
            family,
            status,
            dimensions,
            family2,
        )
        return record

    # ------------------------------------------------------------------ #
    # Native candidates
    # ------------------------------------------------------------------ #

    def add_native_candidate(
        self,
        candidate: dict,
        layered_meta: dict[str, dict],
        image_meta: dict[str, dict] | None = None,
    ) -> None:
        """Adapt one native render candidate into zero or one record."""
        self.usages_processed += 1
        kind = str(candidate["kind"])
        name = str(candidate["name"])

        normalized = _normalize_layers(
            self._candidate_layers(candidate, layered_meta), self.config
        )
        if not normalized:
            return

        key = _record_key(kind, name, normalized)
        locations = self._locations(candidate)
        if key in self.records_by_key:
            self._merge(key, locations)
            return

        digest = key[2]
        target, family, _sub, status, family2 = _determine_family_and_path(
            normalized, kind, name, self.config, digest
        )

        dimensions: tuple[int, int] | None = None
        if not self.dry_run:
            if not _copy_render(candidate, target, self.render_dir):
                return
            dimensions = (int(candidate["width"]), int(candidate["height"]))

        record = self._build(
            name,
            kind,
            normalized,
            locations,
            self._definition_block(candidate, layered_meta, image_meta or {}),
            target,
            family,
            status,
            dimensions,
            family2,
        )
        self._store(key, record)

    def _candidate_layers(
        self, candidate: dict, layered_meta: dict[str, dict]
    ) -> list[Layer]:
        """Map native layer provenance onto static :class:`Layer` records.

        Static layers win whenever the source path matches a parsed
        ``layeredimage``/declaration layer: they carry the declaration line
        numbers, offsets, and condition text that the apply pass relies on.
        Native provenance only fills the gaps.
        """
        name = str(candidate["name"])
        sources_by_candidate = {
            str(source).lower() for source in candidate.get("sources") or []
        }

        if candidate["kind"] == "layeredimage":
            _block, static_layers = self.index.layeredimage_defs.get(name, (None, []))
            by_source: dict[str, Layer] = {}
            for layer in static_layers:
                by_source.setdefault(layer.source.lower(), layer)
            fallback_line = int(
                ((layered_meta.get(name) or {}).get("definition") or {}).get("line")
                or 0
            )

            layers: list[Layer] = []
            seen: set[tuple[str, int]] = set()
            for entry in candidate.get("layers") or []:
                unresolved = tuple(entry.get("unresolved") or ())
                for source in entry.get("sources") or []:
                    static = by_source.get(str(source).lower())
                    if static is not None:
                        marker = (str(source).lower(), static.line)
                        if marker in seen:
                            continue
                        seen.add(marker)
                        layers.append(
                            static.replace(
                                unresolved_conditions=unresolved,
                                attribute=static.attribute or entry.get("attribute"),
                                group=static.group or (entry.get("group") or ""),
                            )
                        )
                        continue
                    marker = (str(source).lower(), fallback_line)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    layers.append(
                        Layer(
                            source=str(source),
                            line=fallback_line,
                            group=entry.get("group") or "",
                            conditions=tuple(entry.get("conditions") or ()),
                            attribute=entry.get("attribute"),
                            unresolved_conditions=unresolved,
                        )
                    )
            return layers

        # Plain image declaration: keep the declared sources the render used.
        declaration = self.index.image_decls.get(name)
        if declaration is None:
            return [
                Layer(source=str(source), line=0, role=_DECLARATION_ROLE)
                for source in candidate.get("sources") or []
            ]
        layers = []
        for source in declaration.sources:
            if sources_by_candidate and source.lower() not in sources_by_candidate:
                continue
            layers.append(
                Layer(
                    source=source,
                    line=declaration.line,
                    role=_DECLARATION_ROLE,
                    declaration_expression=declaration.expression,
                    canvas_size=_declaration_canvas(declaration.expression),
                )
            )
        return layers

    def _locations(self, candidate: dict) -> list[dict]:
        """Native usage records -> manifest usage locations."""
        assignments = candidate.get("condition_assignments") or {}
        snapshot = {key: assignments[key] for key in sorted(assignments)}
        locations = []
        for usage in candidate.get("usages") or []:
            location: dict = {
                "script": usage["script"],
                "line": int(usage["line"]),
                "statement": usage["statement"],
            }
            if snapshot:
                location["state_snapshot"] = dict(snapshot)
            locations.append(location)
        return locations

    def _definition_block(
        self,
        candidate: dict,
        layered_meta: dict[str, dict],
        image_meta: dict[str, dict],
    ) -> ScriptBlock | _DeclBlock:
        """Best-known definition block for one candidate.

        Statically-parsed blocks win (they carry real source lines); the
        native definitions are the fallback for images only the runtime can
        see, such as ``image <name>:`` ATL animations.
        """
        name = str(candidate["name"])
        if candidate["kind"] == "layeredimage":
            block = self.index.layeredimage_defs.get(name, (None, []))[0]
            if block is not None:
                return block
        else:
            declaration = self.index.image_decls.get(name)
            if declaration is not None:
                return _DeclBlock(declaration.script, declaration.line)
        definition = (
            (layered_meta.get(name) or {}).get("definition")
            or image_meta.get(name)
            or {}
        )
        script = str(definition.get("script") or "game/unknown.rpy")

        return _DeclBlock(
            self.config.game_root.parent / script, int(definition.get("line") or 0)
        )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _build(
        self,
        name: str,
        kind: str,
        normalized: list[Layer],
        locations: list[dict],
        block: ScriptBlock | _DeclBlock,
        target: Path,
        family: str,
        status: str,
        dimensions: tuple[int, int] | None,
        family2: str,
    ) -> CompositeRecord:
        record = _build_base_record(
            name,
            kind,
            normalized,
            locations,
            block,  # type: ignore[arg-type]
            self.config,
            target,
            family,
            status,
            dimensions,
        )
        if family2 == "char":
            return _enrich_char_record(record, normalized, self.associator)
        if family2 == "scene":
            return _enrich_scene_record(
                record,
                normalized,
                record.usage_locations,
                block,  # type: ignore[arg-type]
                self.index,
                self.associator,
                self.config,
            )
        return record

    def _store(self, key: tuple[str, str, str], record: CompositeRecord) -> None:
        self.records_by_key[key] = record
        self.key_to_index[key] = len(self.records)
        self.records.append(record)

    def _merge(self, key: tuple[str, str, str], locations: list[dict]) -> None:
        """Merge usage locations for a duplicate composite key."""
        existing = self.records_by_key[key]
        existing_keys = {(loc.script, loc.line) for loc in existing.usage_locations}
        merged = list(existing.usage_locations)
        for location in locations:
            marker = (location["script"], location["line"])
            if marker in existing_keys:
                continue
            merged.append(_loc_to_usage(location))
            existing_keys.add(marker)
        existing = existing.replace(
            usage_locations=tuple(sorted(merged, key=lambda loc: (loc.script, loc.line)))
        )
        self.records_by_key[key] = existing
        index = self.key_to_index.get(key)
        if index is not None:
            self.records[index] = existing


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read one PNG's dimensions from its IHDR without decoding it."""
    try:
        with open(path, "rb") as handle:
            handle.seek(16)
            header = handle.read(8)
    except OSError:
        return None
    if len(header) != 8:
        return None
    return (
        int.from_bytes(header[0:4], "big"),
        int.from_bytes(header[4:8], "big"),
    )


def _copy_render(candidate: dict, target: Path, render_dir: Path | None) -> bool:
    """Copy one rendered PNG out of the disposable workspace."""
    render_file = candidate.get("render_file")
    if not render_file or render_dir is None:
        return False
    source = render_dir / str(render_file)
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _declaration_canvas(expression: str) -> tuple[int, int] | None:
    """Extract a ``Composite((w, h), ...)`` canvas size, if any."""
    match = _COMPOSITE_RE.search(expression)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _native_characters(characters: dict) -> dict[str, CharacterDeclaration]:
    """Convert native ``ADVCharacter`` discoveries into declarations."""
    extra: dict[str, CharacterDeclaration] = {}
    for store_name, entry in sorted((characters or {}).items()):
        if not isinstance(entry, dict):
            continue
        display = str(entry.get("display_name") or store_name)
        image_tag = entry.get("image_tag")
        aliases = {str(store_name)}
        if isinstance(image_tag, str) and image_tag:
            aliases.add(image_tag)
        previous = extra.get(display)
        merged_aliases = frozenset(aliases) | (previous.aliases if previous else set())
        merged_tags = frozenset(
            [image_tag] if isinstance(image_tag, str) and image_tag else []
        ) | (previous.image_tags if previous else set())
        extra[display] = CharacterDeclaration(
            name=display, aliases=merged_aliases, image_tags=merged_tags
        )
    return extra


def _validate_modes(modes: list[str] | None) -> list[str]:
    """Normalise and validate the requested compose modes."""
    requested = [str(mode) for mode in (modes or []) if mode]
    if not requested:
        return list(VALID_MODES)
    unknown = sorted(set(requested) - set(VALID_MODES))
    if unknown:
        raise NativeStageError(
            f"Unknown native compose mode(s): {', '.join(unknown)}. "
            f"Valid modes: {', '.join(VALID_MODES)}."
        )
    return requested


def _resolve_config(
    vn_root: Path, output_dir: Path, config: GameConfig | None
) -> GameConfig:
    """Return the game config to use for the native stage."""
    if config is not None:
        return config

    root = Path(vn_root).resolve()
    game_root = root / "game" if (root / "game").is_dir() else root
    out = Path(output_dir)
    return GameConfig(
        name=root.name,
        game_root=game_root,
        archive_dir=game_root / "archive",
        source_path=root,
        categories=dict(DEFAULT_CATEGORY_FOLDERS),
        output_dir=out if out.is_absolute() else None,
    )


def _engine_config(
    *,
    game_root: Path,
    render_dir: Path,
    modes: list[str],
    defaults: dict,
    batch_index: int,
    state_offset: int,
    progress_path: Path,
    max_render_states: int = MAX_RENDER_STATES_PER_BATCH,
) -> str:
    """Serialise one native batch's engine configuration."""
    return json.dumps(
        {
            "game_root": str(game_root),
            "render_dir": str(render_dir),
            "modes": list(modes),
            "defaults": dict(defaults),
            "max_synthetic_states": MAX_SYNTHETIC_ATTRIBUTE_STATES,
            "max_condition_assignments": MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE,
            "max_render_states": max_render_states,
            "max_cfg_states": MAX_CFG_PROCESSED_NODES,
            "max_cfg_depth": MAX_CFG_CALL_DEPTH,
            "batch_index": batch_index,
            "state_offset": state_offset,
            "progress_path": str(progress_path),
        },
        indent=2,
        sort_keys=True,
    )


def _script_files(config: GameConfig) -> list[str] | None:
    """Scripts to stage into the disposable workspace (relative to game root)."""
    scripts = list(config.rpy_files or []) + list(config.ren_py_files or [])
    return scripts or None


def _previous_dynamic_states(config: GameConfig) -> list[dict]:
    """Carry the previous manifest's dynamic states forward."""
    manifest_path = config.resolved_paths()["manifest_path"]
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    states = data.get("dynamic_states", [])
    return states if isinstance(states, list) else []


def _is_dynamic_expression(expression: str) -> bool:
    return any(token in expression for token in _DYNAMIC_TOKENS)


def _declaration_records(index: ScriptIndex, config: GameConfig) -> list[dict]:
    """Static ``image`` declaration records, matching the static pipeline."""
    return [
        {
            "name": name,
            "script": index.image_decls[name].script.relative_to(
                config.game_root.parent
            ).as_posix(),
            "line": index.image_decls[name].line,
            "expression": index.image_decls[name].expression,
            "source_candidates": list(index.image_decls[name].sources),
            "status": (
                "dynamic_review"
                if _is_dynamic_expression(index.image_decls[name].expression)
                else "declaration_candidate"
            ),
        }
        for name in sorted(index.image_decls)
    ]


def _dynamic_declarations(index: ScriptIndex, config: GameConfig) -> list[dict]:
    """Dynamic ``image`` declaration reports, matching the static pipeline."""
    records = []
    for name in sorted(index.image_decls):
        declaration = index.image_decls[name]
        if not _is_dynamic_expression(declaration.expression):
            continue
        records.append(
            {
                "name": name,
                "declaration": {
                    "script": declaration.script.relative_to(
                        config.game_root.parent
                    ).as_posix(),
                    "line": declaration.line,
                },
                "usage_locations": [],
                "expression": declaration.expression,
                "source_candidates": list(declaration.sources),
                "status": "dynamic_review",
                "reason": "runtime-dependent image declaration",
            }
        )
    return records


def _add_screen_composites(
    sink: _RecordSink, index: ScriptIndex, config: GameConfig, defaults: dict
) -> None:
    """Emit screen-stack composites with the static Pass 3 logic."""
    from ..compose_pass.passes import _process_screen_stacks_from_index
    from ..conditions import compute_interprocedural_label_states

    file_lines = {
        file_index.path: file_index.lines for file_index in index.per_file.values()
    }
    label_entry_states = compute_interprocedural_label_states(defaults, file_lines)
    _process_screen_stacks_from_index(
        index,
        index.screens,
        index.layeredimage_defs,
        defaults,
        sink,
        config,
        label_entry_states,
    )


def _run_batches(
    workspace: NativeWorkspace,
    modes: list[str],
    defaults: dict,
    layout,
    timeout: int,
) -> tuple[list[dict], dict]:
    """Page through native batches, returning merged candidates + last result.

    A child process that dies mid-batch (the software renderer can crash under
    memory pressure) is retried from the same offset with a halved sub-batch,
    so pagination always makes forward progress.

    Rendering progress is streamed from the child's progress log into a
    host-side progress bar so the UI advances in real time instead of waiting
    for each batch to finish.
    """
    candidates: list[dict] = []
    batch_index = 0
    state_offset = 0
    batch_size = MAX_RENDER_STATES_PER_BATCH
    last: dict = {}
    attempt = 0

    while True:
        attempt += 1
        render_dir = workspace.root / "renders"
        workspace.config_path.write_text(
            _engine_config(
                game_root=workspace.game_dir,
                render_dir=render_dir,
                modes=modes,
                defaults=defaults,
                batch_index=batch_index,
                state_offset=state_offset,
                progress_path=workspace.root / "progress.log",
                max_render_states=batch_size,
            ),
            encoding="utf-8",
        )

        # Truncate the progress log so this batch only reports its own
        # progress (the child appends to it, so old lines would otherwise be
        # re-read on every batch).
        progress_log = workspace.root / "progress.log"
        try:
            progress_log.write_text("", encoding="utf-8")
        except OSError:
            pass

        batch_rendered = [0]
        total_known = [False]

        try:
            with progress_bar(
                total=None,
                desc=f"Native batch {batch_index} rendering",
                unit="states",
            ) as bar:
                batch_rendered[0] = 0

                def _cb(done: int, _br: list[int] = batch_rendered) -> None:
                    delta = done - _br[0]
                    _br[0] = done
                    bar.update(delta)

                def _total_cb(
                    total: int,
                    _tk: list[bool] = total_known,
                ) -> None:
                    if not _tk[0]:
                        _tk[0] = True
                        bar.total = total
                        # Directly update the underlying rich progress task so
                        # the percentage picks up the new total immediately.
                        bar._prog.update(bar._task_id, total=total, completed=bar.n)

                run_native_batch(
                    layout=layout,
                    basedir=workspace.root,
                    savedir=workspace.savedir,
                    config_path=workspace.config_path,
                    result_path=workspace.result_path,
                    command="composite_export",
                    timeout=timeout,
                    progress_path=progress_log,
                    progress_cb=_cb,
                    total_cb=_total_cb,
                )
        except NativeExecutionError:
            if batch_size <= MIN_RENDER_STATES_PER_BATCH:
                raise
            batch_size = max(MIN_RENDER_STATES_PER_BATCH, batch_size // 2)
            info(
                f"Native batch {batch_index} failed (attempt {attempt}); "
                f"retrying with {batch_size} states per batch."
            )
            continue

        data = read_result(workspace.result_path)
        if data["status"] == "error":
            raise NativeProtocolError(
                "Native Ren'Py compose stage failed: "
                + str(data["error"].get("message", "")).strip()
            )
        last = data
        candidates.extend(data["candidates"])
        batch = data["batch"]
        info(
            "Native batch {}: rendered {} of {} states.".format(
                batch_index, batch.get("rendered", 0), batch.get("total_states", 0)
            )
        )
        next_offset = batch.get("next_offset")
        if next_offset is None:
            return candidates, last
        state_offset = int(next_offset)
        batch_index += 1
        attempt = 0


def _run_native_stage(
    vn_root: Path,
    output_dir: Path,
    modes: list[str],
    *,
    config: GameConfig | None = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
) -> NativeStageResult:
    """Run the complete native compose stage and write the split manifests."""
    modes = _validate_modes(modes)
    game_config = _resolve_config(vn_root, output_dir, config)
    if game_config.source_path is None:
        raise ConfigError(
            "Native compose requires a bundled Ren'Py runtime: set 'source_path' "
            "in the decompile config.json."
        )

    with loading("Indexing game scripts for native compose..."):
        index = index_scripts(game_config)

    layout = discover_runtime(game_config.source_path)
    paths = game_config.resolved_paths()
    reports_dir = paths["manifest_path"].parent
    # The child writes its result JSON into reports_dir; create it up front so
    # the first batch doesn't fail with FileNotFoundError.
    reports_dir.mkdir(parents=True, exist_ok=True)

    compositor = Compositor(game_config)
    associator = CharacterAssociator(game_config)

    workspace: NativeWorkspace | None = None
    try:
        workspace = create_workspace(
            game_root=game_config.game_root,
            script_files=_script_files(game_config),
            engine_config={},
            result_path=reports_dir / ".native-result.json",
            workspace_parent=game_config.game_root.parent,
        )
        candidates, last = _run_batches(
            workspace, modes, dict(index.all_defaults), layout, timeout
        )
        layered_meta = {
            str(entry["name"]): entry for entry in last.get("layered_images", [])
        }
        image_meta = {
            str(name): definition
            for name, definition in (last.get("image_definitions") or {}).items()
        }

        sink = _RecordSink(
            game_config,
            index,
            associator,
            compositor,
            dry_run=dry_run,
            render_dir=workspace.root / "renders",
        )

        with progress_bar(
            total=len(candidates),
            desc="Native states processed",
            unit="states",
        ) as bar:
            for candidate in candidates:
                # Empty renders (nothing visible for that state) and failed
                # renders produce no PNG, so they emit no record.
                if candidate.get("error") or not candidate.get("content_hash"):
                    bar.update(1)
                    continue
                sink.add_native_candidate(candidate, layered_meta, image_meta)
                bar.update(1)

        _add_screen_composites(sink, index, game_config, dict(index.all_defaults))

        failed = sum(1 for candidate in candidates if candidate.get("error"))
        empty = sum(
            1
            for candidate in candidates
            if not candidate.get("error") and not candidate.get("content_hash")
        )
        dynamic_declarations = _dynamic_declarations(index, game_config)
        manifest = CompositeManifest(
            composites=tuple(sink.records),
            dynamic_states=_previous_dynamic_states(game_config),
            image_declarations=_declaration_records(index, game_config),
            dynamic_declarations=dynamic_declarations,
            unresolved=(),
            summary={
                "scripts_scanned": len(index.files),
                "composites": len(sink.records),
                "uses_processed": sink.usages_processed,
                "native_states": len(candidates),
                "native_failed_states": failed,
                "native_empty_states": empty,
                "native_batches": int(last.get("batch", {}).get("index", 0)) + 1,
                "native_layered_images": len(layered_meta),
                "image_declarations_indexed": len(index.image_decls),
                "dynamic_declarations": len(dynamic_declarations),
            },
        )

        from ..manifest import ManifestWriter

        ManifestWriter(game_config).write(
            manifest,
            dry_run=dry_run,
            extra_characters=_native_characters(last.get("characters") or {}),
        )

        batch = last.get("batch", {})
        return NativeStageResult(
            manifest_path=reports_dir / "manifest_index.json",
            manifest=manifest,
            batches=int(batch.get("index", 0)) + 1,
            states=len(candidates),
            rendered=len(candidates) - failed - empty,
            failed_states=failed,
            empty_states=empty,
            layered_images=len(layered_meta),
            characters=len(last.get("characters") or {}),
        )
    finally:
        if workspace is not None:
            workspace.cleanup()
        # Remove the intermediate per-batch result JSON; it is not part of
        # the final manifest output.
        result_path = reports_dir / ".native-result.json"
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_composite_stage(
    vn_root: Path,
    output_dir: Path,
    modes: list[str],
    *,
    config: GameConfig | None = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
) -> Path:
    """Run the native Ren'Py compose stage and return its manifest index path.

    Args:
        vn_root: Ren'Py project/runtime root containing ``renpy/``, ``lib/``,
            and the game itself (``game/``).
        output_dir: Directory that receives the composite PNGs and the
            ``reports/`` split manifests.
        modes: Subset of ``{"cfg", "layered_all", "conditions"}``.  Results are
            merged deterministically; an empty list selects every mode.
        config: Optional :class:`GameConfig` supplying asset categories and
            resolved output paths.  Derived from ``vn_root``/``output_dir``
            when omitted.
        dry_run: Skip PNG copies and manifest writes.
        timeout: Per-batch subprocess timeout in seconds.

    Returns:
        Path to ``<output_dir>/reports/manifest_index.json``.
    """
    result = _run_native_stage(
        vn_root,
        output_dir,
        modes,
        config=config,
        dry_run=dry_run,
        timeout=timeout,
    )
    info(
        "Native compose: {} rendered from {} states in {} batch(es); "
        "{} layered images.".format(
            counted(result.rendered, "state"),
            result.states,
            result.batches,
            result.layered_images,
        )
    )
    return result.manifest_path


__all__ = ["NativeStageResult", "run_composite_stage"]

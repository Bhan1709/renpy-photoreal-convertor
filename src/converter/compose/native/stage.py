"""Native Ren'Py compose stage: extraction, rendering, and record adaptation.

Responsibility: run the injected ``_composite_export`` command inside the
game's bundled Ren'Py runtime, page through its rendered batches, and adapt the
native protocol results into the existing
:class:`~converter.compose.models.CompositeRecord` /
:class:`~converter.compose.models.CompositeManifest` objects so the review,
apply, and manifest contracts stay unchanged.

The native stage's CFG walk handles every ``show``/``scene`` statement —
including ``show screen`` — so screen-stack composites are produced natively by
the runtime.  No static Pass 1/2/3 logic is needed; compose v2 is the only
compose path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli import counted, info, loading, progress_bar, warning

from ...utility.manifest_bundle import (
    game_script_lock,
    manifest_bundle_lock,
    recover_manifest_bundle,
)
from ...utility.safe_paths import (
    UnsafePathError,
    resolve_game_output_path,
    safe_relative_path,
)
from ..association import CharacterAssociator, CharacterDeclaration
from ..compositing import Compositor
from ..config import DEFAULT_CATEGORY_FOLDERS, GameConfig
from ..errors import (
    ConfigError,
    ManifestError,
    NativeExecutionError,
    NativeLimitError,
    NativeProtocolError,
    NativeStageError,
)
from ..models import CompositeManifest, CompositeRecord, Layer
from ..parsing import ScriptBlock, ScriptIndex, index_scripts, live_scripts
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
    MAX_TOTAL_RENDER_STATES,
    MAX_TOTAL_VARIANTS,
    MIN_RENDER_STATES_PER_BATCH,
    NATIVE_CACHE_DIR_NAME,
    NATIVE_CAS_DIR_NAME,
    NATIVE_DISCOVERY_CACHE_NAME,
    NATIVE_DISCOVERY_CACHE_VERSION,
    NATIVE_RENDER_STORE_DIR_NAME,
    PROTOCOL_SCHEMA,
    PROTOCOL_VERSION,
    VALID_MODES,
    normalize_modes,
)
from .discovery import RuntimeLayout, discover_runtime
from .process import PersistentNativeWorker, run_native_batch
from .protocol import read_result_stream
from .prune import prune_stale_generated_images
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

    @property
    def manifest_index_path(self) -> Path:
        """Return the explicit split-manifest index path."""
        return self.manifest_path


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
        cas_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.index = index
        self.associator = associator
        self.compositor = compositor
        self.dry_run = dry_run
        self.render_dir = render_dir
        self.cas_dir = cas_dir
        self.records: list[CompositeRecord] = []
        self.records_by_key: dict[tuple[str, str, str], CompositeRecord] = {}
        self.key_to_index: dict[tuple[str, str, str], int] = {}
        self.usages_processed = 0
        self.unresolved_errors: set[str] = set()
        self.native_diagnostics: set[str] = set()
        self.publication_cache: dict[str, Path] = {}

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
        if not _safe_native_name(name):
            self.unresolved_errors.add(
                "native render failure: candidate name is not a safe output "
                f"file name: {name!r}"
            )
            return

        normalized = _normalize_layers(
            self._candidate_layers(candidate, layered_meta), self.config
        )
        if not normalized:
            return
        try:
            for layer in normalized:
                safe_relative_path(
                    layer.source,
                    field_name="native layer source",
                )
        except UnsafePathError as exc:
            self.unresolved_errors.add(
                "native render failure: unsafe native layer source: "
                f"{exc}"
            )
            return

        key = _native_record_key(candidate, normalized)
        locations = self._locations(candidate)
        if key in self.records_by_key:
            self._merge(key, locations)
            return

        digest = key[2]
        target, family, _sub, status, family2 = _determine_family_and_path(
            normalized, kind, name, self.config, digest
        )
        paths = self.config.resolved_paths()
        try:
            publication_target = resolve_game_output_path(
                self.config.game_root,
                paths["output_dir"],
                target.relative_to(self.config.game_root.parent),
                excluded_roots=(
                    paths["report_dir"],
                    paths["output_dir"] / "rejected",
                ),
            )
        except (UnsafePathError, ValueError) as exc:
            self.unresolved_errors.add(
                "native render failure: unsafe generated output target: "
                f"{target}: {exc}"
            )
            return

        dimensions: tuple[int, int] | None = None
        if not self.dry_run:
            if not _copy_render(
                candidate,
                publication_target,
                self.render_dir,
                self.cas_dir,
            ):
                context = _native_diagnostic_context(candidate)
                detail = (
                    f"native render failure [{context}]: rendered file could not "
                    "be copied from the disposable workspace"
                    if context
                    else "native render failure: rendered file could not be copied "
                    "from the disposable workspace"
                )
                self.unresolved_errors.add(detail)
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
            content_hash=str(candidate["content_hash"]),
            condition_state=dict(candidate.get("condition_assignments") or {}),
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
                unresolved_error = next(
                    iter(entry.get("unresolved_errors") or ()),
                    None,
                )
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
                                warning=static.warning or unresolved_error,
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
                            warning=unresolved_error,
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
        *,
        content_hash: str | None = None,
        condition_state: dict[str, object] | None = None,
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
            content_hash=content_hash,
            condition_state=condition_state,
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
    """Read and validate one PNG's dimensions from its IHDR chunk."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or int.from_bytes(header[8:12], "big") != 13
        or header[12:16] != b"IHDR"
    ):
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _sha256_file(path: Path) -> str | None:
    """Return one file's SHA-256 digest without loading it into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _atomic_link_or_copy(source: Path, target: Path) -> bool:
    """Publish *source* at *target* through an atomic hard link or copy.

    The shared source is never consumed: the staged directory entry is moved
    over the target, preserving the render/CAS object for other outputs.
    """
    temporary: Path | None = None
    try:
        if source.is_symlink() or not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(raw_temporary)
        os.close(descriptor)
        # The temporary file already exists, so remove it before creating a
        # hard link.  A copy fallback is used on unsupported filesystems.
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        with temporary.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not mask the publication result.
                pass


def _copy_render(
    candidate: dict,
    target: Path,
    render_dir: Path | None,
    cas_dir: Path | None = None,
) -> bool:
    """Publish one rendered PNG through the content-addressed store.

    The native ``content_hash`` remains the canonical pixel identity used by
    the manifest.  The cache additionally records the SHA-256 of the encoded
    PNG bytes so a stored object can be verified before it is reused.  Final
    semantic paths are published by hard link where possible, with an atomic
    copy fallback.
    """
    render_file = candidate.get("render_file")
    if not render_file or render_dir is None:
        return False
    if _is_link_like(render_dir):
        return False
    render_root = render_dir.resolve()
    unresolved_source = render_root / str(render_file)
    if unresolved_source.is_symlink():
        # A symlink could point at an arbitrary host file even when its lexical
        # location is inside the disposable render directory.
        return False
    source = unresolved_source.resolve()
    try:
        source.relative_to(render_root)
    except ValueError:
        # A child result may only reference files from its disposable render
        # directory.  Never follow an absolute or parent-traversing path out
        # of that workspace.
        return False
    if not source.is_file():
        return False

    expected_size = (
        int(candidate.get("width") or 0),
        int(candidate.get("height") or 0),
    )
    if expected_size != (0, 0) and _png_size(source) != expected_size:
        return False

    publication_source = source
    if cas_dir is not None:
        blob_hash = _sha256_file(source)
        if blob_hash is None:
            return False
        if _is_link_like(cas_dir):
            return False
        cas_root = cas_dir.resolve()
        cas_parent = _prepare_cache_subdirectory(
            cas_root,
            cas_root / blob_hash[:2],
        )
        if cas_parent is None:
            return False
        cas_path = cas_parent / f"{blob_hash}.png"
        if _is_link_like(cas_path):
            return False
        if cas_path.is_file():
            # A corrupt cache object is never reused or silently published.
            # The render store remains intact and can republish the canonical
            # bytes after the cache is repaired.
            if _sha256_file(cas_path) != blob_hash:
                return False
        elif not _atomic_link_or_copy(source, cas_path):
            return False
        publication_source = cas_path

    return _atomic_link_or_copy(publication_source, target)


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


def _safe_native_name(name: str) -> bool:
    """Return whether one native image name is safe as an output file name."""
    if not name or name in {".", ".."}:
        return False
    try:
        relative = safe_relative_path(name, field_name="native image name")
    except UnsafePathError:
        return False
    return len(relative.parts) == 1


def _native_record_key(
    candidate: dict[str, Any],
    normalized: list[Layer],
) -> tuple[str, str, str]:
    """Return the host identity key for one native render candidate."""
    content_hash = candidate.get("content_hash")
    condition_state = candidate.get("condition_assignments") or {}
    return _record_key(
        str(candidate["kind"]),
        str(candidate["name"]),
        normalized,
        str(content_hash) if content_hash else None,
        dict(condition_state),
    )


def _native_diagnostic_context(candidate: dict[str, Any]) -> str | None:
    """Return identifying context for one native candidate, if available."""
    candidate_id = candidate.get("candidate_id")
    name = candidate.get("name")
    image_name = candidate.get("image_name")
    usages = candidate.get("usages")

    has_context = any(
        value not in (None, "", [], ())
        for value in (candidate_id, name, image_name, usages)
    )
    if not has_context:
        return None

    if isinstance(image_name, str):
        image_text = image_name
    elif isinstance(image_name, (list, tuple)):
        image_text = " ".join(str(part) for part in image_name)
    else:
        image_text = str(image_name) if image_name else ""
    usage_text = ", ".join(
        (
            f"{usage.get('script', '<unknown>')}:{usage.get('line', 0)}"
            + (
                f" ({usage['statement']})"
                if usage.get("statement")
                else ""
            )
        )
        for usage in (usages or [])
        if isinstance(usage, dict)
    )
    return (
        f"candidate_id={candidate_id or '<unknown>'}, "
        f"name={name or '<unknown>'}, "
        f"image_name={image_text or '<unknown>'}, "
        f"usages=[{usage_text}]"
    )


def _native_layer_diagnostic_context(
    candidate: dict[str, Any],
    layer: dict[str, Any],
) -> str:
    """Return candidate and layer context for a layer-level diagnostic."""
    base = _native_diagnostic_context(candidate)
    sources = layer.get("sources") or []
    source_text = ", ".join(str(source) for source in sources)
    attribute = layer.get("attribute")
    group = layer.get("group")
    details = (
        f"layer_sources=[{source_text}], "
        f"layer_attribute={attribute or '<none>'}, "
        f"layer_group={group or '<none>'}"
    )
    return f"{base}, {details}" if base else details


def _native_unresolved_errors(candidates: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return unique, contextual native diagnostics from render candidates."""
    diagnostics: dict[tuple[str, str, str], str] = {}
    for candidate in candidates:
        context = _native_diagnostic_context(candidate)
        for error in candidate.get("unresolved_errors") or []:
            text = str(error)
            key = ("condition", context or "", text)
            if context:
                text = f"native condition error [{context}]: {text}"
            diagnostics[key] = text
        for layer in candidate.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            layer_context = _native_layer_diagnostic_context(candidate, layer)
            for error in layer.get("unresolved_errors") or []:
                text = str(error)
                key = ("layer-condition", layer_context, text)
                text = f"native layer condition error [{layer_context}]: {text}"
                diagnostics[key] = text
        error = candidate.get("error")
        if error:
            text = str(error)
            key = ("render", context or "", text)
            if context:
                text = f"native render error [{context}]: {text}"
            diagnostics[key] = text
    return tuple(sorted(diagnostics.values()))


def _native_walk_state(result: dict[str, Any]) -> dict[str, object]:
    """Return the native CFG walk state carried by the last batch result."""
    batch = result.get("batch") or {}
    walk = batch.get("walk") or {}
    if not isinstance(walk, dict):
        return {}
    return {str(key): value for key, value in walk.items()}


def _validate_modes(modes: list[str] | None) -> list[str]:
    """Normalise and validate the requested compose modes."""
    requested = [
        normalized
        for mode in (modes or [])
        if (normalized := str(mode).strip().lower())
    ]
    unknown = sorted(set(requested) - set(VALID_MODES))
    if unknown:
        raise NativeStageError(
            f"Unknown native compose mode(s): {', '.join(unknown)}. "
            f"Valid modes: {', '.join(VALID_MODES)}."
        )
    return normalize_modes(requested)


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
    states_path: Path,
    result_path: Path,
    max_render_states: int = MAX_RENDER_STATES_PER_BATCH,
    max_total_states: int = MAX_TOTAL_RENDER_STATES,
    max_total_variants: int = MAX_TOTAL_VARIANTS,
    discovery_fingerprint: str = "",
    session_id: str = "",
    job_id: int = 0,
    attempt: int = 1,
) -> dict[str, Any]:
    """Build one native batch's engine configuration."""
    return {
        "game_root": str(game_root),
        "render_dir": str(render_dir),
        "states_path": str(states_path),
        "result_path": str(result_path),
        "modes": list(modes),
        "defaults": dict(defaults),
        "max_synthetic_states": MAX_SYNTHETIC_ATTRIBUTE_STATES,
        "max_condition_assignments": MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE,
        "max_render_states": max_render_states,
        "max_total_states": max_total_states,
        "max_total_variants": max_total_variants,
        "max_cfg_states": MAX_CFG_PROCESSED_NODES,
        "max_cfg_depth": MAX_CFG_CALL_DEPTH,
        "batch_index": batch_index,
        "state_offset": state_offset,
        "progress_path": str(progress_path),
        "discovery_fingerprint": discovery_fingerprint,
        "session_id": session_id,
        "job_id": job_id,
        "attempt": attempt,
    }


def _script_files(config: GameConfig) -> list[str]:
    """Scripts to stage into the disposable workspace (relative to game root).

    Uses the scripts actually present in the game folder, not the list recorded
    by decompile, so the child Ren'Py engine sees the real game.  Archive and
    generated copies are excluded to avoid duplicate labels.
    """
    return live_scripts(config)


def _discovery_fingerprint(
    *,
    game_root: Path,
    script_files: list[str],
    layout: RuntimeLayout,
    modes: list[str],
    defaults: dict,
    max_total_states: int,
    max_total_variants: int,
) -> str:
    """Hash every input that can change native state enumeration.

    The disposable workspace path is deliberately excluded.  A durable
    discovery cache may therefore be reused by a later compose run of the
    same game, while any script, runtime, mode, default, or budget change
    invalidates it.
    """
    digest = hashlib.sha256()
    digest.update(b"renpy-compose-native-discovery-v3\0")
    digest.update(PROTOCOL_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(PROTOCOL_SCHEMA).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(NATIVE_DISCOVERY_CACHE_VERSION).encode("ascii"))
    digest.update(b"\0")
    digest.update(layout.version.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(
        json.dumps(
            {
                "modes": list(modes),
                "defaults": defaults,
                "max_total_states": max_total_states,
                "max_total_variants": max_total_variants,
            },
            sort_keys=True,
            default=repr,
        ).encode("utf-8")
    )
    for relative in sorted(script_files):
        path = game_root / relative
        digest.update(b"\0")
        digest.update(relative.replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        try:
            content = path.read_bytes()
        except OSError:
            digest.update(b"<missing>")
            continue
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _previous_dynamic_states(config: GameConfig) -> list[dict]:
    """Carry the previous manifest's dynamic states forward."""
    paths = config.resolved_paths()
    index_path = paths["manifest_index_path"]
    with manifest_bundle_lock(index_path, report_dir=paths["report_dir"]):
        recover_manifest_bundle(index_path, paths["report_dir"])
        if not index_path.is_file():
            return []
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        states = data.get("dynamic_states", []) if isinstance(data, dict) else []
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


def _is_link_like(path: Path) -> bool:
    """Return whether a path is a symlink or Windows junction alias."""
    return path.is_symlink() or path.is_junction()


def _prepare_cache_directory(path: Path, root: Path, *, label: str) -> Path:
    """Create one real cache directory and keep it contained under *root*."""
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise NativeStageError(
            f"Native {label} path escapes the cache root: {path}"
        ) from exc
    if _is_link_like(path):
        raise NativeStageError(
            f"Native {label} path must not be a symbolic link or junction: {path}"
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise NativeStageError(
            f"Native {label} path is not contained by the cache root: {path}"
        ) from exc
    if _is_link_like(path) or path.resolve() != resolved:
        raise NativeStageError(
            f"Native {label} path changed while it was being prepared: {path}"
        )
    return path


def _prepare_cache_subdirectory(root: Path, directory: Path) -> Path | None:
    """Create a contained cache subdirectory without following link aliases."""
    root_resolved = root.resolve()
    try:
        relative = directory.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            return None
        try:
            current.mkdir(exist_ok=True)
            resolved = current.resolve()
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            return None
        if _is_link_like(current):
            return None
        current = resolved
    return current


def _native_cache_paths(
    config: GameConfig,
    workspace: NativeWorkspace,
    *,
    dry_run: bool,
) -> tuple[Path, Path, Path, Path]:
    """Return durable cache, discovery, render-store, and CAS paths."""
    output_root = config.resolved_paths()["output_dir"].resolve()
    if config.native_cache_enabled and not dry_run:
        cache_path = output_root / NATIVE_CACHE_DIR_NAME
        if _is_link_like(cache_path):
            raise NativeStageError(
                "Native cache path must not be a symbolic link or junction: "
                f"{cache_path}"
            )
        try:
            cache_path.mkdir(parents=True, exist_ok=True)
            cache_root = cache_path.resolve()
            cache_root.relative_to(output_root)
        except (OSError, ValueError) as exc:
            raise NativeStageError(
                f"Native cache path escapes the output directory: {cache_path}"
            ) from exc
        if _is_link_like(cache_path):
            raise NativeStageError(
                "Native cache path must not be a symbolic link or junction: "
                f"{cache_path}"
            )
    else:
        cache_path = workspace.root / "native-cache"
        cache_root = _prepare_cache_directory(
            cache_path,
            workspace.root,
            label="workspace cache",
        ).resolve()

    states_path = cache_root / NATIVE_DISCOVERY_CACHE_NAME
    render_dir = _prepare_cache_directory(
        cache_root / NATIVE_RENDER_STORE_DIR_NAME,
        cache_root,
        label="render store",
    )
    cas_dir = _prepare_cache_directory(
        cache_root / NATIVE_CAS_DIR_NAME,
        cache_root,
        label="PNG CAS",
    )
    if _is_link_like(states_path):
        raise NativeStageError(
            "Native discovery cache must not be a symbolic link or junction: "
            f"{states_path}"
        )
    return cache_root, states_path, render_dir, cas_dir


def _run_batches(
    workspace: NativeWorkspace,
    modes: list[str],
    defaults: dict,
    layout: RuntimeLayout,
    timeout: int,
    *,
    render_dir: Path,
    states_path: Path,
    max_total_states: int,
    max_total_variants: int,
    discovery_fingerprint: str,
    session_id: str,
    persistent_worker: bool,
    header_cb: Callable[[dict[str, Any]], None],
    candidate_cb: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Render native batches while adapting one batch at a time.

    The preferred path reuses one Ren'Py worker for the complete page
    sequence.  If that optional mode is unavailable, the existing one-shot
    subprocess path remains a compatible fallback.  Raw candidates are never
    accumulated across batches: each complete schema-v2 result is validated,
    adapted, and released before the next page is requested.
    """
    batch_index = 0
    job_sequence = 0
    state_offset = 0
    batch_size = MAX_RENDER_STATES_PER_BATCH
    last: dict[str, Any] = {}
    attempt = 0
    total_states: int | None = None
    reported_total_states: int | None = None
    rendered_total = 0
    bar: Any = None
    batch_rendered = 0
    spinner_stack: ExitStack | None = None
    worker: PersistentNativeWorker | None = None

    if persistent_worker:
        try:
            worker = PersistentNativeWorker(
                layout=layout,
                basedir=workspace.root,
                savedir=workspace.savedir,
                timeout=timeout,
            )
            worker.start()
        except (NativeExecutionError, OSError):
            worker = None

    with ExitStack() as stack:

        def _start_spinner() -> None:
            nonlocal spinner_stack
            if bar is not None or spinner_stack is not None:
                return
            spinner_stack = ExitStack()
            stack.enter_context(spinner_stack)
            spinner_stack.enter_context(
                loading(f"Native batch {batch_index}: enumerating states...")
            )

        def _stop_spinner() -> None:
            nonlocal spinner_stack
            if spinner_stack is None:
                return
            spinner_stack.close()
            spinner_stack = None

        def _set_status(message: str) -> None:
            if bar is None:
                return
            # tqdm_rich.set_description() appends a stray ': ', so update the
            # description the same way display() reads it.
            bar.desc = message
            bar.display()

        def _ensure_bar(total: int) -> None:
            nonlocal bar, total_states
            if bar is not None:
                return
            _stop_spinner()
            total_states = total
            bar = stack.enter_context(
                progress_bar(
                    total=total,
                    desc=f"Native batch {batch_index}: rendering",
                    unit="states",
                    # Clearing on close avoids a stale duplicate bar when an
                    # interrupt message lands in the middle of the live line.
                    leave=False,
                )
            )

        def _set_progress(absolute: int) -> None:
            nonlocal rendered_total
            if bar is None:
                return
            if absolute < rendered_total:
                # A failed batch retries from the same offset; do not keep its
                # partial progress or the bar would over-count.
                bar.n = absolute
                bar.display()
            elif absolute > rendered_total:
                bar.update(absolute - rendered_total)
            rendered_total = absolute

        def _cb(done: int) -> None:
            nonlocal batch_rendered
            batch_rendered = done
            _set_progress(state_offset + done)

        def _total_cb(total: int) -> None:
            if total_states is not None:
                return
            _ensure_bar(total)
            _set_progress(state_offset + batch_rendered)

        def _write_one_shot_config(
            progress_log: Path,
            result_path: Path,
        ) -> None:
            config = _engine_config(
                game_root=workspace.game_dir,
                render_dir=render_dir,
                modes=modes,
                defaults=defaults,
                batch_index=batch_index,
                state_offset=state_offset,
                progress_path=progress_log,
                states_path=states_path,
                result_path=result_path,
                max_render_states=batch_size,
                max_total_states=max_total_states,
                max_total_variants=max_total_variants,
                discovery_fingerprint=discovery_fingerprint,
                session_id=session_id,
                job_id=batch_index,
                attempt=attempt,
            )
            workspace.config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        try:
            while True:
                attempt += 1
                job_sequence += 1
                batch_rendered = 0
                _set_progress(state_offset)
                _set_status(f"Native batch {batch_index}: rendering")

                progress_log = workspace.root / f"progress-{job_sequence:06d}.log"
                try:
                    progress_log.write_text("", encoding="utf-8")
                except OSError:
                    pass

                result_path = workspace.result_path
                used_worker = worker is not None
                if used_worker:
                    assert worker is not None
                    config = _engine_config(
                        game_root=workspace.game_dir,
                        render_dir=render_dir,
                        modes=modes,
                        defaults=defaults,
                        batch_index=batch_index,
                        state_offset=state_offset,
                        progress_path=progress_log,
                        states_path=states_path,
                        result_path=workspace.result_path,
                        max_render_states=batch_size,
                        max_total_states=max_total_states,
                        max_total_variants=max_total_variants,
                        discovery_fingerprint=discovery_fingerprint,
                        session_id=session_id,
                        job_id=batch_index,
                        attempt=attempt,
                    )
                    _start_spinner()
                    try:
                        result_path = worker.run_job(
                            job_sequence,
                            config,
                            progress_path=progress_log,
                            timeout=timeout,
                            progress_cb=_cb,
                            total_cb=_total_cb,
                        )
                    except NativeExecutionError:
                        _stop_spinner()
                        worker.abort()
                        worker = None
                        # Fall through to the compatible one-shot path for this
                        # logical batch.  The batch index and offset do not
                        # change, so no state can be skipped or duplicated.
                        used_worker = False
                    else:
                        _stop_spinner()

                if not used_worker:
                    _write_one_shot_config(progress_log, workspace.result_path)
                    try:
                        workspace.result_path.unlink(missing_ok=True)
                    except OSError as exc:
                        raise NativeStageError(
                            "Could not clear native result file before launch: "
                            f"{workspace.result_path}: {exc}"
                        ) from exc
                    _start_spinner()
                    try:
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
                    except NativeExecutionError as execution_error:
                        _stop_spinner()
                        # The child may have written a structured error result
                        # before exiting.  Classify limit/protocol failures
                        # explicitly instead of retrying a deterministic error
                        # with a smaller batch.
                        if workspace.result_path.is_file():
                            try:
                                read_result_stream(
                                    workspace.result_path,
                                    expected_batch_index=batch_index,
                                    expected_state_offset=state_offset,
                                )
                            except NativeLimitError:
                                raise
                            except NativeProtocolError as protocol_error:
                                raise protocol_error from execution_error
                        if batch_size <= MIN_RENDER_STATES_PER_BATCH:
                            raise
                        batch_size = max(
                            MIN_RENDER_STATES_PER_BATCH,
                            batch_size // 2,
                        )
                        info(
                            f"Native batch {batch_index} failed (attempt "
                            f"{attempt}); retrying with {batch_size} states "
                            "per batch."
                        )
                        continue
                    _stop_spinner()
                    result_path = workspace.result_path

                try:
                    data = read_result_stream(
                        result_path,
                        expected_batch_index=batch_index,
                        expected_state_offset=state_offset,
                        expected_session_id=session_id,
                        expected_job_id=batch_index,
                        expected_attempt=attempt,
                        header_cb=header_cb,
                        candidate_cb=candidate_cb,
                    )
                except NativeLimitError:
                    raise
                except NativeProtocolError:
                    if not used_worker:
                        raise
                    # A persistent-worker response that cannot be validated is
                    # not accepted.  Fall back to a fresh one-shot child for
                    # the same logical batch.
                    if worker is not None:
                        worker.abort()
                    worker = None
                    continue

                if data.get("status") == "error":
                    if used_worker:
                        if worker is not None:
                            worker.abort()
                        worker = None
                        continue
                    error = data.get("error") or {}
                    raise NativeProtocolError(
                        "Native Ren'Py compose stage failed: "
                        + str(error.get("message", "")).strip()
                    )

                last = data
                batch = data["batch"]
                total = int(batch.get("total_states") or 0)
                if reported_total_states is None:
                    reported_total_states = total
                elif total != reported_total_states:
                    raise NativeProtocolError(
                        "Native stage batch total_states changed between pages "
                        f"(expected {reported_total_states}, got {total})"
                    )
                if bar is None and total:
                    _ensure_bar(total)
                _set_progress(state_offset + int(batch.get("rendered", 0)))
                _set_status(
                    "Native batch {}: rendered {} of {} states.".format(
                        batch_index, batch.get("rendered", 0), total
                    )
                )
                next_offset = batch.get("next_offset")
                if next_offset is None:
                    # The live bar is transient, so leave one permanent summary.
                    info(
                        f"Native render complete: {rendered_total} of {total} "
                        "states rendered."
                    )
                    return last
                state_offset = int(next_offset)
                batch_index += 1
                attempt = 0
        finally:
            if worker is not None:
                worker.close()


def _run_native_stage(
    vn_root: Path,
    output_dir: Path,
    modes: list[str],
    *,
    config: GameConfig | None = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
) -> NativeStageResult:
    """Run the native stage while holding the manifest bundle lock."""
    game_config = _resolve_config(vn_root, output_dir, config)
    paths = game_config.resolved_paths()
    with (
        manifest_bundle_lock(
            paths["manifest_index_path"],
            report_dir=paths["report_dir"],
        ),
        game_script_lock(game_config.game_root),
    ):
        recover_manifest_bundle(
            paths["manifest_index_path"],
            paths["report_dir"],
        )
        return _run_native_stage_unlocked(
            vn_root,
            output_dir,
            modes,
            config=game_config,
            dry_run=dry_run,
            timeout=timeout,
        )


def _run_native_stage_unlocked(
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
    reports_dir = paths["report_dir"]
    manifest_index_path = paths["manifest_index_path"]
    # The native result is kept inside the disposable workspace.  The report
    # directory is still created here because the final manifest bundle lives
    # there.  Remove the legacy shared result path if an older run left one.
    reports_dir.mkdir(parents=True, exist_ok=True)
    legacy_result_path = reports_dir / ".native-result.json"
    try:
        legacy_result_path.unlink(missing_ok=True)
    except OSError:
        pass

    compositor = Compositor(game_config)
    script_lines = {
        path: file_index.lines for path, file_index in index.per_file.items()
    }
    associator = CharacterAssociator(game_config, script_lines)

    workspace: NativeWorkspace | None = None
    try:
        script_files = _script_files(game_config)
        workspace = create_workspace(
            game_root=game_config.game_root,
            script_files=script_files,
            engine_config={},
            result_path=Path(".native-result.json"),
            workspace_parent=game_config.game_root.parent,
            archive_dirs=[path for path in game_config.archive_dirs],
        )
        cache_root, states_path, render_dir, cas_dir = _native_cache_paths(
            game_config,
            workspace,
            dry_run=dry_run,
        )
        fingerprint = _discovery_fingerprint(
            game_root=game_config.game_root,
            script_files=script_files,
            layout=layout,
            modes=modes,
            defaults=dict(index.all_defaults),
            max_total_states=game_config.native_max_total_states,
            max_total_variants=game_config.native_max_total_variants,
        )
        session_id = uuid.uuid4().hex

        sink = _RecordSink(
            game_config,
            index,
            associator,
            compositor,
            dry_run=dry_run,
            render_dir=render_dir,
            cas_dir=cas_dir,
        )
        layered_meta: dict[str, dict] = {}
        image_meta: dict[str, dict] = {}
        candidate_count = 0
        failed = 0
        empty = 0

        def _header_cb(result: dict[str, Any]) -> None:
            layered_meta.clear()
            layered_meta.update(
                {
                    str(entry["name"]): entry
                    for entry in result.get("layered_images", [])
                }
            )
            image_meta.clear()
            image_meta.update(
                {
                    str(name): definition
                    for name, definition in (
                        result.get("image_definitions") or {}
                    ).items()
                }
            )

        def _candidate_cb(candidate: dict[str, Any]) -> None:
            nonlocal candidate_count, failed, empty
            candidate_count += 1
            sink.native_diagnostics.update(
                _native_unresolved_errors([candidate])
            )
            if candidate.get("error"):
                failed += 1
                return
            if not candidate.get("content_hash"):
                empty += 1
                return
            sink.add_native_candidate(candidate, layered_meta, image_meta)

        last = _run_batches(
            workspace,
            modes,
            dict(index.all_defaults),
            layout,
            timeout,
            render_dir=render_dir,
            states_path=states_path,
            max_total_states=game_config.native_max_total_states,
            max_total_variants=game_config.native_max_total_variants,
            discovery_fingerprint=fingerprint,
            session_id=session_id,
            persistent_worker=game_config.native_persistent_worker,
            header_cb=_header_cb,
            candidate_cb=_candidate_cb,
        )

        dynamic_declarations = _dynamic_declarations(index, game_config)
        walk_state = _native_walk_state(last)
        processed_value = walk_state.get("processed", 0)
        cfg_processed = (
            int(processed_value)
            if isinstance(processed_value, (int, float, str))
            else 0
        )
        cfg_truncated = bool(walk_state.get("truncated", False))
        budget = dict(last.get("batch", {}).get("budget") or {})
        manifest = CompositeManifest(
            composites=tuple(sink.records),
            dynamic_states=_previous_dynamic_states(game_config),
            image_declarations=_declaration_records(index, game_config),
            dynamic_declarations=dynamic_declarations,
            unresolved=tuple(
                sorted(
                    sink.native_diagnostics | set(sink.unresolved_errors)
                )
            ),
            summary={
                "scripts_scanned": len(index.files),
                "composites": len(sink.records),
                "uses_processed": sink.usages_processed,
                "native_states": candidate_count,
                "native_failed_states": failed,
                "native_empty_states": empty,
                "native_batches": int(last.get("batch", {}).get("index", 0)) + 1,
                "native_layered_images": len(layered_meta),
                "image_declarations_indexed": len(index.image_decls),
                "dynamic_declarations": len(dynamic_declarations),
                "cfg_processed": cfg_processed,
                "cfg_truncated": cfg_truncated,
                "native_budget": budget,
                "native_cache_enabled": game_config.native_cache_enabled,
                "native_cache_path": str(cache_root),
                "native_persistent_worker": game_config.native_persistent_worker,
            },
            cfg_processed=cfg_processed,
            cfg_truncated=cfg_truncated,
        )

        from ..manifest import ManifestWriter

        ManifestWriter(
            game_config,
            manifest_index_path=manifest_index_path,
            report_dir=reports_dir,
        ).write(
            manifest,
            dry_run=dry_run,
            extra_characters=_native_characters(last.get("characters") or {}),
        )

        # Stale-image cleanup is intentionally after the manifest transaction.
        # A failure before publication leaves the previous bundle and its
        # images untouched.  Once the commit marker is durable, cleanup is
        # post-commit maintenance: a pruning failure is reported without
        # pretending that the published bundle was rolled back.  Live script
        # references are retained by the pruner, so an already-applied
        # generation remains usable until the next apply.
        if not dry_run:
            try:
                prune_result = prune_stale_generated_images(
                    game_config,
                    manifest_index_path=manifest_index_path,
                    report_dir=reports_dir,
                )
            except (ManifestError, OSError) as exc:
                # The manifest transaction is already committed.  A cleanup
                # failure must not make callers believe the new bundle was
                # rolled back, and the pruner itself validates before its
                # first deletion.
                warning(
                    "Compose completed, but stale-image pruning was skipped: "
                    f"{exc}"
                )
            else:
                if prune_result.removed:
                    info(
                        f"Removed {len(prune_result.removed)} stale generated "
                        "image(s)."
                    )
                if prune_result.skipped:
                    info(
                        f"Skipped {len(prune_result.skipped)} generated path(s) "
                        "during stale-image pruning."
                    )

        batch = last.get("batch", {})
        return NativeStageResult(
            manifest_path=manifest_index_path,
            manifest=manifest,
            batches=int(batch.get("index", 0)) + 1,
            states=candidate_count,
            rendered=candidate_count - failed - empty,
            failed_states=failed,
            empty_states=empty,
            layered_images=len(layered_meta),
            characters=len(last.get("characters") or {}),
        )
    finally:
        if workspace is not None:
            try:
                workspace.cleanup()
            except OSError as cleanup_exc:
                active_error = sys.exception()
                if active_error is None:
                    raise
                active_error.add_note(
                    f"Native workspace cleanup also failed: {cleanup_exc}"
                )


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
        The configured manifest index path.  With default configuration this is
        ``<output_dir>/reports/manifest_index.json``.
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

"""Record emission helpers for the compose pass.

All functions that build, enrich, or render :class:`~converter.compose.models.CompositeRecord`
instances live here.  This keeps :mod:`converter.compose.composer` focused on
orchestration (pass ordering, index consumption, progress reporting) rather
than record construction details.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from .character_associator import CharacterAssociator
from .compositor import Compositor
from .config import GameConfig
from .models import (
    CompositeRecord,
    DefinitionLocation,
    Layer,
    UsageLocation,
)
from .parser import (
    ScriptBlock,
    ScriptIndex,
    find_nearby_dialog,
)
from .resolver import Resolver


def _loc_to_usage(loc: dict) -> UsageLocation:
    """Convert a location dict to a :class:`UsageLocation`."""
    return UsageLocation(
        script=loc["script"], line=loc["line"], statement=loc["statement"],
        state_snapshot=loc.get("state_snapshot"),
    )


def _normalize_layers(layers: list[Layer], config: GameConfig) -> list[Layer]:
    """Keep only layers whose source lives under a configured asset folder.

    Pure filtering/copying — no disk or image work — so it is cheap to run for
    every usage before the dedup check.
    """
    prefixes = tuple(f"{n}/" for n in config.categories.values())
    return [
        Layer(
            source=x.source, line=x.line, group=x.group, conditions=x.conditions,
            attribute=x.attribute, default_attribute=x.default_attribute,
            offset=x.offset, offset_rel=x.offset_rel, chain_id=x.chain_id,
            resolved_path=x.resolved_path, resolution_method=x.resolution_method,
            warning=x.warning, unresolved_conditions=x.unresolved_conditions,
            via_image_tag=x.via_image_tag, role=x.role, canvas_size=x.canvas_size,
            declaration_expression=x.declaration_expression,
        )
        for x in layers
        if any(x.source.lower().startswith(p) for p in prefixes)
        and not config.is_image_excluded(x.source.lower())
    ]


def _restore_layer_order(layers: list[Layer], source_order: list[Layer]) -> list[Layer]:
    """Restore declaration/stack order after conditional layers were grouped.

    The condition-selection pass separates unconditional layers from ``if``
    chains for evaluation. Emitting those buckets directly changes z-order:
    conditional layers that were declared before facial attributes can end up
    on top of them. Match selected layers back to their original positions.
    """

    def key(layer: Layer) -> tuple:
        return (
            layer.source, layer.line, layer.group, layer.conditions,
            layer.attribute, layer.chain_id, layer.via_image_tag, layer.role,
        )

    positions: dict[tuple, int] = {}
    for index, layer in enumerate(source_order):
        positions.setdefault(key(layer), index)
    return sorted(layers, key=lambda layer: positions.get(key(layer), len(positions)))


def _record_key(kind: str, name: str, normalized: list[Layer]) -> tuple[str, str, str]:
    """Dedup key for a composite: (kind, name, sha1 of normalized layer semantics)."""
    semantic = json.dumps(
        [
            kind,
            name,
            [
                (
                    x.source,
                    list(x.conditions),
                    x.attribute,
                    x.offset,
                    x.offset_rel,
                    x.canvas_size,
                )
                for x in normalized
            ],
        ],
        sort_keys=True,
    )
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    return (kind, name, digest)


def _determine_family_and_path(
    normalized: list[Layer], kind: str, name: str, config: GameConfig, digest: str,
) -> tuple[Path, str, Path, str, bool, str]:
    """Determine output family, sub-path, status, and target PNG path for a composite.

    Returns ``(target, family, sub, status, is_xxx, family2)``.
    """
    dynamic = any(x.unresolved_conditions for x in normalized)

    folder = next(
        (x.source.split("/")[:-1] for x in normalized if "/" in x.source), [kind]
    )
    family = config.category_for_folder(folder[0]) if folder and folder[0] else kind
    if family is None:
        family = folder[0] if folder else kind
    sub = Path(*folder[1:]) if len(folder) > 1 else Path()
    status = "dynamic_review" if dynamic else "static"
    paths = config.resolved_paths()
    target = paths["output_dir"] / status / family / sub / f"{name}__{digest}.png"

    family2 = ""
    for x in normalized:
        if "/" in x.source:
            f2 = config.category_for_folder(x.source.split("/", 1)[0].lower())
            if f2 is None:
                f2 = x.source.split("/", 1)[0].lower()
            family2 = f2
            break

    scene_folder = config.folder_for("scene").lower()
    is_xxx = any(
        x.source.split("/", 1)[0].lower() == scene_folder
        for x in normalized if "/" in x.source
    )

    return target, family, sub, status, is_xxx, family2


def _render_or_reuse_png(
    normalized: list[Layer], target: Path, compositor: Compositor, dry_run: bool,
) -> tuple[int, int] | None:
    """Render a composite PNG, or reuse an existing one if already on disk.

    Returns the image dimensions, or ``None`` if rendering failed or no image
    could be produced.
    """
    dimensions: tuple[int, int] | None = None

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            # Re-read dimensions from the existing PNG without re-compositing.
            # The digest in the filename is a self-validating cache key: if the
            # output file exists, its inputs are unchanged.
            try:
                with Image.open(target) as _im:
                    dimensions = _im.size
            except (OSError, ValueError):
                dimensions = None
        else:
            image = compositor.composite(normalized, canvas_size=None)
            if image is None:
                return None
            dimensions = (int(image.size[0]), int(image.size[1]))
            image.save(target)

    return dimensions


def _build_base_record(
    name: str,
    kind: str,
    normalized: list[Layer],
    locations: list[dict],
    block: ScriptBlock,
    config: GameConfig,
    target: Path,
    family: str,
    status: str,
    dimensions: tuple[int, int] | None,
) -> CompositeRecord:
    """Build the base :class:`CompositeRecord` from shared data."""
    usage_locations = tuple(
        UsageLocation(
            script=loc["script"], line=loc["line"], statement=loc["statement"],
            state_snapshot=loc.get("state_snapshot"),
        )
        for loc in locations
    )
    return CompositeRecord(
        composite_path=target.relative_to(config.game_root.parent).as_posix(),
        name=name,
        kind=kind,
        status=status,
        definition=DefinitionLocation(
            script=block.path.relative_to(config.game_root.parent).as_posix(),
            line=block.line,
        ),
        usage_locations=usage_locations,
        ordered_layers=tuple(normalized),
        dimensions=dimensions,
        dynamic_conditions=tuple(
            sorted({c for x in normalized for c in x.unresolved_conditions})
        ),
        resolved_conditions=tuple(
            sorted({c for x in normalized for c in x.conditions})
        ),
        family=family,
    )


def _enrich_xxx_record(
    record: CompositeRecord,
    normalized: list[Layer],
    usage_locations: tuple[UsageLocation, ...],
    block: ScriptBlock,
    index: ScriptIndex,
    associator: CharacterAssociator,
    config: GameConfig,
) -> CompositeRecord:
    """Enrich a record in the ``xxx`` family with character associations."""
    char_locs = [
        {"script": l.script, "line": l.line, "statement": l.statement}
        for l in usage_locations
    ]
    file_index = index.per_file.get(block.path)
    chars = associator.associate(
        list(normalized), record.name, block, char_locs,
        lines=file_index.lines if file_index is not None else None,
    )
    return record.replace(
        characters=tuple(chars),
        character_association_status="associated" if chars else "missing",
        family="xxx",
    )


def _enrich_char_record(
    record: CompositeRecord, normalized: list[Layer], associator: CharacterAssociator,
) -> CompositeRecord:
    """Enrich a ``char`` record with a character display name."""
    record = record.replace(family="char")
    char_folder = None
    for x in normalized:
        parts = x.source.split("/", 1)
        if len(parts) > 1 and parts[0].lower() == "chars":
            char_folder = parts[1].split("/", 1)[0]
            break
    if char_folder:
        record = record.replace(character_name=associator.display_name(char_folder))
    return record


def _enrich_scene_record(
    record: CompositeRecord, block: ScriptBlock, index: ScriptIndex, config: GameConfig,
) -> CompositeRecord:
    """Enrich a ``scene`` record with nearby dialog context."""
    record = record.replace(family="scene")
    file_index = index.per_file.get(block.path)
    dialog = find_nearby_dialog(
        block.path,
        block.line,
        lines=list(file_index.lines) if file_index is not None else None,
    )
    if dialog:
        record = record.replace(
            dialog_context=tuple(
                {"speaker": d.speaker, "text": d.text, "line": d.line}
                for d in dialog
            )
        )
    return record


def _emit_record(
    name: str, kind: str, normalized: list[Layer], locations: list[dict], block: ScriptBlock,
    index: ScriptIndex, config: GameConfig, resolver: Resolver, compositor: Compositor,
    associator: CharacterAssociator, *,
    dry_run: bool = False, digest: str | None = None,
) -> CompositeRecord | None:
    """Register a composite record (and optionally render its PNG).

    ``normalized`` must already be the prefix-filtered layer list; the dedup
    key is computed by the caller, so this function only performs the
    per-unique-composite work (render / association / dialog) once.
    Returns ``None`` if the stack could not be normalized to a configured
    asset folder or rendering failed.
    """
    digest = digest or _record_key(kind, name, normalized)[2]

    target, family, _sub, status, is_xxx, family2 = _determine_family_and_path(
        normalized, kind, name, config, digest
    )

    dimensions = _render_or_reuse_png(normalized, target, compositor, dry_run)
    if dimensions is None and not dry_run:
        return None

    record = _build_base_record(
        name, kind, normalized, locations, block, config, target, family,
        status, dimensions,
    )
    usage_locations = record.usage_locations

    if is_xxx:
        record = _enrich_xxx_record(
            record, normalized, usage_locations, block, index, associator, config,
        )
    elif family2 == "char":
        record = _enrich_char_record(record, normalized, associator)
    elif family2 == "scene":
        record = _enrich_scene_record(record, block, index, config)

    return record

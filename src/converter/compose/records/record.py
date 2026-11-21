"""Record emission helpers for the compose pass.

All functions that build, enrich, or render :class:`~converter.compose.models.CompositeRecord`
instances live here.  This keeps :mod:`converter.compose.native.stage`
focused on orchestration (pass ordering, index consumption, progress reporting)
rather than record construction details.

Compose v2 delegates extraction and rendering to the native Ren'Py stage.
There is no separate v1 static pipeline.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from PIL import Image

from ..association.associator import CharacterAssociator, _sub_non_alnum
from ..compositing import Compositor
from ..config import GameConfig
from ..models import (
    CompositeRecord,
    DefinitionLocation,
    Layer,
    UsageLocation,
)
from ..parsing import (
    ScriptBlock,
    ScriptIndex,
    find_nearby_dialog,
)
from ..resolution import Resolver

# Character composites gather in one folder per character, however deep their
# sprite asset folders nest (``chars/emma/ArmL/`` -> ``static/char/emma/``).
_CHARACTER_FOLDER_DEPTH: Final = 1


def _loc_to_usage(loc: dict) -> UsageLocation:
    """Convert a location dict to a :class:`UsageLocation`."""
    return UsageLocation(
        script=loc["script"],
        line=loc["line"],
        statement=loc["statement"],
        state_snapshot=loc.get("state_snapshot"),
    )


def _resolve_speaker_name(
    speaker: str,
    config: GameConfig,
    associator: CharacterAssociator,
) -> str:
    """Resolve a speaker to its canonical character folder name for display.

    Player aliases map to ``player_folder`` when both are configured in
    ``config.json`` (no hard-coded defaults).  Other special speakers
    (e.g. ``narrator``) are kept as-is.  Remaining speakers are resolved via
    the ``Character()`` alias map (e.g. ``"h"`` -> ``"hazel"``).
    Speakers that cannot be resolved are returned unchanged.
    """
    special_speakers = config.special_speakers
    player_folder = config.player_folder
    player_aliases = set(config.player_aliases)

    # 1. Player alias maps to player_folder (takes priority over special
    #    speakers since "you"/"y" are both in special_speakers).
    if speaker.lower() in {a.lower() for a in player_aliases} and player_folder:
        return player_folder

    # 2. Special speaker — keep as-is.
    if speaker.lower() in {s.lower() for s in special_speakers}:
        return speaker

    # 3. Resolve via Character() alias map.
    matched = associator.resolve_speaker(speaker)
    if matched:
        return min(matched)

    # 4. Cannot resolve — keep the original speaker name.
    return speaker


def _normalize_layers(layers: list[Layer], config: GameConfig) -> list[Layer]:
    """Keep only layers whose source lives under a configured asset folder.

    Pure filtering/copying — no disk or image work — so it is cheap to run for
    every usage before the dedup check.
    """
    prefixes = tuple(f"{n}/" for n in config.categories.values())
    return [
        Layer(
            source=x.source,
            line=x.line,
            group=x.group,
            conditions=x.conditions,
            attribute=x.attribute,
            default_attribute=x.default_attribute,
            offset=x.offset,
            offset_rel=x.offset_rel,
            chain_id=x.chain_id,
            resolved_path=x.resolved_path,
            resolution_method=x.resolution_method,
            warning=x.warning,
            unresolved_conditions=x.unresolved_conditions,
            via_image_tag=x.via_image_tag,
            role=x.role,
            canvas_size=x.canvas_size,
            declaration_expression=x.declaration_expression,
        )
        for x in layers
        if any(x.source.lower().startswith(p) for p in prefixes)
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
            layer.source,
            layer.line,
            layer.group,
            layer.conditions,
            layer.attribute,
            layer.chain_id,
            layer.via_image_tag,
            layer.role,
        )

    positions: dict[tuple, int] = {}
    for index, layer in enumerate(source_order):
        positions.setdefault(key(layer), index)
    return sorted(layers, key=lambda layer: positions.get(key(layer), len(positions)))


def _record_key(
    kind: str,
    name: str,
    normalized: list[Layer],
    content_hash: str | None = None,
    condition_state: Mapping[str, object] | None = None,
) -> tuple[str, str, str]:
    """Return a composite dedup key.

    Native composites additionally include their rendered content hash and the
    condition environment used for that render.  Static callers retain the
    original layer-semantics identity.
    """
    identity: list[object] = [
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
    ]
    if content_hash is not None or condition_state is not None:
        identity.extend(
            [
                content_hash,
                dict(sorted((condition_state or {}).items())),
            ]
        )
    semantic = json.dumps(identity, sort_keys=True)
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    return (kind, name, digest)


def _composite_directory(layers: list[Layer], kind: str) -> list[str]:
    """Return the asset directory chain a composite belongs under.

    The chain starts at the most common category folder among the layers, so a
    couple of shared prop layers (``chars/clothes/...``) cannot hijack a
    character's folder, and extends deeper while a majority of the layers keep
    sharing the same folder.  Layers spread across category folders (a scene
    composite that also pulls in a sprite) resolve to their dominant category.
    """
    chains = [x.source.split("/")[:-1] for x in layers if "/" in x.source]
    if not chains:
        return [kind]

    head_counts = Counter(chain[0] for chain in chains)
    head = max(head_counts.items(), key=lambda item: (item[1], item[0]))[0]
    members = [chain for chain in chains if chain[0] == head]

    chain = [head]
    deepest = max(len(member) for member in members)
    for depth in range(1, deepest):
        counts = Counter(
            member[depth] for member in members if len(member) > depth
        )
        if not counts:
            break
        part, count = max(counts.items(), key=lambda item: (item[1], item[0]))
        if count * 2 < len(members):
            break
        chain.append(part)
    return chain


def _determine_family_and_path(
    normalized: list[Layer],
    kind: str,
    name: str,
    config: GameConfig,
    digest: str,
) -> tuple[Path, str, Path, str, str]:
    """Determine output family, sub-path, status, and target PNG path for a composite.

    Composites are grouped so the output tree stays navigable: the folder chain
    mirrors the game's own asset tree (``bgs/home/`` -> ``static/bg/home/``),
    except that character composites always gather in one folder per character
    instead of body-part subfolders (``chars/emma/ArmL/`` -> ``static/char/emma/``).

    Returns ``(target, family, sub, status, family2)``.
    """
    dynamic = any(x.unresolved_conditions for x in normalized)
    status = "dynamic_review" if dynamic else "static"

    folder = _composite_directory(normalized, kind)
    head = folder[0].lower() if folder else ""
    family = config.category_for_folder(head) if head else kind
    if family is None:
        family = head or kind

    sub_parts = list(folder[1:])
    if family == "char":
        sub_parts = sub_parts[:_CHARACTER_FOLDER_DEPTH]
    sub = Path(*sub_parts) if sub_parts else Path()

    paths = config.resolved_paths()
    target = paths["output_dir"] / status / family / sub / f"{name}__{digest}.png"

    # Enrichment follows the same category as the output folder so a record can
    # never land in a family manifest without that family's enrichment.
    family2 = family

    return target, family, sub, status, family2


def _render_or_reuse_png(
    normalized: list[Layer],
    target: Path,
    compositor: Compositor,
    dry_run: bool,
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
    *,
    content_hash: str | None = None,
    condition_state: Mapping[str, object] | None = None,
) -> CompositeRecord:
    """Build the base :class:`CompositeRecord` from shared data."""
    usage_locations = tuple(
        UsageLocation(
            script=loc["script"],
            line=loc["line"],
            statement=loc["statement"],
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
        content_hash=content_hash,
        condition_state=dict(condition_state or {}),
    )


def _enrich_char_record(
    record: CompositeRecord,
    normalized: list[Layer],
    associator: CharacterAssociator,
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
    record: CompositeRecord,
    normalized: list[Layer],
    usage_locations: tuple[UsageLocation, ...],
    block: ScriptBlock,
    index: ScriptIndex,
    associator: CharacterAssociator,
    config: GameConfig,
) -> CompositeRecord:
    """Enrich a ``scene`` record with character associations and per-location dialog context."""
    record = record.replace(family="scene")

    # Build a lookup from relative script string -> source lines for per-location dialog.
    script_to_lines: dict[str, tuple[str, ...]] = {}
    for file_index in index.per_file.values():
        rel = file_index.path.relative_to(config.game_root.parent).as_posix()
        script_to_lines[rel] = file_index.lines

    # Character association — scene composites can include characters too.
    char_locs = [
        {"script": l.script, "line": l.line, "statement": l.statement}
        for l in usage_locations
    ]
    block_file_index = index.per_file.get(block.path)
    block_lines = list(block_file_index.lines) if block_file_index is not None else None
    chars = associator.associate(
        list(normalized),
        record.name,
        block,
        char_locs,
        lines=block_lines,
    )

    # Nearby dialog/narration surrounding each call site — recorded per usage location.
    # Forward dialogs (spoken while the scene is showing) are weighted more than
    # backward dialogs (spoken before the scene appears).
    enriched_locations: list[UsageLocation] = []
    for loc in usage_locations:
        lines = script_to_lines.get(loc.script)
        dialog = find_nearby_dialog(
            Path(loc.script),
            loc.line,
            max_dialogs_back=1,
            max_dialogs_forward=5,
            lines=list(lines) if lines is not None else None,
        )
        dialog_tuple = (
            tuple(
                {
                    "speaker": _resolve_speaker_name(d.speaker, config, associator),
                    "text": d.text,
                    "line": d.line,
                }
                for d in dialog
            )
            if dialog
            else None
        )
        enriched_locations.append(
            UsageLocation(
                script=loc.script,
                line=loc.line,
                statement=loc.statement,
                state_snapshot=loc.state_snapshot,
                dialog_context=dialog_tuple,
            )
        )
    record = record.replace(usage_locations=tuple(enriched_locations))

    # Dialog-based character inference for scenes.
    # Scene assets come from the xxx/ folder, so asset-path matching won't find
    # characters. Instead, infer from the speakers in nearby dialog, then verify
    # each speaker exists as a character folder under chars/ (or is a special
    # speaker like narrator/you/y, or maps to the player folder).
    special_speakers = config.special_speakers
    player_folder = config.player_folder
    player_aliases = set(config.player_aliases)

    dialog_chars: dict[str, list[str]] = {}
    unmapped_speakers: dict[str, list[str]] = {}
    for loc in enriched_locations:
        if not loc.dialog_context:
            continue
        for d in loc.dialog_context:
            speaker = d["speaker"]
            normalized_speaker = _sub_non_alnum(speaker)
            if not normalized_speaker:
                continue
            evidence = f"dialog:{loc.script}:{loc.line}"

            # 1. Special speaker (narrator, you, y) — always kept.
            if speaker.lower() in {s.lower() for s in special_speakers}:
                dialog_chars.setdefault(speaker, []).append(evidence)
                continue

            # 2. Player alias (you, y) maps to player_folder if configured.
            if speaker.lower() in {a.lower() for a in player_aliases} and player_folder:
                dialog_chars.setdefault(player_folder, []).append(evidence)
                continue

            # 3. Resolve speaker via alias map (Character() declarations).
            #    E.g. speaker "h" -> char folder "hazel" via
            #    ``define h = Character("Hazel", ...)``.
            matched_folders = associator.resolve_speaker(speaker)
            if matched_folders:
                for matched_folder in sorted(matched_folders):
                    dialog_chars.setdefault(matched_folder, []).append(evidence)
            else:
                # Speaker could not be mapped to any character folder — record for review.
                unmapped_speakers.setdefault(speaker, []).append(evidence)

    # Merge dialog-inferred characters with any already found.
    existing_chars = {c["character"] for c in chars}
    merged_chars = list(chars)
    for char_folder in sorted(dialog_chars):
        ev_list = dialog_chars[char_folder]
        if char_folder not in existing_chars:
            merged_chars.append(
                {
                    "character": char_folder,
                    "evidence": sorted(set(ev_list)),
                }
            )
            existing_chars.add(char_folder)

    if merged_chars or unmapped_speakers:
        record = record.replace(
            characters=tuple(merged_chars) if merged_chars else None,
            character_association_status="associated" if merged_chars else None,
            unmapped_speakers=tuple(sorted(unmapped_speakers))
            if unmapped_speakers
            else None,
        )

    return record


def _emit_record(
    name: str,
    kind: str,
    normalized: list[Layer],
    locations: list[dict],
    block: ScriptBlock,
    index: ScriptIndex,
    config: GameConfig,
    resolver: Resolver,
    compositor: Compositor,
    associator: CharacterAssociator,
    *,
    dry_run: bool = False,
    digest: str | None = None,
) -> CompositeRecord | None:
    """Register a composite record (and optionally render its PNG).

    ``normalized`` must already be the prefix-filtered layer list; the dedup
    key is computed by the caller, so this function only performs the
    per-unique-composite work (render / association / dialog) once.
    Returns ``None`` if the stack could not be normalized to a configured
    asset folder or rendering failed.
    """
    digest = digest or _record_key(kind, name, normalized)[2]

    target, family, _sub, status, family2 = _determine_family_and_path(
        normalized, kind, name, config, digest
    )

    dimensions = _render_or_reuse_png(normalized, target, compositor, dry_run)
    if dimensions is None and not dry_run:
        return None

    record = _build_base_record(
        name,
        kind,
        normalized,
        locations,
        block,
        config,
        target,
        family,
        status,
        dimensions,
    )
    usage_locations = record.usage_locations

    if family2 == "char":
        record = _enrich_char_record(record, normalized, associator)
    elif family2 == "scene":
        record = _enrich_scene_record(
            record,
            normalized,
            usage_locations,
            block,
            index,
            associator,
            config,
        )

    return record

"""Manifest read/write for the compose pass.

Responsibility: serialise a :class:`~converter.compose.models.CompositeManifest`
to JSON, write per-category manifest files and an index, and load manifests back
for the apply pass.  No compositing or script parsing happens here.

Layout (under the configured ``report_dir``):

- ``manifest_bg.json``   — composites in the ``bg`` family
- ``manifest_char.json`` — composites in the ``char`` family
- ``manifest_scene.json`` — composites in the ``scene`` family
- ``manifest_gui.json``  — composites in the ``gui`` family
- ``manifest_other.json`` — composites that don't fit a known family
- ``characters.json`` — canonical character names, generated folders, and aliases
- ``manifest_index.json`` — global data (summary, dynamic_states, image_declarations,
  dynamic_declarations, unresolved, CFG walk state) plus references to each
  category file.

There is no single monolithic ``manifest.json``.  The bundle is published with
atomic replacements, and the index stores relative category references that all
consumers resolve.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from cli import print_summary

from ...utility import atomic_write_json, atomic_write_json_files
from ...utility.manifest_paths import MANIFEST_CATEGORIES, ManifestPathError
from ...utility.manifest_paths import (
    resolve_category_manifest_paths as _resolve_category_manifest_paths,
)
from ..association import CharacterAssociator, CharacterDeclaration
from ..config import GameConfig
from ..errors import ManifestError
from ..models import CompositeManifest, CompositeRecord

KNOWN_FAMILIES = MANIFEST_CATEGORIES

# Canonical character catalog — source of truth for character names and their
# speaker aliases. Seeded during compose from Ren'Py Character() declarations;
# used by the interactive review task.
CHARACTERS_FILE = "characters.json"


def _relative_manifest_reference(target: Path, manifest_index_path: Path) -> str:
    """Return a category path relative to the manifest index directory."""
    try:
        relative = os.path.relpath(
            target.resolve(),
            manifest_index_path.resolve().parent,
        )
    except ValueError as exc:
        raise ManifestError(
            f"Manifest category path is on a different filesystem: {target}"
        ) from exc
    return Path(relative).as_posix()


def resolve_category_manifest_paths(
    index_payload: dict,
    manifest_index_path: Path,
    report_dir: Path | None = None,
) -> dict[str, Path]:
    """Resolve category manifests from the references stored in an index.

    Relative references are resolved against the index directory.  Absolute
    references remain readable for compatibility with older manifests.  A
    missing reference key falls back to the canonical category filename in
    *report_dir*, while an explicitly referenced missing file is an error.
    """
    try:
        return _resolve_category_manifest_paths(
            index_payload,
            manifest_index_path,
            report_dir,
        )
    except ManifestPathError as exc:
        raise ManifestError(str(exc)) from exc


def _collect_unmapped_speakers(
    manifest: CompositeManifest,
) -> dict[str, list[str]]:
    """Collect scene speakers that could not be mapped to a character folder."""
    unmapped: dict[str, list[str]] = {}
    for record in manifest.composites_list:
        if record.family != "scene":
            continue
        for speaker in record.unmapped_speakers or ():
            unmapped.setdefault(speaker, []).append(record.composite_path)
    return unmapped


def _load_characters(reports_dir: Path) -> dict[str, dict]:
    """Load character -> {folder, aliases} from reports/characters.json."""
    characters_path = reports_dir / CHARACTERS_FILE
    if not characters_path.is_file():
        return {}

    try:
        characters = json.loads(characters_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(characters, dict):
        return {}
    return characters


def _save_characters(reports_dir: Path, characters: dict[str, dict]) -> None:
    """Persist character -> {folder, aliases} map to characters.json."""
    characters_path = reports_dir / CHARACTERS_FILE
    atomic_write_json(characters_path, characters)


def _generated_character_folders(
    manifest: CompositeManifest | None,
) -> dict[str, str]:
    """Return composite names mapped to their generated character folders."""
    if manifest is None:
        return {}

    folders_by_name: dict[str, set[str]] = {}
    for record in manifest.composites_list:
        if record.family != "char" or record.status != "static":
            continue

        parts = Path(record.composite_path).parts
        try:
            static_index = next(
                index for index, part in enumerate(parts) if part.lower() == "static"
            )
            char_index = next(
                index
                for index, part in enumerate(parts)
                if index > static_index and part.lower() == "char"
            )
        except StopIteration:
            continue

        # A character folder must have at least one path component after it;
        # otherwise the next component is the composite filename itself.
        if char_index + 2 >= len(parts):
            continue

        folder = parts[char_index + 1]
        folders_by_name.setdefault(record.name.lower(), set()).add(folder)

    return {name: min(folders) for name, folders in folders_by_name.items() if folders}


def _generated_character_folder(
    config: GameConfig,
    name: str,
    declaration: CharacterDeclaration,
    generated_folders: dict[str, str],
) -> str:
    """Return the generated character-composite path relative to the game root."""
    output_name = generated_folders.get(name.lower())
    if output_name is None:
        for image_tag in sorted(declaration.image_tags):
            output_name = generated_folders.get(image_tag.lower())
            if output_name is not None:
                break
    if output_name is None:
        output_name = name

    output_path = (
        config.resolved_paths()["output_dir"] / "static" / "char" / output_name
    )
    try:
        return output_path.relative_to(config.game_root).as_posix()
    except ValueError:
        return output_path.as_posix()


def _seed_characters(
    config: GameConfig,
    reports_dir: Path,
    manifest: CompositeManifest | None = None,
    extra: dict[str, CharacterDeclaration] | None = None,
    *,
    persist: bool = True,
) -> dict[str, dict]:
    """Build characters.json from Ren'Py Character() declarations.

    Existing aliases are preserved for characters that still have a matching
    declaration. Characters found only by scanning source directories are
    omitted, and every ``folder`` value points to the generated static
    character-composite directory.

    ``extra`` merges characters that only the native compose stage could
    discover (its runtime ``ADVCharacter`` registry); they are added after the
    statically-parsed declarations and never replace an existing entry.
    """
    declarations = dict(CharacterAssociator(config).character_declarations())
    for name, declaration in sorted((extra or {}).items()):
        if name not in declarations:
            declarations[name] = declaration

    existing = _load_characters(reports_dir)
    generated_folders = _generated_character_folders(manifest)

    characters: dict[str, dict] = {}
    for name, declaration in sorted(declarations.items()):
        previous = existing.get(name)
        info = dict(previous) if isinstance(previous, dict) else {}

        previous_aliases = info.get("aliases")
        aliases = (
            [str(alias) for alias in previous_aliases]
            if isinstance(previous_aliases, list)
            else []
        )
        aliases_lower = {alias.lower() for alias in aliases}

        for alias in sorted(declaration.aliases):
            if not alias or alias.lower() == name.lower():
                continue
            if alias.lower() not in aliases_lower:
                aliases.append(alias)
                aliases_lower.add(alias.lower())

        info["folder"] = _generated_character_folder(
            config,
            name,
            declaration,
            generated_folders,
        )
        info["aliases"] = sorted(aliases, key=str.lower)
        characters[name] = info

    if persist:
        _save_characters(reports_dir, characters)
    return characters


class ManifestWriter:
    """Writes and loads split category manifests for the compose pass."""

    def __init__(
        self,
        config: GameConfig,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> None:
        self.config = config
        paths = config.resolved_paths()
        if manifest_index_path is not None:
            self.manifest_index_path = Path(manifest_index_path)
            self.report_dir = (
                Path(report_dir)
                if report_dir is not None
                else self.manifest_index_path.parent
            )
        else:
            self.report_dir = (
                Path(report_dir) if report_dir is not None else paths["report_dir"]
            )
            self.manifest_index_path = (
                self.report_dir / "manifest_index.json"
                if report_dir is not None and config.manifest_index_path is None
                else paths["manifest_index_path"]
            )

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def write(
        self,
        manifest: CompositeManifest,
        dry_run: bool = False,
        extra_characters: dict[str, CharacterDeclaration] | None = None,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> CompositeManifest:
        """Write the split-manifest bundle as one atomic transaction.

        Each category gets its own file and global data lives in the manifest
        index.  The index is replaced last and acts as the transaction's commit
        marker.  ``extra_characters`` merges native-stage discoveries into the
        character catalog without overriding statically parsed declarations.
        The native compose stage is the intended sole caller; callers must not
        republish an already-published manifest.
        """
        if manifest_index_path is not None:
            index_path = Path(manifest_index_path)
            reports_dir = (
                Path(report_dir) if report_dir is not None else index_path.parent
            )
        else:
            index_path = self.manifest_index_path
            reports_dir = (
                Path(report_dir) if report_dir is not None else self.report_dir
            )
        self.manifest_index_path = index_path
        self.report_dir = reports_dir

        bundle_paths = [
            reports_dir / f"manifest_{category}.json"
            for category in KNOWN_FAMILIES
        ]
        bundle_paths.extend(
            [
                reports_dir / CHARACTERS_FILE,
                reports_dir / "review_pending.json",
            ]
        )
        resolved_index = index_path.resolve()
        if any(path.resolve() == resolved_index for path in bundle_paths):
            raise ManifestError(
                f"Manifest index path collides with a bundle file: {index_path}"
            )

        known_families = set(KNOWN_FAMILIES)
        categories: dict[str, list] = {name: [] for name in KNOWN_FAMILIES}
        for record in manifest.composites_list:
            family = record.family
            if family in known_families:
                categories[family].append(record)
            else:
                categories.setdefault("other", []).append(record)

        if dry_run:
            scene_records = [
                record for record in manifest.composites_list if record.family == "scene"
            ]
            missing = [
                record.composite_path for record in scene_records if not record.characters
            ]
            pending_scenes = missing
            unmapped_speakers = _collect_unmapped_speakers(manifest)
            association_summary = {
                "images": len(scene_records),
                "associated": len(scene_records) - len(missing),
                "missing": len(missing),
                "missing_examples": missing[:25],
            }
        else:
            reports_dir.mkdir(parents=True, exist_ok=True)
            index_path.parent.mkdir(parents=True, exist_ok=True)

            transaction: dict[Path, dict] = {}
            category_files: dict[str, str] = {}
            for category_name in KNOWN_FAMILIES:
                category_records = categories.get(category_name, [])
                category_manifest = {
                    "category": category_name,
                    "composites": [record.to_dict() for record in category_records],
                    "summary": {"composites": len(category_records)},
                }
                category_path = reports_dir / f"manifest_{category_name}.json"
                transaction[category_path] = category_manifest
                category_files[category_name] = _relative_manifest_reference(
                    category_path, index_path
                )

            characters = _seed_characters(
                self.config,
                reports_dir,
                manifest,
                extra_characters,
                persist=False,
            )
            transaction[reports_dir / CHARACTERS_FILE] = characters

            scene_without_chars = [
                {
                    "composite_path": record.composite_path,
                    "name": record.name,
                    "definition": {
                        "script": record.definition.script,
                        "line": record.definition.line,
                    },
                    "usage_locations": [
                        {
                            "script": location.script,
                            "line": location.line,
                            "statement": location.statement,
                        }
                        for location in record.usage_locations
                    ],
                }
                for record in manifest.composites_list
                if record.family == "scene" and not record.characters
            ]
            all_unmapped = _collect_unmapped_speakers(manifest)
            transaction[reports_dir / "review_pending.json"] = {
                "pending_reviews": len(scene_without_chars),
                "scenes": scene_without_chars,
                "unmapped_speakers": [
                    {"speaker": speaker, "composites": sorted(composites)}
                    for speaker, composites in sorted(all_unmapped.items())
                ],
            }

            index_manifest = {
                "categories": category_files,
                "summary": manifest.summary,
                "dynamic_states": manifest.dynamic_states,
                "image_declarations": manifest.image_declarations,
                "dynamic_declarations": manifest.dynamic_declarations,
                "unresolved": list(manifest.unresolved),
                "cfg_processed": manifest.cfg_processed,
                "cfg_truncated": manifest.cfg_truncated,
            }
            # The index is intentionally the final replacement in the bundle.
            transaction[index_path] = index_manifest
            atomic_write_json_files(transaction)

            pending_scenes = cast(
                list[str],
                [scene["composite_path"] for scene in scene_without_chars],
            )
            unmapped_speakers = dict(all_unmapped)
            association_summary = cast(
                dict, manifest.summary.get("scene_character_associations", {})
            )

        _print_summary(manifest, association_summary, pending_scenes, unmapped_speakers)
        return manifest

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    @classmethod
    def has_composite_manifest(
        cls,
        config: GameConfig,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> bool:
        """Return whether the compose pass has produced a manifest index."""
        writer = cls(
            config,
            manifest_index_path=manifest_index_path,
            report_dir=report_dir,
        )
        return writer.manifest_index_path.is_file()

    @classmethod
    def load_all(
        cls,
        config: GameConfig,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> CompositeManifest:
        """Load the index and referenced category manifests into one manifest."""
        writer = cls(
            config,
            manifest_index_path=manifest_index_path,
            report_dir=report_dir,
        )
        index_path = writer.manifest_index_path
        if not index_path.is_file():
            raise ManifestError(f"Manifest index not found: {index_path}")

        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"Could not read manifest index {index_path}: {exc}"
            ) from exc
        category_paths = resolve_category_manifest_paths(
            index,
            index_path,
            writer.report_dir,
        )

        all_composites: list[dict] = []
        for category_path in category_paths.values():
            try:
                category_data = json.loads(category_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ManifestError(
                    f"Could not read manifest category file {category_path}: {exc}"
                ) from exc
            all_composites.extend(category_data.get("composites", []))

        return CompositeManifest(
            composites=tuple(CompositeRecord.from_dict(c) for c in all_composites),
            dynamic_states=index.get("dynamic_states", []),
            image_declarations=index.get("image_declarations", []),
            dynamic_declarations=index.get("dynamic_declarations", []),
            unresolved=tuple(index.get("unresolved", [])),
            summary=index.get("summary", {}),
            cfg_processed=int(index.get("cfg_processed", 0)),
            cfg_truncated=bool(index.get("cfg_truncated", False)),
        )

    @classmethod
    def load_pending_review(
        cls,
        config: GameConfig,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> dict:
        """Load the pending scene review report, if any."""
        writer = cls(
            config,
            manifest_index_path=manifest_index_path,
            report_dir=report_dir,
        )
        pending_path = writer.report_dir / "review_pending.json"
        if not pending_path.is_file():
            return {}
        try:
            return json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"Could not read pending review file {pending_path}: {exc}"
            ) from exc


def _print_summary(
    manifest: CompositeManifest,
    association_summary: object | None = None,
    pending_scenes: list[str] | None = None,
    unmapped_speakers: dict[str, list[str]] | None = None,
) -> None:
    print_summary(
        "Compose summary",
        [
            ("Scripts scanned", str(manifest.summary["scripts_scanned"])),
            ("Composites", str(manifest.summary["composites"])),
            ("Uses processed", str(manifest.summary["uses_processed"])),
            ("Unresolved scenes", str(len(pending_scenes or []))),
            (
                "Unmapped speakers",
                str(
                    len(unmapped_speakers) if isinstance(unmapped_speakers, dict) else 0
                ),
            ),
        ],
    )

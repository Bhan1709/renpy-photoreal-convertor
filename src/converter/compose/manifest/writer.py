"""Manifest read/write for the compose pass.

Responsibility: serialise a :class:`~converter.compose.models.CompositeManifest`
to JSON, write per-category manifest files and an index, and load manifests back
for the apply pass.  No compositing or script parsing happens here.

Layout (under ``<output_dir>/reports/``):

- ``manifest_bg.json``   — composites in the ``bg`` family
- ``manifest_char.json`` — composites in the ``char`` family
- ``manifest_scene.json`` — composites in the ``scene`` family
- ``manifest_gui.json``  — composites in the ``gui`` family
- ``manifest_other.json`` — composites that don't fit a known family
- ``manifest_index.json`` — global data (summary, dynamic_states, image_declarations,
  dynamic_declarations, unresolved) plus references to each category file.

There is no single monolithic ``manifest.json``.  The apply pass loads the index
and merges all category manifests in memory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from cli import print_summary

from ..association import CharacterAssociator
from ..config import GameConfig
from ..errors import ManifestError
from ..models import CompositeManifest, CompositeRecord

KNOWN_FAMILIES = ("bg", "char", "scene", "gui", "other")

# Character alias file — source of truth for character names and their speaker
# aliases.  Seeded during compose from chars/ folders and Character() declarations;
# used by the interactive review task.
CHARACTER_ALIASES_FILE = "character_aliases.json"


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


def _load_character_aliases(reports_dir: Path) -> dict[str, dict]:
    """Load character -> {folder, aliases} from reports/character_aliases.json."""
    alias_path = reports_dir / CHARACTER_ALIASES_FILE
    if alias_path.is_file():
        try:
            return json.loads(alias_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return {}
    return {}


def _save_character_aliases(reports_dir: Path, char_aliases: dict[str, dict]) -> None:
    """Persist character -> {folder, aliases} map."""
    alias_path = reports_dir / CHARACTER_ALIASES_FILE
    alias_path.write_text(json.dumps(char_aliases, indent=2), encoding="utf-8")


def _seed_character_aliases(config: GameConfig, reports_dir: Path) -> dict[str, dict]:
    """Build character_aliases.json from chars/ folders and Character()
    declarations.

    If the file already exists, it is loaded and new aliases from
    Character() declarations are merged in (manual edits are preserved).
    If the file does not exist, it is created from scratch.

    The ``folder`` field stores a forward-slash path relative to
    ``game_folder`` (e.g. ``archive/chars/hazel``) so downstream processes
    can locate assets without reconstructing the path from categories.
    """
    alias_path = reports_dir / CHARACTER_ALIASES_FILE

    associator = CharacterAssociator(config)
    alias_map = associator._aliases()  # normalized_speaker -> set of char folders
    folders = set(associator.known_char_folders())

    # Build relative path from game_folder to the char asset directory.
    char_category_folder = config.folder_for("char")
    rel_char_dir = Path("archive") / char_category_folder

    # Start with existing data (if any) so manual edits are preserved.
    if alias_path.is_file():
        char_aliases = _load_character_aliases(reports_dir)
    else:
        char_aliases = {}

    # Ensure every folder has an entry.
    for folder in sorted(folders):
        if folder not in char_aliases:
            char_aliases[folder] = {
                "folder": (rel_char_dir / folder).as_posix(),
                "aliases": [],
            }
        else:
            info = char_aliases[folder]
            if "folder" not in info:
                info["folder"] = (rel_char_dir / folder).as_posix()
            if "aliases" not in info:
                info["aliases"] = []

    # Ensure narrator entry. Narrator has no corresponding char folder, so it
    # carries only an aliases list (e.g. "context").
    if "narrator" not in char_aliases:
        char_aliases["narrator"] = {"aliases": []}
    # "context" is a common narrator-style speaker alias in Ren'Py dialogs.
    narrator_aliases = char_aliases["narrator"].setdefault("aliases", [])
    if "context" not in {a.lower() for a in narrator_aliases}:
        narrator_aliases.append("context")

    # Merge aliases from Character() declarations (append, don't overwrite).
    # Skip self-references (folder maps to itself in the alias map).
    for speaker_key, char_folders in alias_map.items():
        for char_folder in char_folders:
            if char_folder not in char_aliases:
                continue
            if speaker_key.lower() == char_folder.lower():
                continue
            existing = char_aliases[char_folder].setdefault("aliases", [])
            if speaker_key not in {a.lower() for a in existing}:
                existing.append(speaker_key)

    # Sort aliases for readability.
    for info in char_aliases.values():
        info["aliases"] = sorted(info["aliases"], key=str.lower)

    _save_character_aliases(reports_dir, char_aliases)
    return char_aliases


class ManifestWriter:
    """Writes and loads split category manifests for the compose pass."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def write(
        self,
        manifest: CompositeManifest,
        dry_run: bool = False,
    ) -> CompositeManifest:
        """Write per-category manifests + index to disk (unless ``dry_run``).

        Does *not* write a monolithic manifest.json.  Each category gets its
        own file; global data lives in manifest_index.json.
        """
        paths = self.config.resolved_paths()
        reports_dir = paths["manifest_path"].parent

        known_families = set(KNOWN_FAMILIES)
        categories: dict[str, list] = {name: [] for name in KNOWN_FAMILIES}
        for record in manifest.composites_list:
            family = record.family
            if family in known_families:
                categories[family].append(record)
            else:
                categories.setdefault("other", []).append(record)

        if dry_run:
            scene_records = [r for r in manifest.composites_list if r.family == "scene"]
            missing = [r.composite_path for r in scene_records if not r.characters]
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

            category_files: dict[str, str] = {}
            for cat_name in KNOWN_FAMILIES:
                cat_records = categories.get(cat_name, [])
                cat_manifest = {
                    "category": cat_name,
                    "composites": [r.to_dict() for r in cat_records],
                    "summary": {"composites": len(cat_records)},
                }
                cat_path = reports_dir / f"manifest_{cat_name}.json"
                cat_path.write_text(
                    json.dumps(cat_manifest, indent=2), encoding="utf-8"
                )
                category_files[cat_name] = str(cat_path)

            # Index carries the global (non-composite) data plus file references.
            index_manifest = {
                "categories": category_files,
                "summary": manifest.summary,
                "dynamic_states": manifest.dynamic_states,
                "image_declarations": manifest.image_declarations,
                "dynamic_declarations": manifest.dynamic_declarations,
                "unresolved": list(manifest.unresolved),
            }
            index_path = reports_dir / "manifest_index.json"
            index_path.write_text(
                json.dumps(index_manifest, indent=2), encoding="utf-8"
            )

            # Build pending review: scenes without character association, plus
            # all unmapped speakers found across scene composites (speakers in
            # dialog that could not be mapped to any char folder).
            # Seed character_aliases.json (source of truth for character names
            # and speaker aliases) before generating the pending review.
            _seed_character_aliases(self.config, reports_dir)

            scene_without_chars = [
                {
                    "composite_path": r.composite_path,
                    "name": r.name,
                    "definition": {
                        "script": r.definition.script,
                        "line": r.definition.line,
                    },
                    "usage_locations": [
                        {
                            "script": loc.script,
                            "line": loc.line,
                            "statement": loc.statement,
                        }
                        for loc in r.usage_locations
                    ],
                }
                for r in manifest.composites_list
                if r.family == "scene" and not r.characters
            ]
            all_unmapped = _collect_unmapped_speakers(manifest)
            pending_scenes = cast(
                list[str], [s["composite_path"] for s in scene_without_chars]
            )
            unmapped_speakers = dict(all_unmapped)
            pending_path = reports_dir / "review_pending.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "pending_reviews": len(scene_without_chars),
                        "scenes": scene_without_chars,
                        "unmapped_speakers": [
                            {"speaker": speaker, "composites": sorted(composites)}
                            for speaker, composites in sorted(all_unmapped.items())
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            association_summary = cast(
                dict, manifest.summary.get("scene_character_associations", {})
            )

        _print_summary(manifest, association_summary, pending_scenes, unmapped_speakers)
        return manifest

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    @classmethod
    def has_composite_manifest(cls, config: GameConfig) -> bool:
        """Return whether the compose pass has produced a manifest index."""
        paths = config.resolved_paths()
        index_path = paths["manifest_path"].parent / "manifest_index.json"
        return index_path.is_file()

    @classmethod
    def load_all(cls, config: GameConfig) -> CompositeManifest:
        """Load the index and all category manifests, merge into one manifest.

        Used by the apply pass.  Reads ``manifest_index.json`` for global data
        and each ``manifest_<cat>.json`` for the composites in that category.
        """
        paths = config.resolved_paths()
        reports_dir = paths["manifest_path"].parent
        index_path = reports_dir / "manifest_index.json"

        if not index_path.is_file():
            raise ManifestError(f"Manifest index not found: {index_path}")

        index = json.loads(index_path.read_text(encoding="utf-8"))

        all_composites: list[dict] = []
        for cat_name in KNOWN_FAMILIES:
            cat_path = reports_dir / f"manifest_{cat_name}.json"
            if not cat_path.is_file():
                continue
            cat_data = json.loads(cat_path.read_text(encoding="utf-8"))
            all_composites.extend(cat_data.get("composites", []))

        return CompositeManifest(
            composites=tuple(CompositeRecord.from_dict(c) for c in all_composites),
            dynamic_states=index.get("dynamic_states", []),
            image_declarations=index.get("image_declarations", []),
            dynamic_declarations=index.get("dynamic_declarations", []),
            unresolved=tuple(index.get("unresolved", [])),
            summary=index.get("summary", {}),
        )

    @classmethod
    def load_pending_review(cls, config: GameConfig) -> dict:
        """Load the pending scene review report, if any.

        Returns the full pending review dict with ``scenes`` and
        ``unmapped_speakers`` keys.  Empty dict when no pending reviews exist
        or the file is absent.
        """
        paths = config.resolved_paths()
        reports_dir = paths["manifest_path"].parent
        pending_path = reports_dir / "review_pending.json"
        if not pending_path.is_file():
            return {}
        return json.loads(pending_path.read_text(encoding="utf-8"))


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

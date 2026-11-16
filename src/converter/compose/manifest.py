"""Manifest read/write for the compose pass.

Responsibility: serialise a :class:`~converter.compose.models.CompositeManifest`
to JSON, write per-category manifest files and an index, and load manifests back
for the apply pass.  No compositing or script parsing happens here.

Layout (under ``<output_dir>/reports/``):

- ``manifest_bg.json``   — composites in the ``bg`` family
- ``manifest_char.json`` — composites in the ``char`` family
- ``manifest_scene.json`` — composites in the ``scene`` family
- ``manifest_gui.json``  — composites in the ``gui`` family
- ``manifest_xxx.json``  — composites in the ``xxx`` (scene/adult) family
- ``manifest_other.json`` — composites that don't fit a known family
- ``manifest_index.json`` — global data (summary, dynamic_states, image_declarations,
  dynamic_declarations, unresolved) plus references to each category file.

There is no single monolithic ``manifest.json``.  The apply pass loads the index
and merges all category manifests in memory.
"""

from __future__ import annotations

import json
from typing import cast

from .config import GameConfig
from .errors import ManifestError
from .models import CompositeManifest, CompositeRecord

KNOWN_FAMILIES = ("bg", "char", "scene", "gui", "xxx", "other")


class ManifestWriter:
    """Writes and loads split category manifests for the compose pass."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def write(
        self, manifest: CompositeManifest, dry_run: bool = False,
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
            xxx_records = [r for r in manifest.composites_list if r.family == "xxx"]
            missing = [
                r.composite_path
                for r in xxx_records
                if not r.characters
            ]
            association_summary = {
                "images": len(xxx_records),
                "associated": len(xxx_records) - len(missing),
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
                cat_path.write_text(json.dumps(cat_manifest, indent=2), encoding="utf-8")
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
            index_path.write_text(json.dumps(index_manifest, indent=2), encoding="utf-8")

            association_summary = cast(
                dict, manifest.summary.get("xxx_character_associations", {})
            )

        _print_summary(manifest, association_summary)
        return manifest

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

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
            composites=tuple(
                CompositeRecord.from_dict(c) for c in all_composites
            ),
            dynamic_states=index.get("dynamic_states", []),
            image_declarations=index.get("image_declarations", []),
            dynamic_declarations=index.get("dynamic_declarations", []),
            unresolved=tuple(index.get("unresolved", [])),
            summary=index.get("summary", {}),
        )


def _print_summary(manifest: CompositeManifest, association_summary: object | None = None) -> None:
    print(json.dumps({
        "scripts_scanned": manifest.summary["scripts_scanned"],
        "composites": manifest.summary["composites"],
        "uses_processed": manifest.summary["uses_processed"],
        "unresolved": sorted(manifest.unresolved),
        "xxx_character_associations": association_summary if isinstance(association_summary, dict) else {},
    }, indent=2))

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

from .config import GameConfig
from .errors import ManifestError
from .models import CompositeManifest

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
                    "composites": [_record_to_dict(r) for r in cat_records],
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

            association_summary = manifest.summary.get("xxx_character_associations", {})

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
                CompositeManifest._record_from_dict(c) for c in all_composites
            ),
            dynamic_states=index.get("dynamic_states", []),
            image_declarations=index.get("image_declarations", []),
            dynamic_declarations=index.get("dynamic_declarations", []),
            unresolved=tuple(index.get("unresolved", [])),
            summary=index.get("summary", {}),
        )


def _record_to_dict(record) -> dict:
    """Serialise a single CompositeRecord to a JSON-friendly dict."""
    d = {
        "composite_path": record.composite_path,
        "name": record.name,
        "kind": record.kind,
        "status": record.status,
        "definition": {"script": record.definition.script, "line": record.definition.line},
        "usage_locations": [
            {
                "script": loc.script,
                "line": loc.line,
                "statement": loc.statement,
                **({"state_snapshot": loc.state_snapshot} if loc.state_snapshot else {}),
            }
            for loc in record.usage_locations
        ],
        "ordered_layers": [
            {
                "source": l.source,
                "line": l.line,
                "group": l.group,
                "conditions": list(l.conditions),
                "attribute": l.attribute,
                "default_attribute": l.default_attribute,
                "offset": list(l.offset) if l.offset else None,
                "offset_rel": list(l.offset_rel) if l.offset_rel else None,
                "chain_id": l.chain_id,
                "resolved_path": str(l.resolved_path) if l.resolved_path else None,
                "resolution_method": l.resolution_method,
                "warning": l.warning,
                "unresolved_conditions": list(l.unresolved_conditions),
                "via_image_tag": l.via_image_tag,
                "role": l.role,
                "canvas_size": list(l.canvas_size) if l.canvas_size else None,
                "declaration_expression": l.declaration_expression,
            }
            for l in record.ordered_layers
        ],
        "dimensions": list(record.dimensions) if record.dimensions else None,
        "dynamic_conditions": list(record.dynamic_conditions),
        "resolved_conditions": list(record.resolved_conditions),
        "family": record.family,
    }
    if record.characters is not None:
        d["characters"] = list(record.characters)
    if record.character_name is not None:
        d["character_name"] = record.character_name
    if record.character_association_status is not None:
        d["character_association_status"] = record.character_association_status
    if record.dialog_context is not None:
        d["dialog_context"] = list(record.dialog_context)
    return d


def _print_summary(manifest: CompositeManifest, association_summary: object | None = None) -> None:
    print(json.dumps({
        "scripts_scanned": manifest.summary["scripts_scanned"],
        "composites": manifest.summary["composites"],
        "uses_processed": manifest.summary["uses_processed"],
        "unresolved": sorted(manifest.unresolved),
        "xxx_character_associations": association_summary if isinstance(association_summary, dict) else {},
    }, indent=2))

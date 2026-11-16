"""Domain models for the compose subpackage.

Replaces the raw-dict records used in the original implementation with typed,
immutable dataclasses.  This makes the data flowing through the pipeline
explicit, self-documenting, and easier to validate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Layer:
    """A single layer in a composite stack.

    Attributes:
        source: Forward-slash asset path relative to ``archive_dir``.
        line: 1-based source line where the layer was declared.
        group: Optional layeredimage group name.
        conditions: Tuple of condition expressions guarding this layer.
        attribute: Optional attribute name (from ``attribute`` blocks).
        default_attribute: Whether this attribute is a default.
        offset: Absolute (x, y) offset in pixels, if specified.
        offset_rel: Relative (xalign, yalign) offset as fractions, if specified.
        chain_id: Identifier of the if/elif/else chain this layer belongs to.
        resolved_path: Absolute on-disk path resolved from ``source``.
        resolution_method: How the path was resolved (for diagnostics).
        warning: Non-fatal warning about this layer (e.g. size mismatch).
        unresolved_conditions: Conditions that could not be statically evaluated.
        via_image_tag: Name of the named image tag this layer came through.
        role: Role hint (e.g. "screen_add", "image_declaration_layer").
        canvas_size: Optional (width, height) canvas override.
        declaration_expression: Full expression string for image declarations.
    """

    source: str
    line: int
    group: str = ""
    conditions: tuple[str, ...] = field(default_factory=tuple)
    attribute: str | None = None
    default_attribute: bool = False
    offset: tuple[float, float] | None = None
    offset_rel: tuple[float, float] | None = None
    chain_id: str | None = None
    resolved_path: Path | None = None
    resolution_method: str | None = None
    warning: str | None = None
    unresolved_conditions: tuple[str, ...] = field(default_factory=tuple)
    via_image_tag: str | None = None
    role: str = ""
    canvas_size: tuple[int, int] | None = None
    declaration_expression: str | None = None

    def with_unresolved(self, conditions: tuple[str, ...]) -> Layer:
        """Return a copy with additional unresolved conditions."""
        merged = tuple(sorted(set(self.unresolved_conditions) | set(conditions)))
        return field_replace(self, unresolved_conditions=merged)

    def replace(self, **changes) -> Layer:
        """Return a copy with the given fields replaced."""
        return field_replace(self, **changes)


def field_replace(obj, **changes):
    """Helper to replace fields on a frozen dataclass instance."""
    return obj.__class__(**{**obj.__dict__, **changes})


@dataclass(frozen=True)
class DefinitionLocation:
    """Where a composite was defined in the source scripts."""

    script: str  # relative to game_root parent
    line: int


@dataclass(frozen=True)
class UsageLocation:
    """Where a composite is used (show/scene statement) in live scripts."""

    script: str  # relative to game_root parent
    line: int
    statement: str
    state_snapshot: dict[str, object] | None = None


@dataclass(frozen=True)
class CompositeRecord:
    """A single composite: one output PNG plus provenance metadata."""

    composite_path: str  # relative to game_root parent
    name: str
    kind: str  # "layeredimage" | "screen" | "image_declaration"
    status: str  # "static" | "dynamic_review"
    definition: DefinitionLocation
    usage_locations: tuple[UsageLocation, ...]
    ordered_layers: tuple[Layer, ...]
    dimensions: tuple[int, int] | None
    dynamic_conditions: tuple[str, ...]
    resolved_conditions: tuple[str, ...]
    family: str
    characters: tuple[dict, ...] | None = None
    character_name: str | None = None
    character_association_status: str | None = None
    dialog_context: tuple[dict, ...] | None = None

    def with_usage(self, location: UsageLocation) -> CompositeRecord:
        """Return a copy with an additional usage location appended."""
        locations = tuple(sorted(set(self.usage_locations) | {location}, key=lambda l: (l.script, l.line)))
        return field_replace(self, usage_locations=locations)

    def replace(self, **changes) -> CompositeRecord:
        """Return a copy with the given fields replaced."""
        return field_replace(self, **changes)


@dataclass(frozen=True)
class CompositeManifest:
    """The full manifest produced by the compose pass."""

    composites: tuple[CompositeRecord, ...]
    dynamic_states: list[dict]
    image_declarations: list[dict]
    dynamic_declarations: list[dict]
    unresolved: tuple[str, ...]
    summary: dict[str, object]

    @property
    def composites_list(self) -> list[CompositeRecord]:
        return list(self.composites)

    @classmethod
    def from_dict(cls, data: dict) -> CompositeManifest:
        """Build a manifest from a JSON-loaded dict (used by the apply pass)."""
        composites = tuple(
            CompositeRecord(
                composite_path=r["composite_path"],
                name=r["name"],
                kind=r["kind"],
                status=r["status"],
                definition=DefinitionLocation(
                    script=r["definition"]["script"],
                    line=r["definition"]["line"],
                ),
                usage_locations=tuple(
                    UsageLocation(
                        script=loc["script"],
                        line=loc["line"],
                        statement=loc["statement"],
                        state_snapshot=loc.get("state_snapshot"),
                    )
                    for loc in r.get("usage_locations", [])
                ),
                ordered_layers=tuple(
                    Layer(
                        source=l["source"],
                        line=l["line"],
                        group=l.get("group", ""),
                        conditions=tuple(l.get("conditions", [])),
                        attribute=l.get("attribute"),
                        default_attribute=l.get("default_attribute", False),
                        offset=tuple(l["offset"]) if l.get("offset") else None,
                        offset_rel=tuple(l["offset_rel"]) if l.get("offset_rel") else None,
                        chain_id=l.get("chain_id"),
                        resolved_path=Path(l["resolved_path"]) if l.get("resolved_path") else None,
                        resolution_method=l.get("resolution_method"),
                        warning=l.get("warning"),
                        unresolved_conditions=tuple(l.get("unresolved_conditions", [])),
                        via_image_tag=l.get("via_image_tag"),
                        role=l.get("role", ""),
                        canvas_size=tuple(l["canvas_size"]) if l.get("canvas_size") else None,
                        declaration_expression=l.get("declaration_expression"),
                    )
                    for l in r.get("ordered_layers", [])
                ),
                dimensions=tuple(r["dimensions"]) if r.get("dimensions") else None,
                dynamic_conditions=tuple(r.get("dynamic_conditions", [])),
                resolved_conditions=tuple(r.get("resolved_conditions", [])),
                family=r.get("family", r.get("kind", "")),
                characters=tuple(r.get("characters", [])) if r.get("characters") else None,
                character_name=r.get("character_name"),
                character_association_status=r.get("character_association_status"),
                dialog_context=tuple(r.get("dialog_context", [])) if r.get("dialog_context") else None,
            )
            for r in data.get("composites", [])
        )
        return cls(
            composites=composites,
            dynamic_states=data.get("dynamic_states", []),
            image_declarations=data.get("image_declarations", []),
            dynamic_declarations=data.get("dynamic_declarations", []),
            unresolved=tuple(data.get("unresolved", [])),
            summary=data.get("summary", {}),
        )

    @staticmethod
    def _record_from_dict(data: dict) -> CompositeRecord:
        """Build a single CompositeRecord from a JSON-loaded dict."""
        return CompositeRecord(
            composite_path=data["composite_path"],
            name=data["name"],
            kind=data["kind"],
            status=data["status"],
            definition=DefinitionLocation(
                script=data["definition"]["script"],
                line=data["definition"]["line"],
            ),
            usage_locations=tuple(
                UsageLocation(
                    script=loc["script"],
                    line=loc["line"],
                    statement=loc["statement"],
                    state_snapshot=loc.get("state_snapshot"),
                )
                for loc in data.get("usage_locations", [])
            ),
            ordered_layers=tuple(
                Layer(
                    source=l["source"],
                    line=l["line"],
                    group=l.get("group", ""),
                    conditions=tuple(l.get("conditions", [])),
                    attribute=l.get("attribute"),
                    default_attribute=l.get("default_attribute", False),
                    offset=tuple(l["offset"]) if l.get("offset") else None,
                    offset_rel=tuple(l["offset_rel"]) if l.get("offset_rel") else None,
                    chain_id=l.get("chain_id"),
                    resolved_path=Path(l["resolved_path"]) if l.get("resolved_path") else None,
                    resolution_method=l.get("resolution_method"),
                    warning=l.get("warning"),
                    unresolved_conditions=tuple(l.get("unresolved_conditions", [])),
                    via_image_tag=l.get("via_image_tag"),
                    role=l.get("role", ""),
                    canvas_size=tuple(l["canvas_size"]) if l.get("canvas_size") else None,
                    declaration_expression=l.get("declaration_expression"),
                )
                for l in data.get("ordered_layers", [])
            ),
            dimensions=tuple(data["dimensions"]) if data.get("dimensions") else None,
            dynamic_conditions=tuple(data.get("dynamic_conditions", [])),
            resolved_conditions=tuple(data.get("resolved_conditions", [])),
            family=data.get("family", data.get("kind", "")),
            characters=tuple(data.get("characters", [])) if data.get("characters") else None,
            character_name=data.get("character_name"),
            character_association_status=data.get("character_association_status"),
            dialog_context=tuple(data.get("dialog_context", [])) if data.get("dialog_context") else None,
        )

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "composites": [
                {
                    "composite_path": r.composite_path,
                    "name": r.name,
                    "kind": r.kind,
                    "status": r.status,
                    "definition": {
                        "script": r.definition.script,
                        "line": r.definition.line,
                    },
                    "usage_locations": [
                        {
                            "script": loc.script,
                            "line": loc.line,
                            "statement": loc.statement,
                            **({"state_snapshot": loc.state_snapshot} if loc.state_snapshot else {}),
                        }
                        for loc in r.usage_locations
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
                        for l in r.ordered_layers
                    ],
                    "dimensions": list(r.dimensions) if r.dimensions else None,
                    "dynamic_conditions": list(r.dynamic_conditions),
                    "resolved_conditions": list(r.resolved_conditions),
                    "family": r.family,
                    **(
                        {"characters": list(r.characters)} if r.characters else {}
                    ),
                    **(
                        {"character_name": r.character_name} if r.character_name else {}
                    ),
                    **(
                        {"character_association_status": r.character_association_status}
                        if r.character_association_status
                        else {}
                    ),
                    **(
                        {"dialog_context": list(r.dialog_context)} if r.dialog_context else {}
                    ),
                }
                for r in self.composites
            ],
            "dynamic_states": self.dynamic_states,
            "image_declarations": self.image_declarations,
            "dynamic_declarations": self.dynamic_declarations,
            "unresolved": list(self.unresolved),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class AdoptionReport:
    """Report produced by the apply pass."""

    applied: int
    unchanged: int
    skipped_runtime_state: int
    skipped: list[dict]
    declared_generated_composites: int
    runtime_paths_replaced: int
    runtime_switch_uses: int
    runtime_switch_declarations: int
    runtime_switch_updates_this_run: int
    missing_generated_files: list[str]

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "unchanged": self.unchanged,
            "skipped_runtime_state": self.skipped_runtime_state,
            "skipped": self.skipped,
            "declared_generated_composites": self.declared_generated_composites,
            "runtime_paths_replaced": self.runtime_paths_replaced,
            "runtime_switch_uses": self.runtime_switch_uses,
            "runtime_switch_declarations": self.runtime_switch_declarations,
            "runtime_switch_updates_this_run": self.runtime_switch_updates_this_run,
            "missing_generated_files": self.missing_generated_files,
        }


__all__ = [
    "AdoptionReport",
    "CompositeManifest",
    "CompositeRecord",
    "DefinitionLocation",
    "Layer",
    "UsageLocation",
    "field_replace",
]

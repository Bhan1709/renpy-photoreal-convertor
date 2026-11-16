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

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "source": self.source,
            "line": self.line,
            "group": self.group,
            "conditions": list(self.conditions),
            "attribute": self.attribute,
            "default_attribute": self.default_attribute,
            "offset": list(self.offset) if self.offset else None,
            "offset_rel": list(self.offset_rel) if self.offset_rel else None,
            "chain_id": self.chain_id,
            "resolved_path": str(self.resolved_path) if self.resolved_path else None,
            "resolution_method": self.resolution_method,
            "warning": self.warning,
            "unresolved_conditions": list(self.unresolved_conditions),
            "via_image_tag": self.via_image_tag,
            "role": self.role,
            "canvas_size": list(self.canvas_size) if self.canvas_size else None,
            "declaration_expression": self.declaration_expression,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Layer:
        """Build a :class:`Layer` from a JSON-loaded dict."""
        return cls(
            source=data["source"],
            line=data["line"],
            group=data.get("group", ""),
            conditions=tuple(data.get("conditions", [])),
            attribute=data.get("attribute"),
            default_attribute=data.get("default_attribute", False),
            offset=tuple(data["offset"]) if data.get("offset") else None,
            offset_rel=tuple(data["offset_rel"]) if data.get("offset_rel") else None,
            chain_id=data.get("chain_id"),
            resolved_path=Path(data["resolved_path"]) if data.get("resolved_path") else None,
            resolution_method=data.get("resolution_method"),
            warning=data.get("warning"),
            unresolved_conditions=tuple(data.get("unresolved_conditions", [])),
            via_image_tag=data.get("via_image_tag"),
            role=data.get("role", ""),
            canvas_size=tuple(data["canvas_size"]) if data.get("canvas_size") else None,
            declaration_expression=data.get("declaration_expression"),
        )


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

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        d: dict = {
            "composite_path": self.composite_path,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "definition": {"script": self.definition.script, "line": self.definition.line},
            "usage_locations": [
                {
                    "script": loc.script,
                    "line": loc.line,
                    "statement": loc.statement,
                    **({"state_snapshot": loc.state_snapshot} if loc.state_snapshot else {}),
                }
                for loc in self.usage_locations
            ],
            "ordered_layers": [l.to_dict() for l in self.ordered_layers],
            "dimensions": list(self.dimensions) if self.dimensions else None,
            "dynamic_conditions": list(self.dynamic_conditions),
            "resolved_conditions": list(self.resolved_conditions),
            "family": self.family,
        }
        if self.characters is not None:
            d["characters"] = list(self.characters)
        if self.character_name is not None:
            d["character_name"] = self.character_name
        if self.character_association_status is not None:
            d["character_association_status"] = self.character_association_status
        if self.dialog_context is not None:
            d["dialog_context"] = list(self.dialog_context)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> CompositeRecord:
        """Build a :class:`CompositeRecord` from a JSON-loaded dict."""
        return cls(
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
            ordered_layers=tuple(Layer.from_dict(l) for l in data.get("ordered_layers", [])),
            dimensions=tuple(data["dimensions"]) if data.get("dimensions") else None,
            dynamic_conditions=tuple(data.get("dynamic_conditions", [])),
            resolved_conditions=tuple(data.get("resolved_conditions", [])),
            family=data.get("family", data.get("kind", "")),
            characters=tuple(data.get("characters", [])) if data.get("characters") else None,
            character_name=data.get("character_name"),
            character_association_status=data.get("character_association_status"),
            dialog_context=tuple(data.get("dialog_context", [])) if data.get("dialog_context") else None,
        )


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
            CompositeRecord.from_dict(r) for r in data.get("composites", [])
        )
        return cls(
            composites=composites,
            dynamic_states=data.get("dynamic_states", []),
            image_declarations=data.get("image_declarations", []),
            dynamic_declarations=data.get("dynamic_declarations", []),
            unresolved=tuple(data.get("unresolved", [])),
            summary=data.get("summary", {}),
        )

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "composites": [r.to_dict() for r in self.composites],
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

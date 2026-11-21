"""Shared path resolution for split compose-manifest bundles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MANIFEST_CATEGORIES: tuple[str, ...] = ("bg", "char", "scene", "gui", "other")


class ManifestPathError(ValueError):
    """Raised when a manifest index contains an invalid category reference."""


def _reference_path(reference: Any, category: str) -> Path:
    """Return one validated category reference as a path."""
    if isinstance(reference, Mapping):
        reference = reference.get("path")
    if not isinstance(reference, str) or not reference.strip():
        raise ManifestPathError(
            f"Manifest category reference for {category!r} must be a non-empty path"
        )
    return Path(reference)


def resolve_category_manifest_paths(
    index_payload: Mapping[str, object],
    manifest_index_path: Path,
    report_dir: Path | None = None,
    *,
    categories: Sequence[str] = MANIFEST_CATEGORIES,
) -> dict[str, Path]:
    """Resolve category manifests referenced by a split-manifest index.

    Relative references are resolved against the manifest index directory.
    Absolute references remain supported for compatibility with older
    manifests.  When a known category has no explicit reference, the
    canonical filename is looked up in *report_dir*; this keeps consumers
    consistent when the index and report directory are configured separately.
    """
    if not isinstance(index_payload, Mapping):
        raise ManifestPathError("Manifest index payload must be an object")

    raw_categories = index_payload.get("categories", {})
    if not isinstance(raw_categories, Mapping):
        raise ManifestPathError("Manifest index 'categories' must be an object")

    index_path = Path(manifest_index_path)
    reference_base = index_path.parent
    fallback_dir = Path(report_dir) if report_dir is not None else reference_base

    resolved: dict[str, Path] = {}
    for category in categories:
        if category in raw_categories:
            category_path = _reference_path(raw_categories[category], category)
            if not category_path.is_absolute():
                category_path = reference_base / category_path
            if not category_path.is_file():
                raise ManifestPathError(
                    f"Manifest category file not found: {category_path}"
                )
        else:
            category_path = fallback_dir / f"manifest_{category}.json"
            if not category_path.is_file():
                continue
        resolved[category] = category_path

    return resolved


__all__ = [
    "MANIFEST_CATEGORIES",
    "ManifestPathError",
    "resolve_category_manifest_paths",
]

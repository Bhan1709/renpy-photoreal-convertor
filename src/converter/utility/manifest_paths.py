"""Shared path resolution for split compose-manifest bundles."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

MANIFEST_CATEGORIES: tuple[str, ...] = ("bg", "char", "scene", "gui", "other")
MANIFEST_BUNDLE_VERSION = 2


class ManifestPathError(ValueError):
    """Raised when a manifest index contains an invalid category reference."""


class ManifestIntegrityError(ManifestPathError):
    """Raised when a referenced category file does not match its digest."""


def _reference_path(reference: Any, category: str) -> Path:
    """Return one validated category reference as a path."""
    if isinstance(reference, Mapping):
        reference = reference.get("path")
    if not isinstance(reference, str) or not reference.strip():
        raise ManifestPathError(
            f"Manifest category reference for {category!r} must be a non-empty path"
        )
    path = Path(reference)
    if not path.is_absolute() and not PureWindowsPath(reference).drive:
        # A relative reference is a portable path, not an OS-native filename.
        # Normalize Windows separators so an index remains readable on POSIX.
        path = Path(reference.replace("\\", "/"))
    return path


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestIntegrityError(
            f"Could not read manifest category file {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def verify_category_manifest_digests(
    index_payload: Mapping[str, object],
    category_paths: Mapping[str, Path],
    *,
    report_dir: str | Path | None = None,
    manifest_index_path: str | Path | None = None,
    categories: Sequence[str] = MANIFEST_CATEGORIES,
) -> None:
    """Verify category files against the digests stored in an index.

    Older unversioned split manifests did not contain digest metadata, so an
    absent ``category_digests`` field is accepted for backwards compatibility.
    Versioned bundles require the digest map.  Once the field is present,
    every known category must have a valid digest and its file must still
    exist.  Checking the complete category set (rather
    than only the files that happen to be present) prevents a deleted
    category from being silently treated as an empty category.
    """
    bundle_version = index_payload.get("bundle_version")
    if bundle_version is not None and (
        type(bundle_version) is not int
        or bundle_version != MANIFEST_BUNDLE_VERSION
    ):
        raise ManifestIntegrityError(
            "Manifest index has an unsupported bundle_version: "
            f"{bundle_version!r}"
        )
    if "category_digests" not in index_payload:
        if bundle_version == MANIFEST_BUNDLE_VERSION:
            raise ManifestIntegrityError(
                "Manifest index is missing required 'category_digests' metadata"
            )
        # An unversioned index is a legacy bundle.  Digest metadata was not
        # available in that format, so it remains readable for compatibility.
        return
    raw_digests = index_payload["category_digests"]
    if not isinstance(raw_digests, Mapping):
        raise ManifestIntegrityError(
            "Manifest index 'category_digests' must be an object"
        )

    fallback_dir: Path | None = None
    if report_dir is not None:
        fallback_dir = Path(report_dir)
    elif manifest_index_path is not None:
        fallback_dir = Path(manifest_index_path).parent

    for category in categories:
        expected = raw_digests.get(category)
        if expected is None:
            raise ManifestIntegrityError(
                f"Manifest category digest is missing for {category!r}"
            )
        if not isinstance(expected, str) or not expected:
            raise ManifestIntegrityError(
                f"Manifest category digest for {category!r} must be a non-empty string"
            )
        expected = expected.lower().removeprefix("sha256:")
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ManifestIntegrityError(
                f"Manifest category digest for {category!r} must be a SHA-256 digest"
            )

        category_path = category_paths.get(category)
        if category_path is None:
            if fallback_dir is None:
                raise ManifestIntegrityError(
                    f"Manifest category file is missing for {category!r}; "
                    "integrity cannot be verified"
                )
            category_path = fallback_dir / f"manifest_{category}.json"
        if not category_path.is_file():
            raise ManifestIntegrityError(
                f"Manifest category file failed integrity check for {category!r}: "
                f"{category_path} (file is missing)"
            )

        actual = _file_sha256(category_path)
        if actual != expected:
            raise ManifestIntegrityError(
                f"Manifest category file failed integrity check for {category!r}: "
                f"{category_path} (expected {expected}, got {actual})"
            )


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
    resolved_targets: dict[Path, str] = {}
    index_target = Path(
        os.path.abspath(os.path.normpath(os.fspath(index_path)))
    )
    for category in categories:
        if category in raw_categories:
            category_path = _reference_path(raw_categories[category], category)
            if not category_path.is_absolute():
                category_path = reference_base / category_path
            category_target = Path(
                os.path.abspath(os.path.normpath(os.fspath(category_path)))
            )
            if category_target == index_target:
                raise ManifestPathError(
                    f"Manifest category {category!r} references the manifest index"
                )
            if not category_path.is_file():
                raise ManifestPathError(
                    f"Manifest category file not found: {category_path}"
                )
        else:
            category_path = fallback_dir / f"manifest_{category}.json"
            category_target = Path(
                os.path.abspath(os.path.normpath(os.fspath(category_path)))
            )
            if category_target == index_target:
                raise ManifestPathError(
                    f"Manifest category {category!r} references the manifest index"
                )
            if not category_path.is_file():
                continue
        previous = resolved_targets.get(category_target)
        if previous is not None:
            raise ManifestPathError(
                f"Manifest categories {previous!r} and {category!r} reference "
                "the same file"
            )
        resolved_targets[category_target] = category
        resolved[category] = category_path

    return resolved


__all__ = [
    "MANIFEST_BUNDLE_VERSION",
    "MANIFEST_CATEGORIES",
    "ManifestIntegrityError",
    "ManifestPathError",
    "resolve_category_manifest_paths",
    "verify_category_manifest_digests",
]

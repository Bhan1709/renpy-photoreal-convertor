"""Shared utility modules for the converter subpackages.

Provides tqdm-based progress reporting reused across subpackages
(decompile, compose, …) and atomic JSON publication helpers.
"""

from __future__ import annotations

from tqdm.rich import tqdm  # type: ignore[import-untyped]

from .atomic import AtomicWriteError, atomic_write_json, atomic_write_json_files
from .manifest_paths import (
    MANIFEST_CATEGORIES,
    ManifestPathError,
    resolve_category_manifest_paths,
)

__all__ = [
    "MANIFEST_CATEGORIES",
    "AtomicWriteError",
    "ManifestPathError",
    "atomic_write_json",
    "atomic_write_json_files",
    "resolve_category_manifest_paths",
    "tqdm",
]

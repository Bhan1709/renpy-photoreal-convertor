"""Shared utility modules for the converter subpackages.

Provides tqdm-based progress reporting reused across subpackages
(decompile, compose, …), atomic publication helpers, manifest-bundle
locking/recovery, and shared live-path containment validation.
"""

from __future__ import annotations

from tqdm.rich import tqdm  # type: ignore[import-untyped]

from .atomic import (
    AtomicWriteError,
    atomic_copy_file,
    atomic_write_json,
    atomic_write_json_files,
    json_payload_digest,
    recover_transaction_journal,
)
from .manifest_bundle import (
    BUNDLE_JOURNAL_NAME,
    BUNDLE_OWNER_REGISTRY_NAME,
    ManifestBundlePathError,
    bundle_journal_metadata,
    bundle_journal_path,
    bundle_lock_path,
    bundle_owner_registry_path,
    bundle_paths,
    bundle_support_paths,
    category_digest_map,
    game_script_lock,
    game_script_lock_path,
    manifest_bundle_lock,
    recover_manifest_bundle,
    register_bundle_owner,
    validate_manifest_index_path,
)
from .manifest_paths import (
    MANIFEST_BUNDLE_VERSION,
    MANIFEST_CATEGORIES,
    ManifestIntegrityError,
    ManifestPathError,
    resolve_category_manifest_paths,
    verify_category_manifest_digests,
)
from .safe_paths import (
    UnsafePathError,
    game_script_relative_path,
    resolve_absolute_under_root,
    resolve_game_output_path,
    resolve_game_script_path,
    resolve_under_root,
    safe_relative_path,
)

__all__ = [
    "BUNDLE_JOURNAL_NAME",
    "BUNDLE_OWNER_REGISTRY_NAME",
    "MANIFEST_BUNDLE_VERSION",
    "MANIFEST_CATEGORIES",
    "AtomicWriteError",
    "ManifestBundlePathError",
    "ManifestIntegrityError",
    "ManifestPathError",
    "UnsafePathError",
    "atomic_copy_file",
    "atomic_write_json",
    "atomic_write_json_files",
    "bundle_journal_metadata",
    "bundle_journal_path",
    "bundle_lock_path",
    "bundle_owner_registry_path",
    "bundle_paths",
    "bundle_support_paths",
    "category_digest_map",
    "game_script_lock",
    "game_script_lock_path",
    "game_script_relative_path",
    "json_payload_digest",
    "manifest_bundle_lock",
    "recover_manifest_bundle",
    "recover_transaction_journal",
    "register_bundle_owner",
    "resolve_absolute_under_root",
    "resolve_category_manifest_paths",
    "resolve_game_output_path",
    "resolve_game_script_path",
    "resolve_under_root",
    "safe_relative_path",
    "tqdm",
    "validate_manifest_index_path",
    "verify_category_manifest_digests",
]

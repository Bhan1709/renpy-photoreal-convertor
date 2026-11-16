"""Backwards-compatible re-export shim.

The restore logic has moved to :mod:`converter.restore`.  This module keeps
``from converter.compose.restore import ...`` working for existing callers and
the README examples.
"""

from __future__ import annotations

from converter.restore import (  # noqa: F401  (re-exported for backwards compat)
    RestoreError,
    find_archive,
    restore_from_manifest,
    restore_rpy_files,
)

__all__ = ["RestoreError", "find_archive", "restore_from_manifest", "restore_rpy_files"]

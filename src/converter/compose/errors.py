"""Custom exceptions for the compose subpackage."""

from __future__ import annotations

from typing import Optional


class ComposeError(Exception):
    """Base exception for all compose-related errors."""


class ConfigError(ComposeError):
    """Raised when a GameConfig is invalid or incomplete."""


class AssetResolutionError(ComposeError):
    """Raised when an asset path cannot be resolved under the archive directory."""


class CompositingError(ComposeError):
    """Raised when image compositing fails."""


class ManifestError(ComposeError):
    """Raised when a manifest cannot be read or written."""


class ApplyError(ComposeError):
    """Raised when the apply pass cannot adopt a composite."""


__all__ = [
    "ComposeError",
    "ConfigError",
    "AssetResolutionError",
    "CompositingError",
    "ManifestError",
    "ApplyError",
]

"""Custom exceptions for the compose subpackage."""

from __future__ import annotations


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


class NativeStageError(ComposeError):
    """Base error raised by the native Ren'Py compose stage."""


class NativeDiscoveryError(NativeStageError):
    """Raised when a compatible Ren'Py runtime cannot be discovered."""


class NativeExecutionError(NativeStageError):
    """Raised when the native Ren'Py subprocess fails or times out."""


class NativeProtocolError(NativeStageError):
    """Raised when the native Ren'Py result violates the stage protocol."""


class NativeLimitError(NativeStageError):
    """Raised when a native stage safety limit cannot be satisfied."""


__all__ = [
    "ApplyError",
    "AssetResolutionError",
    "ComposeError",
    "CompositingError",
    "ConfigError",
    "ManifestError",
    "NativeDiscoveryError",
    "NativeExecutionError",
    "NativeLimitError",
    "NativeProtocolError",
    "NativeStageError",
]

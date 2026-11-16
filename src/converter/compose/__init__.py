"""Compose subpackage: build reviewable composites from Ren'Py game assets.

Every Ren'Py game is treated as four canonical component categories:

- ``bg``      backgrounds
- ``char``    characters / sprites
- ``scene``   scene / CG assets
- ``gui``     user interface components

Each category maps to one (or more) asset folders under the game's extracted
archive directory.  The mapping is game-specific and is captured in
:class:`GameConfig`.

Public API
----------

Configuration::

    from converter.compose.config import GameConfig, load_config, default_config

Compose (manifest + PNGs)::

    from converter.compose.composer import Composer
    composer = Composer(config)
    manifest = composer.compose()

Apply (adopt into live scripts)::

    from converter.compose.applier import Applier
    applier = Applier(config)
    manifest, report = applier.apply()

Restore (revert modified .rpy files from archive)::

    from converter.compose.restore import restore_rpy_files, restore_from_manifest
    results = restore_rpy_files(config)  # all .rpy files
    results = restore_from_manifest(config)  # only files touched by apply

One-shot CLI::

    python -m converter.compose --game MyGame
    python -m converter.compose --game MyGame --restore
"""

from __future__ import annotations

from .applier import Applier
from .character_associator import CharacterAssociator
from .composer import Composer
from .compositor import Compositor, CPUCompositor
from .config import GameConfig, default_config, load_config
from .conditions import CallState
from .errors import (
    ApplyError,
    AssetResolutionError,
    CompositingError,
    ComposeError,
    ConfigError,
    ManifestError,
)
from .manifest import ManifestWriter
from .models import (
    AdoptionReport,
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
    Layer,
    UsageLocation,
)

from converter.restore import (
    RestoreError,
    find_archive,
    restore_from_manifest,
    restore_rpy_files,
)

__all__ = [
    "Applier",
    "CharacterAssociator",
    "Composer",
    "Compositor",
    "CPUCompositor",
    "GameConfig",
    "default_config",
    "load_config",
    "ApplyError",
    "AssetResolutionError",
    "CompositingError",
    "ComposeError",
    "ConfigError",
    "ManifestError",
    "ManifestWriter",
    "RestoreError",
    "find_archive",
    "restore_rpy_files",
    "restore_from_manifest",
    "AdoptionReport",
    "CallState",
    "CompositeManifest",
    "CompositeRecord",
    "DefinitionLocation",
    "Layer",
    "UsageLocation",
]

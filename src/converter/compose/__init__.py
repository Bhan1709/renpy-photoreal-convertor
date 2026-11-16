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

    from converter.restore import restore_rpy_files, restore_from_manifest
    results = restore_rpy_files(config)  # all .rpy files
    results = restore_from_manifest(config)  # only files touched by apply

One-shot CLI::

    python -m converter.compose --game MyGame
    python -m converter.compose --game MyGame --restore
"""

from __future__ import annotations

from converter.restore import (
    RestoreError,
    find_archive,
    restore_from_manifest,
    restore_rpy_files,
)

from .applier import Applier
from .character_associator import CharacterAssociator
from .composer import Composer
from .compositor import Compositor, CPUCompositor
from .conditions import CallState
from .config import GameConfig, default_config, load_config
from .errors import (
    ApplyError,
    AssetResolutionError,
    ComposeError,
    CompositingError,
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

__all__ = [
    "AdoptionReport",
    "Applier",
    "ApplyError",
    "AssetResolutionError",
    "CPUCompositor",
    "CallState",
    "CharacterAssociator",
    "ComposeError",
    "Composer",
    "CompositeManifest",
    "CompositeRecord",
    "CompositingError",
    "Compositor",
    "ConfigError",
    "DefinitionLocation",
    "GameConfig",
    "Layer",
    "ManifestError",
    "ManifestWriter",
    "RestoreError",
    "UsageLocation",
    "default_config",
    "find_archive",
    "load_config",
    "restore_from_manifest",
    "restore_rpy_files",
]

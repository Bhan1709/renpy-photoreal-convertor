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

    from converter.compose.config import GameConfig, load_config

Compose (manifest + PNGs)::

    from converter.compose.compose_pass import Composer
    composer = Composer(config)
    manifest = composer.compose()

Apply (adopt into live scripts)::

    from converter.compose.apply_pass import Applier
    applier = Applier(config)
    manifest, report = applier.apply()

Restore (revert modified .rpy files from archive)::

    from converter.restore import restore_rpy_files, restore_from_manifest
    results = restore_rpy_files(config)  # all .rpy files
    results = restore_from_manifest(config)  # only files touched by apply

Interactive CLI (exclusion check, then action menu)::

    python -m converter.compose                  # exclusion check -> menu
    python -m converter.compose clean            # remove previous compose output
    python -m converter.compose compose          # compose only (no apply)
    python -m converter.compose clean-compose    # clean output, then compose
    python -m converter.compose review           # review scenes + speakers
    python -m converter.compose apply            # adopt composites into scripts
"""

from __future__ import annotations

__all__: list[str] = []

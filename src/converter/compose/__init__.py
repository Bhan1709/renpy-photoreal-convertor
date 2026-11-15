"""Compose subpackage: build reviewable composites from Ren'Py game assets.

Every Ren'Py game is treated as four canonical component categories:

- ``bg``      backgrounds
- ``char``    characters / sprites
- ``scene``   scene / CG assets
- ``gui``     user interface components

Each category maps to one (or more) asset folders under the game's extracted
archive directory.  The mapping is game-specific and is captured in
:class:`GameConfig`.
"""

from .config import GameConfig, load_config, default_config

__all__ = ["GameConfig", "load_config", "default_config"]

"""Compose pass orchestration subpackage.

Compose v2 delegates extraction and rendering to the native Ren'Py stage
(:mod:`converter.compose.native.stage`).  There is no separate v1 static
pipeline; compose v2 is the only compose path.

Public API
----------

    from converter.compose.compose_pass import Composer
"""

from __future__ import annotations

from .orchestrate import Composer

__all__ = ["Composer"]

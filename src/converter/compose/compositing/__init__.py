"""Image compositing for the compose pass.

Public API
----------

    from converter.compose.compositing import Compositor, CPUCompositor
"""

from __future__ import annotations

from .cpu import Compositor, CPUCompositor

__all__ = ["CPUCompositor", "Compositor"]

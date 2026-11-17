"""Image compositing strategy interface for the compose pass.

Responsibility: define the strategy protocol for compositing a list of
:class:`~converter.compose.models.Layer` objects into a single RGBA image.
"""

from __future__ import annotations

from typing import Protocol

from PIL import Image

from ..models import Layer


class CompositorStrategy(Protocol):
    """Strategy interface for compositing layers into an image."""

    def composite(
        self,
        layers: list[Layer],
        canvas_size: tuple[int, int] | None = None,
    ) -> Image.Image | None:
        """Composite layers into an RGBA image.

        Returns ``None`` if no layers could be loaded.
        """
        ...

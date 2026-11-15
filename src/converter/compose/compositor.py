"""Image compositing for the compose pass.

Responsibility: composite a list of :class:`~converter.compose.models.Layer`
objects into a single RGBA image using PIL on the CPU.

No script parsing or file discovery happens here — only pixel composition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PIL import Image

from .config import GameConfig
from .errors import CompositingError
from .models import Layer
from .resolver import Resolver


class CompositorStrategy(Protocol):
    """Strategy interface for compositing layers into an image."""

    def composite(self, layers: list[Layer], canvas_size: tuple[int, int] | None = None) -> Image.Image | None:
        """Composite layers into an RGBA image.

        Returns ``None`` if no layers could be loaded.
        """
        ...


class CPUCompositor:
    """PIL-based CPU compositing."""

    def __init__(self, config: GameConfig, resolver: Resolver) -> None:
        self.config = config
        self.resolver = resolver

    def composite(
        self, layers: list[Layer], canvas_size: tuple[int, int] | None = None,
    ) -> Image.Image | None:
        opened: list[tuple[Image.Image, Layer]] = []
        size = canvas_size

        # Determine canvas size from the first layer that specifies one, or from the first loaded image.
        for candidate in layers:
            if candidate.canvas_size is not None:
                size = (candidate.canvas_size[0], candidate.canvas_size[1])
                break

        for layer in layers:
            path = self.resolver.resolve(layer.source)
            if path is None:
                continue
            resolved_layer = layer.replace(
                resolved_path=path,
                resolution_method=self._resolution_method(path, layer.source),
            )
            try:
                im = Image.open(path).convert("RGBA")
            except Exception:
                continue
            if size is None:
                size = im.size
            opened.append((im, resolved_layer))

        if not opened:
            return None

        assert size is not None, "canvas_size could not be determined"
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        for im, layer in opened:
            if im.size != size:
                layer = layer.replace(
                    warning=f"size {im.size} differs from canvas {size}; top-left composited"
                )
            offset = self._resolve_offset(layer, size)
            canvas.alpha_composite(im, (int(offset[0]), int(offset[1])))
        return canvas

    def _resolution_method(self, path: Path, source: str) -> str | None:
        try:
            archive_rel = path.relative_to(self.config.archive_dir).as_posix().lower()
        except ValueError:
            return None
        if archive_rel != source.lower():
            return "unique_folder_suffix_correlation"
        return None

    def _resolve_offset(self, layer: Layer, size: tuple[int, int]) -> tuple[int, int]:
        offset = layer.offset
        if offset is None:
            rel = layer.offset_rel
            if rel is not None:
                offset = (int(rel[0] * size[0]), int(rel[1] * size[1]))
            else:
                offset = (0, 0)
        return (int(offset[0]), int(offset[1]))


class Compositor:
    """High-level compositor that composites layers into an image (CPU via PIL)."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self.resolver = Resolver(config)
        self.strategy: CompositorStrategy = CPUCompositor(config, self.resolver)

    def composite(
        self, layers: list[Layer], canvas_size: tuple[int, int] | None = None,
    ) -> Image.Image | None:
        """Composite layers into an RGBA image using PIL on the CPU.

        Returns ``None`` if no layers could be loaded.
        """
        return self.strategy.composite(layers, canvas_size=canvas_size)

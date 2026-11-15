"""Image compositing for the compose pass.

Responsibility: composite a list of :class:`~converter.compose.models.Layer`
objects into a single RGBA image.  Supports PIL-based CPU compositing and
optional PyTorch CUDA GPU acceleration via a strategy pattern.

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
    """PIL-based CPU compositing (original implementation, always available)."""

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


class GPUCompositor:
    """PyTorch CUDA-accelerated compositing (requires torch + CUDA)."""

    def __init__(self, config: GameConfig, resolver: Resolver) -> None:
        self.config = config
        self.resolver = resolver

    def composite(
        self, layers: list[Layer], canvas_size: tuple[int, int] | None = None,
    ) -> Image.Image | None:
        try:
            import torch  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise CompositingError(
                "GPU compositing requested but PyTorch is not installed"
            ) from exc

        if not torch.cuda.is_available():
            raise CompositingError("CUDA-capable GPU not available for GPU compositing")

        device = torch.device("cuda")

        opened: list[tuple[torch.Tensor, Layer]] = []
        size = canvas_size

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
            arr = (
                torch.from_numpy(np.asarray(im).astype("float32") / 255.0)
                .permute(2, 0, 1)
                .to(device)
            )  # (H, W, C) -> (C, H, W)
            opened.append((arr, resolved_layer))

        if not opened:
            return None

        assert size is not None, "canvas_size could not be determined"
        canvas = torch.zeros(4, size[1], size[0], device=device, dtype=torch.float32)
        for arr, layer in opened:
            if arr.shape[1:] != (size[1], size[0]):
                layer = layer.replace(
                    warning=f"size {arr.shape[1:]} differs from canvas {size}; top-left composited"
                )
            offset = self._resolve_offset(layer, size)
            x0, y0 = int(offset[0]), int(offset[1])
            x1 = min(x0 + arr.shape[2], size[0])
            y1 = min(y0 + arr.shape[1], size[1])
            if x1 <= x0 or y1 <= y0:
                continue
            crop_w, crop_h = x1 - x0, y1 - y0
            src = arr[:, :crop_h, :crop_w]
            dst = canvas[:, y0:y1, x0:x1]
            src_a = src[3:4, :, :]
            out = src * src_a + dst * (1.0 - src_a)
            canvas[:, y0:y1, x0:x1] = out

        arr = (canvas.permute(1, 2, 0).cpu().numpy() * 255.0).clip(0, 255).astype("uint8")
        return Image.fromarray(arr, "RGBA")

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
    """High-level compositor that selects a strategy based on device preference."""

    def __init__(
        self, config: GameConfig, device: str = "auto",
    ) -> None:
        self.config = config
        self.resolver = Resolver(config)
        self.strategy: CompositorStrategy
        self.device = device
        self._select_strategy()

    def _select_strategy(self) -> None:
        use_gpu = False
        if self.device in ("cuda", "auto"):
            try:
                import torch  # type: ignore
                if torch.cuda.is_available():
                    use_gpu = True
            except ImportError:
                pass
        if use_gpu:
            self.strategy = GPUCompositor(self.config, self.resolver)
        else:
            self.strategy = CPUCompositor(self.config, self.resolver)

    def composite(
        self, layers: list[Layer], canvas_size: tuple[int, int] | None = None,
    ) -> Image.Image | None:
        """Composite layers into an RGBA image using the selected strategy.

        Returns ``None`` if no layers could be loaded.
        """
        return self.strategy.composite(layers, canvas_size=canvas_size)

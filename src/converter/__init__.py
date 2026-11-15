"""Converter subpackage: shared utilities for Ren'Py game conversion.

Subpackages (decompile, compose, …) can reuse the progress bar and other
helpers defined here.
"""

from .utility import ProgressBar, make_progress_bar, progress  # noqa: F401  (re-exported for subpackages)

__all__ = ["ProgressBar", "make_progress_bar", "progress"]


def main() -> None:
    print("Hello from renpy-photoreal-vn-converter!")

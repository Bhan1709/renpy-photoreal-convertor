"""Converter subpackage: shared utilities for Ren'Py game conversion.

Subpackages (decompile, compose, …) can reuse tqdm-based progress reporting
defined here.
"""

from .utility import tqdm  # noqa: F401  (re-exported for subpackages)

__all__ = ["tqdm"]


def main() -> None:
    print("Hello from renpy-photoreal-vn-converter!")

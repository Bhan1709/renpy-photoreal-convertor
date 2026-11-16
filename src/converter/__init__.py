"""Converter subpackage: shared utilities for Ren'Py game conversion.

Subpackages (decompile, compose, …) can reuse tqdm-based progress reporting
defined here.
"""

from .utility import tqdm

__all__ = ["tqdm"]


def main() -> None:
    print("Hello from renpy-photoreal-vn-converter!")

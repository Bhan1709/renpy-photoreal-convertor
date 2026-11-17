"""Converter subpackage: shared utilities for Ren'Py game conversion.

Subpackages (decompile, compose, …) can reuse tqdm-based progress reporting
defined here.

This module also provides the ``renpy-photoreal-vn-converter`` console script
entry point (``converter:main``).
"""

from __future__ import annotations

from cli import console, create_app, info, rule

from .utility import tqdm

__all__ = ["app", "main", "tqdm"]

app = create_app("Ren'Py Photoreal VN Converter")


@app.command(name="main")
def _main() -> None:
    """Show the converter welcome message and list available entry points."""
    rule("Ren'Py Photoreal VN Converter")
    console.print()
    info("Available entry points:")
    console.print(
        "  [green]python -m converter.pipeline[/green]        # Decompile -> compose pipeline"
    )
    console.print(
        "  [green]python -m converter.compose[/green]         # Compose composites (interactive menu)"
    )
    console.print(
        "  [green]python -m converter.compose.review[/green]  # Interactive composite review"
    )
    console.print(
        "  [green]python -m agent <profile>[/green]           # Launch OpenCode with a model profile"
    )
    console.print()
    info("Run any entry point with --help for details.")


def main() -> None:
    """Console script entry point for ``renpy-photoreal-vn-converter``."""
    app()


if __name__ == "__main__":
    app()

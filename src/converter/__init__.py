"""Converter subpackage: shared utilities for Ren'Py game conversion.

Subpackages (decompile, compose, …) can reuse tqdm-based progress reporting
defined here.

This module also provides the ``renpy-photoreal-vn-converter`` console script
entry point (``converter:main``).
"""

from __future__ import annotations

from cli import command, console, create_app, info, rule

from .utility import tqdm

__all__ = ["app", "main", "tqdm"]

app = create_app("Ren'Py Photoreal VN Converter")


@app.command(name="main")
def _main() -> None:
    """Show the converter welcome message and list available entry points."""
    rule("Ren'Py Photoreal VN Converter")
    console.print()
    info("Choose a task:")
    command(
        "mise run decompile",
        "Decompile scripts and extract game archives.",
    )
    command(
        "mise run compose",
        "Create, review, and apply photoreal composites.",
    )
    command(
        "python -m agent",
        "Launch OpenCode with an Ollama or NVIDIA model profile.",
    )
    command(
        "python -m converter.pipeline",
        "Run decompile and compose in one workflow.",
    )
    console.print()
    info("Add --help to any command to see its options.")


def main() -> None:
    """Console script entry point for ``renpy-photoreal-vn-converter``."""
    app()


if __name__ == "__main__":
    app()

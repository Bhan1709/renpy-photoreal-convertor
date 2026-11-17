"""Shared CLI utilities: rich console, typer app factory, questionary prompts.

Modern CLI design inspired by uv, ruff, and Cargo:

- ``rule()`` for major phase dividers (sleek horizontal line)
- ``step()`` / ``substep()`` with tree connectors (``+--``, ``|--``) for
  sub-steps under their parent step
- ``print_panel()`` reserved strictly for final summaries or errors
- Color palette: Cool Cyan (#38bdf8) headers, Emerald Green (#34d399) success,
  Slate Gray (dim) for paths and tree lines
"""

from __future__ import annotations

import questionary
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# --------------------------------------------------------------------------- #
# Color palette                                                               #
# --------------------------------------------------------------------------- #

CYAN = "#38bdf8"  # headers / numbers
GREEN = "#34d399"  # success
SLATE = "dim"  # paths / tree lines / muted text


# --------------------------------------------------------------------------- #
# Status messages                                                             #
# --------------------------------------------------------------------------- #


def success(message: str) -> None:
    """Print a success message (emerald green)."""
    console.print(f"[{GREEN}][OK][/{GREEN}] {message}")


def error(message: str) -> None:
    """Print an error message (red)."""
    console.print(f"[red][ERR][/red] {message}")


def warning(message: str) -> None:
    """Print a warning message (yellow)."""
    console.print(f"[yellow][WARN][/yellow] {message}")


def note(message: str) -> None:
    """Print a secondary informational note (dim slate)."""
    console.print(f"[{SLATE}][NOTE][/{SLATE}] {message}")


def info(message: str) -> None:
    """Print an informational message (blue)."""
    console.print(f"[blue][INFO][/blue] {message}")


# --------------------------------------------------------------------------- #
# Major phase divider                                                         #
# --------------------------------------------------------------------------- #


def rule(title: str) -> None:
    """Print a sleek horizontal rule for a major phase (like uv/ruff)."""
    console.rule(f"[{CYAN}]{title}[/{CYAN}]")


# --------------------------------------------------------------------------- #
# Step / substep helpers (tree connectors)                                    #
# --------------------------------------------------------------------------- #


def step(number: int | str, message: str) -> None:
    """Print a numbered top-level step with a tree connector."""
    console.print(f"[{CYAN}]+--[/{CYAN}] [{CYAN}]{number}[/{CYAN}] {message}")


def substep(message: str, last: bool = False) -> None:
    """Print a sub-step indented under its parent step.

    Uses ASCII tree connectors (``|--``, ``+--``) for terminal compatibility,
    matching the tree-layout style used by cargo and ruff.
    """
    connector = "+--" if last else "|--"
    console.print(f"    [{SLATE}]{connector}[/{SLATE}] {message}")


# --------------------------------------------------------------------------- #
# Key-value pairs                                                             #
# --------------------------------------------------------------------------- #


def kv(key: str, value: str) -> None:
    """Print a key-value pair: dim key, cyan value."""
    console.print(f"[{SLATE}]{key}:[/{SLATE}] [{CYAN}]{value}[/{CYAN}]")


# --------------------------------------------------------------------------- #
# Rich output helpers                                                         #
# --------------------------------------------------------------------------- #


def print_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Print a color-coded rich table."""
    table = Table(title=title, show_lines=True, header_style="bold magenta")
    for column in columns:
        table.add_column(column, style="cyan")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def print_panel(message: str, title: str = "", style: str = "blue") -> None:
    """Print a message inside a rich panel.

    Reserved strictly for final summaries or errors — intermediate steps
    should use :func:`rule`, :func:`step`, or :func:`substep`.
    """
    console.print(Panel(Text(message), title=title, border_style=style, expand=False))


# --------------------------------------------------------------------------- #
# Interactive prompts (questionary)                                           #
# --------------------------------------------------------------------------- #


def confirm(message: str, default: bool = False) -> bool | None:
    """Ask a yes/no question via questionary.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.confirm(message, default=default).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


def prompt_text(message: str, default: str = "") -> str | None:
    """Ask for free-text input via questionary.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.text(message, default=default).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


def select(message: str, choices: list[str], default: str | None = None) -> str | None:
    """Ask the user to pick one option from a list.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.select(message, choices=choices, default=default).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


# --------------------------------------------------------------------------- #
# Typer app factory                                                           #
# --------------------------------------------------------------------------- #


def create_app(help: str) -> typer.Typer:
    """Create a typer app with consistent project settings."""
    return typer.Typer(help=help)

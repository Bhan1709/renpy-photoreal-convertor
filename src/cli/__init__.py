"""Shared CLI utilities: rich console, typer app factory, questionary prompts.

Modern CLI design inspired by uv, ruff, and Cargo:

- ``rule()`` for major phase dividers (sleek horizontal line)
- ``step()`` / ``substep()`` with tree connectors (``+--``, ``|--``) for
  sub-steps under their parent step
- ``print_panel()`` reserved strictly for final summaries or errors
- Color palette: Cool Cyan (#38bdf8) headers, Emerald Green (#34d399) success,
  Slate Gray (dim) for paths and tree lines
- Interactive prompts use a custom questionary style matching the Rich
  palette, type-to-filter (autocomplete) with keyboard shortcuts, and
  :class:`questionary.Choice` / :class:`questionary.Separator` for grouped,
  metadata-rich menus.
"""

from __future__ import annotations

import sys

import questionary
import typer
from questionary import Choice, Separator  # noqa: F401  # re-exported for consumers
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
SLATE = "#888888"  # slate gray for questionary style (Rich uses "dim")


# --------------------------------------------------------------------------- #
# Custom questionary style matching the Rich color palette                    #
# --------------------------------------------------------------------------- #

CUSTOM_STYLE = questionary.Style(
    [
        ("qmark", f"fg:{CYAN} bold"),  # cyan question mark
        ("pointer", f"fg:{CYAN} bold"),  # cyan » pointer
        ("highlighted", f"fg:{CYAN} bold"),  # cyan highlighted choice
        ("selected", f"fg:{GREEN} bold"),  # emerald selected answer
        ("answer", f"fg:{GREEN} bold"),  # emerald answer text
        ("separator", f"fg:{SLATE}"),  # slate separator lines
        ("instruction", f"fg:{SLATE} italic"),  # slate instruction text
    ]
)


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
        return questionary.confirm(message, default=default, style=CUSTOM_STYLE).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


def prompt_text(message: str, default: str = "") -> str | None:
    """Ask for free-text input via questionary.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.text(message, default=default, style=CUSTOM_STYLE).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


def is_interactive() -> bool:
    """Return ``True`` when stdin and stdout are attached to a terminal.

    Interactive prompts are skipped (and info messages printed instead) when
    the CLI runs without a TTY, for example from a script or CI job.
    """
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def select(
    message: str,
    choices: list[str | Choice],
    default: str | None = None,
    use_shortcuts: bool = False,
) -> str | None:
    """Ask the user to pick one option from a list.

    Uses ``questionary.autocomplete`` for type-to-filter (fuzzy search) by
    default. Set ``use_shortcuts=True`` for short lists where single-key
    selection is preferred (uses ``questionary.select`` under the hood).

    Accepts plain strings, :class:`questionary.Choice` (with metadata title
    and clean value), and :class:`questionary.Separator` for grouping.

    When ``default`` is set, the matching choice is annotated with `` (current)``
    so the active choice is visually obvious.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    # Normalize choices: wrap plain strings in Choice, annotate default with (current).
    normalized: list[str | Choice] = []
    for choice in choices:
        if isinstance(
            choice, list
        ):  # questionary.Separator() returns a list of choice dicts
            normalized.append(choice)  # type: ignore[arg-type]
            continue
        if isinstance(choice, Choice):
            title = choice.title
            if default is not None and choice.value == default:
                title = f"{title}  (current)"
            normalized.append(Choice(title=title, value=choice.value))
        else:
            # choice is str
            title = choice  # type: ignore[assignment]
            if default is not None and choice == default:
                title = f"{title}  (current)"
            normalized.append(title)

    try:
        if use_shortcuts:
            return questionary.select(
                message,
                choices=normalized,
                default=default,
                use_shortcuts=True,
                style=CUSTOM_STYLE,
            ).ask()
        return questionary.autocomplete(  # type: ignore[arg-type]
            message,
            choices=normalized,  # type: ignore[arg-type]
            default=default,  # type: ignore[arg-type]
            style=CUSTOM_STYLE,
        ).ask()
    except KeyboardInterrupt, EOFError:
        console.print()
        return None


# --------------------------------------------------------------------------- #
# Typer app factory                                                           #
# --------------------------------------------------------------------------- #


def create_app(help: str) -> typer.Typer:
    """Create a typer app with consistent project settings."""
    return typer.Typer(help=help)

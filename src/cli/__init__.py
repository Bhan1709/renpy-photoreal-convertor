"""Shared CLI presentation and interaction utilities.

The project uses one consistent visual language across its Typer entry points:

- A Catppuccin Mocha Rich theme provides the shared color palette.
- ``rule()`` separates major phases so the workflow is easy to scan.
- ``step()`` and ``substep()`` show ordered work and its details.
- Status helpers use clear, color-coded labels for success, errors, warnings,
  notes, and information.
- ``loading()`` displays a transient Rich ``dots`` spinner for operations whose
  duration is unknown, while ``progress_bar()`` provides a themed
  ``tqdm.rich`` bar for measurable work.
- ``command()``, ``bullet()``, and ``code_block()`` format next steps and
  reference material without forcing each command to build its own markup.
- ``print_panel()`` and ``print_summary()`` box final summaries, grouped
  guidance, and important errors.
- Interactive prompts use a custom questionary style, keyboard shortcuts, and
  type-to-filter menus. Long selection lists use a compact seven-row viewport
  that rolls as the user navigates. Context can remain visible beside a
  selection through a two-pane workspace with Tab-based focus switching.

The helpers intentionally contain presentation only; command behavior and exit
codes remain the responsibility of each entry point.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

import questionary
import typer
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import AnyFormattedText, to_formatted_text
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Dimension,
    FormattedTextControl,
    HSplit,
    Layout,
    ScrollablePane,
    Window,
)
from prompt_toolkit.layout.menus import CompletionsMenuControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from questionary import Choice, Separator  # re-exported for consumers
from questionary.prompts.common import InquirerControl
from rich import box
from rich.console import Console, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from tqdm.rich import tqdm as tqdm_rich  # type: ignore[import-untyped]
from tqdm.std import TqdmExperimentalWarning  # type: ignore[import-untyped]

# --------------------------------------------------------------------------- #
# Catppuccin Mocha theme                                                     #
# --------------------------------------------------------------------------- #

CATPPUCCIN_MOCHA: dict[str, str] = {
    "rosewater": "#f5e0dc",
    "flamingo": "#f2cdcd",
    "pink": "#f5c2e7",
    "mauve": "#cba6f7",
    "red": "#f38ba8",
    "maroon": "#eba0ac",
    "peach": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "teal": "#94e2d5",
    "sky": "#89dceb",
    "sapphire": "#74c7ec",
    "blue": "#89b4fa",
    "lavender": "#b4befe",
    "text": "#cdd6f4",
    "subtext1": "#bac2de",
    "subtext0": "#a6adc8",
    "overlay2": "#9399b2",
    "overlay1": "#7f849c",
    "overlay0": "#6c7086",
    "surface2": "#585b70",
    "surface1": "#45475a",
    "surface0": "#313244",
    "base": "#1e1e2e",
    "mantle": "#181825",
    "crust": "#11111b",
}

CATPPUCCIN_THEME = Theme(
    {
        # Override Rich's stock colors so incidental Rich output also follows
        # the Catppuccin palette.
        "bold": f"bold {CATPPUCCIN_MOCHA['text']}",
        "dim": CATPPUCCIN_MOCHA["overlay1"],
        "italic": f"italic {CATPPUCCIN_MOCHA['text']}",
        "red": CATPPUCCIN_MOCHA["red"],
        "green": CATPPUCCIN_MOCHA["green"],
        "yellow": CATPPUCCIN_MOCHA["yellow"],
        "blue": CATPPUCCIN_MOCHA["blue"],
        "magenta": CATPPUCCIN_MOCHA["mauve"],
        "cyan": CATPPUCCIN_MOCHA["sky"],
        "bright_red": CATPPUCCIN_MOCHA["red"],
        "bright_green": CATPPUCCIN_MOCHA["green"],
        "bright_yellow": CATPPUCCIN_MOCHA["yellow"],
        "bright_blue": CATPPUCCIN_MOCHA["blue"],
        "bright_magenta": CATPPUCCIN_MOCHA["mauve"],
        "bright_cyan": CATPPUCCIN_MOCHA["sky"],
        "rule.line": CATPPUCCIN_MOCHA["surface1"],
        # Rich progress columns.
        "progress.description": f"bold {CATPPUCCIN_MOCHA['lavender']}",
        "progress.percentage": f"bold {CATPPUCCIN_MOCHA['blue']}",
        "progress.remaining": CATPPUCCIN_MOCHA["sky"],
        "progress.elapsed": CATPPUCCIN_MOCHA["yellow"],
        "progress.download": CATPPUCCIN_MOCHA["green"],
        "progress.filesize": CATPPUCCIN_MOCHA["green"],
        "progress.filesize.total": CATPPUCCIN_MOCHA["green"],
        "progress.data.speed": CATPPUCCIN_MOCHA["pink"],
        "progress.spinner": CATPPUCCIN_MOCHA["mauve"],
        "status.spinner": CATPPUCCIN_MOCHA["lavender"],
        "bar.back": CATPPUCCIN_MOCHA["surface0"],
        "bar.complete": CATPPUCCIN_MOCHA["blue"],
        "bar.finished": CATPPUCCIN_MOCHA["green"],
        "bar.pulse": CATPPUCCIN_MOCHA["mauve"],
    }
)

console = Console(theme=CATPPUCCIN_THEME)


# Semantic aliases used by the shared CLI helpers.
CYAN = CATPPUCCIN_MOCHA["sky"]  # headings, commands, and step numbers
GREEN = CATPPUCCIN_MOCHA["green"]  # successful completion
RED = CATPPUCCIN_MOCHA["red"]  # errors
YELLOW = CATPPUCCIN_MOCHA["yellow"]  # warnings
BLUE = CATPPUCCIN_MOCHA["blue"]  # informational messages
SLATE = CATPPUCCIN_MOCHA["overlay1"]  # secondary details and tree lines
MAUVE = CATPPUCCIN_MOCHA["mauve"]  # panels and highlighted headings
LAVENDER = CATPPUCCIN_MOCHA["lavender"]  # active progress and prompts

# Interactive lists use a compact viewport so long review data does not flood
# the terminal. Selection menus keep the active row visible while the
# surrounding rows roll.
MAX_VISIBLE_ROWS = 7

RICH_PROGRESS_COLUMNS = (
    "[progress.description]{task.description}",
    BarColumn(bar_width=20),
    "[progress.percentage]{task.percentage:>3.0f}%",
    MofNCompleteColumn(),
    TimeElapsedColumn(),
    TimeRemainingColumn(),
)


# --------------------------------------------------------------------------- #
# Custom questionary style matching the Rich color palette                    #
# --------------------------------------------------------------------------- #

CUSTOM_STYLE = questionary.Style(
    [
        ("qmark", f"fg:{CYAN} bold"),  # cyan question mark
        ("pointer", f"fg:{CYAN} bold"),  # cyan » pointer
        ("highlighted", f"fg:{CYAN} bold"),  # cyan highlighted choice
        ("selected", f"fg:{GREEN} bold"),  # green selected answer
        ("answer", f"fg:{GREEN} bold"),  # green answer text
        ("separator", f"fg:{SLATE}"),  # slate separator lines
        ("instruction", f"fg:{SLATE} italic"),  # slate instruction text
    ]
)

LIST_VIEW_STYLE = Style.from_dict(
    {
        "list-view.header": f"bold {CYAN}",
        "list-view.count": f"bold {LAVENDER}",
        "list-view.number": SLATE,
        "list-view.item": CYAN,
        "list-view.help": f"italic {SLATE}",
        "workspace.header": f"bold {SLATE}",
        "workspace.header.active": f"bold {CYAN}",
        "workspace.context-metadata-label": f"bold {CYAN}",
        "workspace.context-metadata-value": CATPPUCCIN_MOCHA["text"],
        "workspace.context-item": CYAN,
        "workspace.choice": CATPPUCCIN_MOCHA["text"],
        "workspace.choice.active": f"bold {CYAN}",
        "workspace.choice.inactive": SLATE,
        "workspace.separator": CATPPUCCIN_MOCHA["surface1"],
        "workspace.help": f"italic {SLATE}",
    }
)


# --------------------------------------------------------------------------- #
# Status messages                                                             #
# --------------------------------------------------------------------------- #


def _print_status(label: str, color: str, message: str) -> None:
    """Print a labeled status message with aligned continuation lines."""
    continuation = "\n" + " " * (len(label) + 1)
    console.print(f"[{color}]{label}[/{color}] {message.replace('\n', continuation)}")


def success(message: str) -> None:
    """Print a successful completion message."""
    _print_status("[OK]", GREEN, message)


def error(message: str) -> None:
    """Print an error message."""
    _print_status("[ERROR]", RED, message)


def warning(message: str) -> None:
    """Print a warning message."""
    _print_status("[WARNING]", YELLOW, message)


def note(message: str) -> None:
    """Print a secondary note for supporting context."""
    _print_status("[NOTE]", SLATE, message)


def info(message: str) -> None:
    """Print an informational message."""
    _print_status("[INFO]", BLUE, message)


@contextmanager
def loading(message: str) -> Iterator[Status]:
    """Show a transient Rich ``dots`` spinner while an operation is running."""
    with console.status(
        f"[{LAVENDER}]{message}[/{LAVENDER}]",
        spinner="dots",
        spinner_style=LAVENDER,
    ) as status:
        yield status


class _RepaintOnUpdateBar(tqdm_rich):  # type: ignore[misc,valid-type]
    """Repaint the Rich bar only when its data changes.

    tqdm_rich leaves Rich's background auto-refresh enabled, which repaints the
    bar even while nothing is happening. If another process writes to the same
    terminal (for example mise's interrupt notice), that repaint draws a second
    copy of the bar below the corrupted line. Refreshing explicitly on update
    keeps the bar on one line and stops repainting as soon as work stops.
    """

    def display(self, *args: Any, **kwargs: Any) -> None:
        super().display(*args, **kwargs)
        progress = getattr(self, "_prog", None)
        if progress is not None:
            progress.refresh()


def progress_bar(*args: Any, **kwargs: Any) -> tqdm_rich:
    """Create a compact ``tqdm.rich`` bar using the shared themed console."""
    options = dict(kwargs.pop("options", {}) or {})
    options.setdefault("console", console)
    options.setdefault("auto_refresh", False)
    kwargs.setdefault("progress", RICH_PROGRESS_COLUMNS)
    if kwargs.get("total") == 0:
        kwargs.setdefault("disable", True)
    with warnings.catch_warnings():
        # tqdm labels its Rich integration experimental even though this project
        # intentionally uses it. Suppress only that known warning.
        warnings.simplefilter("ignore", TqdmExperimentalWarning)
        return _RepaintOnUpdateBar(*args, options=options, **kwargs)


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """Return a count with a correctly pluralized noun."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


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
    """Print a numbered top-level step."""
    console.print(f"[{CYAN}]{number}.[/{CYAN}] {message}")


def substep(message: str, last: bool = False) -> None:
    """Print a detail indented beneath its parent step.

    ASCII tree connectors are used for compatibility with minimal terminals.
    Pass ``last=True`` for the final detail in a group.
    """
    connector = "+--" if last else "|--"
    console.print(f"    [{SLATE}]{connector}[/{SLATE}] {message}")


# --------------------------------------------------------------------------- #
# Key-value pairs                                                             #
# --------------------------------------------------------------------------- #


def kv(key: str, value: str) -> None:
    """Print an indented key-value pair with a dim key and highlighted value."""
    console.print(f"  [{SLATE}]{key}:[/{SLATE}] [{CYAN}]{value}[/{CYAN}]")


# --------------------------------------------------------------------------- #
# Action guidance                                                             #
# --------------------------------------------------------------------------- #


def command(command_text: str, description: str = "") -> None:
    """Print a command, optionally followed by a short explanation."""
    grid = Table.grid(expand=False, padding=(0, 2))
    grid.add_column(style=CYAN, overflow="fold")
    if description:
        grid.add_column(style=SLATE, overflow="fold")
        grid.add_row(command_text, description)
    else:
        grid.add_row(command_text)
    console.print(Padding(grid, (0, 2)))


def bullet(message: str, level: int = 0) -> None:
    """Print an indented bullet point with a hanging continuation line."""
    grid = Table.grid(expand=False, padding=(0, 1))
    grid.add_column(style=SLATE, no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("-", message)
    console.print(Padding(grid, (0, 2 * (level + 1))))


def code_block(code: str, language: str = "json", *, level: int = 0) -> None:
    """Print a syntax-highlighted code block at the requested indent level."""
    syntax = Syntax(
        code,
        language,
        theme="ansi_dark",
        background_color="default",
        word_wrap=True,
        padding=(0, 1),
    )
    console.print(Padding(syntax, (0, 2 * (level + 1))))


# --------------------------------------------------------------------------- #
# Rich output helpers                                                         #
# --------------------------------------------------------------------------- #


def print_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Print a clean, color-coded table."""
    table = Table(
        title=title,
        title_style=f"bold {CYAN}",
        header_style="bold",
        box=box.ROUNDED,
        show_lines=False,
        show_edge=False,
        pad_edge=False,
        padding=(0, 1),
    )
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    console.print(table)


def print_panel(message: RenderableType, title: str = "", style: str = BLUE) -> None:
    """Print a message or Rich renderable inside a themed panel.

    Reserved for final summaries, grouped guidance, or important errors —
    intermediate steps should use :func:`rule`, :func:`step`, or
    :func:`substep`.
    """
    content = Text(message) if isinstance(message, str) else message
    console.print(
        Panel(
            content,
            title=title,
            border_style=style,
            expand=False,
            padding=(1, 2),
        )
    )


def print_summary(
    title: str,
    rows: list[tuple[str, str]],
    *,
    style: str = MAUVE,
) -> None:
    """Print a compact key-value summary inside a themed panel."""
    grid = Table.grid(expand=False, padding=(0, 2))
    grid.add_column(style=SLATE, no_wrap=True)
    grid.add_column(style=CYAN, overflow="fold")
    for key, value in rows:
        grid.add_row(Text(key), Text(value))
    print_panel(grid, title=title, style=style)


# --------------------------------------------------------------------------- #
# Interactive prompts (questionary)                                           #
# --------------------------------------------------------------------------- #


def confirm(message: str, default: bool = False) -> bool | None:
    """Ask a yes/no question via questionary.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.confirm(message, default=default, style=CUSTOM_STYLE).ask()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return None


def prompt_text(message: str, default: str = "") -> str | None:
    """Ask for free-text input via questionary.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    try:
        return questionary.text(message, default=default, style=CUSTOM_STYLE).ask()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return None


def is_interactive() -> bool:
    """Return ``True`` when stdin and stdout are attached to a terminal.

    Interactive prompts are skipped (and info messages printed instead) when
    the CLI runs without a TTY, for example from a script or CI job.
    """
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def _truncate_middle(text: str, max_width: int) -> str:
    """Shorten text while retaining both its beginning and its useful tail."""
    if max_width < 1:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    marker = "..."
    tail_length = (max_width - len(marker) + 1) // 2
    head_length = max_width - len(marker) - tail_length
    return f"{text[:head_length]}{marker}{text[-tail_length:]}"


def _print_compact_list(items: Sequence[str], *, level: int = 0) -> None:
    """Print list items on one terminal row each."""
    available_width = max(20, console.width - (2 * (level + 1)) - 8)
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(style=SLATE, no_wrap=True)
    grid.add_column(style=CYAN, no_wrap=True, overflow="ellipsis")
    for item in items:
        grid.add_row("-", _truncate_middle(item, available_width))
    console.print(Padding(grid, (0, 2 * (level + 1))))


def _run_read_only_list_viewer(
    message: str,
    rows: Sequence[tuple[int, str]],
    *,
    max_visible_rows: int,
    level: int,
    input_source: Input | None = None,
    output_target: Output | None = None,
) -> bool:
    """Run a scrollable viewer whose entries cannot be selected."""
    indent = "  " * level
    number_width = len(str(len(rows)))

    def body_text() -> AnyFormattedText:
        """Return all numbered rows for the scrollable viewport."""
        formatted: list[tuple[str, str]] = []
        for number, item in rows:
            formatted.append(
                ("class:list-view.number", f"{indent}{number:>{number_width}}. ")
            )
            formatted.append(("class:list-view.item", item))
            formatted.append(("", "\n"))
        if formatted:
            formatted.pop()
        return cast(AnyFormattedText, formatted)

    header = Window(
        content=FormattedTextControl(
            text=[
                ("class:list-view.header", f"{indent}{message} "),
                ("class:list-view.count", f"({counted(len(rows), 'item')})"),
            ],
            show_cursor=False,
        ),
        height=Dimension.exact(1),
        wrap_lines=False,
    )
    body = Window(
        content=FormattedTextControl(text=body_text, focusable=True, show_cursor=False),
        height=Dimension.exact(len(rows)),
        wrap_lines=False,
    )
    scroll_pane = ScrollablePane(
        content=body,
        height=Dimension.exact(max_visible_rows),
        max_available_height=len(rows),
        keep_cursor_visible=False,
        keep_focused_window_visible=False,
        show_scrollbar=True,
    )
    footer = Window(
        content=FormattedTextControl(
            text=(
                f"{indent}Up/Down scroll, PgUp/PgDn page, "
                "Enter continues, Ctrl+C cancels"
            ),
            style="class:list-view.help",
            show_cursor=False,
        ),
        height=Dimension.exact(1),
        wrap_lines=False,
    )

    max_scroll = max(0, len(rows) - max_visible_rows)
    bindings = KeyBindings()

    @bindings.add(Keys.Up)
    def scroll_up(event: Any) -> None:
        scroll_pane.vertical_scroll = max(0, scroll_pane.vertical_scroll - 1)
        event.app.invalidate()

    @bindings.add(Keys.Down)
    def scroll_down(event: Any) -> None:
        scroll_pane.vertical_scroll = min(max_scroll, scroll_pane.vertical_scroll + 1)
        event.app.invalidate()

    @bindings.add(Keys.PageUp)
    def page_up(event: Any) -> None:
        scroll_pane.vertical_scroll = max(
            0, scroll_pane.vertical_scroll - max_visible_rows
        )
        event.app.invalidate()

    @bindings.add(Keys.PageDown)
    def page_down(event: Any) -> None:
        scroll_pane.vertical_scroll = min(
            max_scroll, scroll_pane.vertical_scroll + max_visible_rows
        )
        event.app.invalidate()

    @bindings.add(Keys.Home)
    def scroll_home(event: Any) -> None:
        scroll_pane.vertical_scroll = 0
        event.app.invalidate()

    @bindings.add(Keys.End)
    def scroll_end(event: Any) -> None:
        scroll_pane.vertical_scroll = max_scroll
        event.app.invalidate()

    @bindings.add(Keys.ControlM, eager=True)
    def close_viewer(event: Any) -> None:
        event.app.exit(result=True)

    @bindings.add(Keys.ControlC, eager=True)
    def cancel_viewer(event: Any) -> None:
        event.app.exit(result=False)

    @bindings.add(Keys.Escape, eager=True)
    def escape_viewer(event: Any) -> None:
        event.app.exit(result=False)

    application = Application[bool](
        layout=Layout(
            HSplit([header, scroll_pane, footer]),
            focused_element=body,
        ),
        key_bindings=bindings,
        style=LIST_VIEW_STYLE,
        mouse_support=True,
        input=input_source,
        output=output_target,
    )

    try:
        return application.run() is True
    except (KeyboardInterrupt, EOFError):
        console.print()
        return False


def browse_list(
    message: str,
    items: Sequence[str],
    *,
    max_visible_rows: int = MAX_VISIBLE_ROWS,
    level: int = 0,
) -> bool:
    """Display a scrollable, informational list without selectable entries.

    Lists with at most ``max_visible_rows`` items are printed compactly. Longer
    lists open as a read-only viewer that shows at most ``max_visible_rows``
    rows at a time. Arrow keys and page-navigation keys roll through the list;
    Enter closes the viewer and Ctrl+C cancels it.

    Returns ``False`` when the user cancels the viewer, otherwise ``True``.
    """
    if max_visible_rows < 1:
        raise ValueError("max_visible_rows must be at least 1")

    normalized = [str(item) for item in items]
    if not normalized:
        return True

    if len(normalized) <= max_visible_rows:
        _print_compact_list(normalized, level=level)
        return True

    if not is_interactive():
        _print_compact_list(normalized[:max_visible_rows], level=level)
        omitted = len(normalized) - max_visible_rows
        note(
            f"{counted(omitted, 'additional item')} not shown; "
            "rerun in an interactive terminal to scroll the full list."
        )
        return True

    indent = "  " * level
    number_width = len(str(len(normalized)))
    available_width = max(20, console.width - len(indent) - number_width - 10)
    rows = [
        (index, _truncate_middle(item, available_width))
        for index, item in enumerate(normalized, start=1)
    ]
    return _run_read_only_list_viewer(
        message,
        rows,
        max_visible_rows=max_visible_rows,
        level=level,
    )


def _plain_choice_title(choice: Choice) -> str:
    """Return a choice title as plain text for filtering and rendering."""
    if isinstance(choice.title, str):
        return choice.title
    formatted = cast(AnyFormattedText, choice.title)
    return "".join(fragment[1] for fragment in to_formatted_text(formatted))


def _run_context_selection(
    message: str,
    context_title: str,
    context_items: Sequence[str],
    choices: Sequence[str | Choice],
    *,
    max_visible_rows: int,
    multiple: bool,
    exclusive_values: Sequence[str],
    context_metadata: Sequence[str] = (),
    context_item_noun: str = "item",
    input_source: Input | None,
    output_target: Output | None,
) -> list[str] | None:
    """Run a two-pane selector with independently scrollable context."""
    normalized_metadata = [str(item) for item in context_metadata]
    normalized_context = [str(item) for item in context_items]
    normalized_choices: list[tuple[str, str]] = []
    for choice in choices:
        if isinstance(choice, Choice):
            if choice.disabled:
                continue
            title = _plain_choice_title(choice)
            value = choice.value if choice.value is not None else title
        else:
            title = choice
            value = choice
        normalized_choices.append((title, str(value)))

    context_offset = 0
    choice_offset = 0
    choice_cursor = 0
    filter_text = ""
    focused_pane = "context"
    selected_values: set[str] = set()
    exclusive = {str(value) for value in exclusive_values}
    context_number_width = len(str(len(normalized_context)))
    available_width = max(20, console.width - 12)

    def matching_choices() -> list[tuple[str, str]]:
        """Return choices matching the current character filter."""
        if not filter_text:
            return normalized_choices
        query = filter_text.casefold()
        return [
            (title, value)
            for title, value in normalized_choices
            if query in title.casefold()
        ]

    def reset_choice_cursor() -> None:
        """Reset the choice cursor and viewport after filtering changes."""
        nonlocal choice_cursor, choice_offset
        choice_cursor = 0
        choice_offset = 0

    def move_choice_cursor(delta: int) -> None:
        """Move the choice cursor while keeping it inside the viewport."""
        nonlocal choice_cursor, choice_offset
        matches = matching_choices()
        if not matches:
            choice_cursor = 0
            choice_offset = 0
            return

        choice_cursor = max(0, min(len(matches) - 1, choice_cursor + delta))
        if choice_cursor < choice_offset:
            choice_offset = choice_cursor
        elif choice_cursor >= choice_offset + max_visible_rows:
            choice_offset = choice_cursor - max_visible_rows + 1

    def context_metadata_text() -> AnyFormattedText:
        """Render non-list metadata above the contextual rows."""
        fragments: list[tuple[str, str]] = []
        for item in normalized_metadata:
            display = _truncate_middle(item, available_width)
            label, separator, value = display.partition(":")
            if separator:
                fragments.append(
                    (
                        "class:workspace.context-metadata-label",
                        f"{label}{separator}",
                    )
                )
                fragments.append(("class:workspace.context-metadata-value", value))
            else:
                fragments.append(("class:workspace.context-metadata-value", display))
            fragments.append(("", "\n"))
        if fragments:
            fragments.pop()
        return cast(AnyFormattedText, fragments)

    def context_text() -> AnyFormattedText:
        """Render the visible contextual rows without a selection pointer."""
        start = context_offset
        end = min(len(normalized_context), start + max_visible_rows)
        fragments: list[tuple[str, str]] = []
        for index in range(start, end):
            item = _truncate_middle(normalized_context[index], available_width)
            fragments.append(
                (
                    "class:workspace.context-item",
                    f"{index + 1:>{context_number_width}}. {item}\n",
                )
            )
        return cast(AnyFormattedText, fragments)

    def choice_text() -> AnyFormattedText:
        """Render the visible character choices and the active cursor."""
        matches = matching_choices()
        start = choice_offset
        end = min(len(matches), start + max_visible_rows)
        fragments: list[tuple[str, str]] = []
        for index in range(start, end):
            title, value = matches[index]
            title = _truncate_middle(title, available_width - 4)
            active = index == choice_cursor and focused_pane == "choices"
            if active:
                style = "class:workspace.choice.active"
            else:
                style = "class:workspace.choice.inactive"

            if multiple and value not in exclusive:
                marker = "[x]" if value in selected_values else "[ ]"
                prefix = "> " if active else "  "
                fragments.append((style, f"{prefix}{marker} {title}\n"))
            elif multiple:
                prefix = "» " if active else "  "
                fragments.append((style, f"{prefix}{title}\n"))
            else:
                fragments.append((style, f"» {title}\n"))
        if not matches:
            fragments.append(
                (
                    "class:workspace.choice.inactive",
                    "  No matching characters.\n",
                )
            )
        return cast(AnyFormattedText, fragments)

    def context_header_text() -> AnyFormattedText:
        """Render the contextual pane header and active state."""
        style = (
            "class:workspace.header.active"
            if focused_pane == "context"
            else "class:workspace.header"
        )
        fragments: list[tuple[str, str]] = [(style, context_title)]
        if normalized_context:
            start = context_offset + 1
            end = min(len(normalized_context), context_offset + max_visible_rows)
            fragments.append(
                (
                    style,
                    (
                        "  ("
                        f"{counted(len(normalized_context), context_item_noun)}; "
                        f"showing {start}-{end})"
                    ),
                )
            )
        fragments.append((style, "  [ACTIVE]" if focused_pane == "context" else ""))
        return cast(AnyFormattedText, fragments)

    def choice_header_text() -> AnyFormattedText:
        """Render the character pane header, count, and filter."""
        style = (
            "class:workspace.header.active"
            if focused_pane == "choices"
            else "class:workspace.header"
        )
        fragments: list[tuple[str, str]] = [(style, message)]
        fragments.append(
            (
                style,
                f"  ({len(matching_choices())} of {len(normalized_choices)} choices)",
            )
        )
        if multiple:
            fragments.append((style, f"  selected: {len(selected_values)}"))
        if filter_text:
            fragments.append((style, f"  filter: {filter_text}"))
        fragments.append((style, "  [ACTIVE]" if focused_pane == "choices" else ""))
        return cast(AnyFormattedText, fragments)

    def help_text() -> AnyFormattedText:
        """Render controls for the currently focused pane."""
        if focused_pane == "context":
            text = (
                "Tab: focus characters | Up/Down: scroll composites | "
                "Enter: focus characters | Esc: cancel"
            )
        else:
            if multiple:
                text = (
                    "Tab: focus composites | Up/Down: choose character | "
                    "Space: toggle | type: filter | Enter: confirm | "
                    "Esc: clear/cancel"
                )
            else:
                text = (
                    "Tab: focus composites | Up/Down: choose character | "
                    "type: filter | Enter: select | Esc: clear/cancel"
                )
        return cast(AnyFormattedText, [("class:workspace.help", text)])

    context_metadata_window = Window(
        content=FormattedTextControl(text=context_metadata_text, show_cursor=False),
        height=Dimension.exact(len(normalized_metadata)),
        wrap_lines=False,
    )
    context_header = Window(
        content=FormattedTextControl(text=context_header_text, show_cursor=False),
        height=Dimension.exact(1),
        wrap_lines=False,
    )
    context_body = Window(
        content=FormattedTextControl(text=context_text, show_cursor=False),
        height=Dimension.exact(max_visible_rows),
        wrap_lines=False,
    )
    separator = Window(
        content=FormattedTextControl(
            text=("-" * max(20, console.width)),
            style="class:workspace.separator",
            show_cursor=False,
        ),
        height=Dimension.exact(1),
        wrap_lines=False,
    )
    choice_header = Window(
        content=FormattedTextControl(text=choice_header_text, show_cursor=False),
        height=Dimension.exact(1),
        wrap_lines=False,
    )
    choice_body = Window(
        content=FormattedTextControl(text=choice_text, show_cursor=False),
        height=Dimension.exact(max_visible_rows),
        wrap_lines=False,
    )
    footer = Window(
        content=FormattedTextControl(text=help_text, show_cursor=False),
        height=Dimension.exact(1),
        wrap_lines=False,
    )

    bindings = KeyBindings()

    @bindings.add(Keys.ControlI)
    @bindings.add(Keys.BackTab)
    def switch_pane(event: Any) -> None:
        nonlocal focused_pane
        focused_pane = "choices" if focused_pane == "context" else "context"
        event.app.invalidate()

    @bindings.add(Keys.Up)
    def move_up(event: Any) -> None:
        nonlocal context_offset
        if focused_pane == "context":
            context_offset = max(0, context_offset - 1)
        else:
            move_choice_cursor(-1)
        event.app.invalidate()

    @bindings.add(Keys.Down)
    def move_down(event: Any) -> None:
        nonlocal context_offset
        if focused_pane == "context":
            max_offset = max(0, len(normalized_context) - max_visible_rows)
            context_offset = min(max_offset, context_offset + 1)
        else:
            move_choice_cursor(1)
        event.app.invalidate()

    @bindings.add(Keys.PageUp)
    def page_up(event: Any) -> None:
        nonlocal context_offset
        if focused_pane == "context":
            context_offset = max(0, context_offset - max_visible_rows)
        else:
            move_choice_cursor(-max_visible_rows)
        event.app.invalidate()

    @bindings.add(Keys.PageDown)
    def page_down(event: Any) -> None:
        nonlocal context_offset
        if focused_pane == "context":
            max_offset = max(0, len(normalized_context) - max_visible_rows)
            context_offset = min(max_offset, context_offset + max_visible_rows)
        else:
            move_choice_cursor(max_visible_rows)
        event.app.invalidate()

    @bindings.add(Keys.Home)
    def go_home(event: Any) -> None:
        nonlocal choice_cursor, choice_offset, context_offset
        if focused_pane == "context":
            context_offset = 0
        else:
            choice_cursor = 0
            choice_offset = 0
        event.app.invalidate()

    @bindings.add(Keys.End)
    def go_end(event: Any) -> None:
        nonlocal choice_cursor, choice_offset, context_offset
        if focused_pane == "context":
            context_offset = max(0, len(normalized_context) - max_visible_rows)
        else:
            matches = matching_choices()
            if matches:
                choice_cursor = len(matches) - 1
                choice_offset = max(0, len(matches) - max_visible_rows)
        event.app.invalidate()

    @bindings.add(Keys.Backspace)
    def remove_filter_character(event: Any) -> None:
        nonlocal filter_text
        if focused_pane == "choices" and filter_text:
            filter_text = filter_text[:-1]
            reset_choice_cursor()
            event.app.invalidate()

    @bindings.add(Keys.ControlU)
    def clear_filter(event: Any) -> None:
        nonlocal filter_text
        if focused_pane == "choices" and filter_text:
            filter_text = ""
            reset_choice_cursor()
            event.app.invalidate()

    @bindings.add("space")
    def toggle_choice(event: Any) -> None:
        if not multiple or focused_pane != "choices":
            return
        matches = matching_choices()
        if not matches:
            return
        value = matches[choice_cursor][1]
        if value in exclusive:
            return
        if value in selected_values:
            selected_values.remove(value)
        else:
            selected_values.add(value)
        event.app.invalidate()

    @bindings.add(Keys.Any)
    def filter_choices(event: Any) -> None:
        nonlocal filter_text
        data = event.data
        if (
            focused_pane == "choices"
            and data
            and data != " "
            and len(data) == 1
            and data.isprintable()
        ):
            filter_text += data
            reset_choice_cursor()
            event.app.invalidate()

    @bindings.add(Keys.ControlM, eager=True)
    def confirm_choice(event: Any) -> None:
        nonlocal focused_pane
        if focused_pane == "context":
            focused_pane = "choices"
            event.app.invalidate()
            return

        matches = matching_choices()
        if not matches:
            return

        _, value = matches[choice_cursor]
        if multiple and value not in exclusive:
            selected_values.add(value)
            result = [
                choice_value
                for _, choice_value in normalized_choices
                if choice_value in selected_values
            ]
        else:
            result = [value]
        event.app.exit(result=result)

    @bindings.add(Keys.Escape, eager=True)
    def cancel_or_clear(event: Any) -> None:
        nonlocal filter_text
        if focused_pane == "choices" and filter_text:
            filter_text = ""
            reset_choice_cursor()
            event.app.invalidate()
            return
        event.app.exit(result=None)

    @bindings.add(Keys.ControlC, eager=True)
    def cancel(event: Any) -> None:
        event.app.exit(result=None)

    context_windows = [context_header, context_body]
    if normalized_metadata:
        context_windows.insert(0, context_metadata_window)

    application = Application[list[str] | None](
        layout=Layout(
            HSplit(
                [
                    *context_windows,
                    separator,
                    choice_header,
                    choice_body,
                    footer,
                ]
            ),
            focused_element=context_body,
        ),
        key_bindings=bindings,
        style=LIST_VIEW_STYLE,
        input=input_source,
        output=output_target,
    )

    try:
        return application.run()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return None


def select_with_context(
    message: str,
    *,
    context_title: str,
    context_items: Sequence[str],
    choices: Sequence[str | Choice],
    context_metadata: Sequence[str] = (),
    context_item_noun: str = "item",
    max_visible_rows: int = MAX_VISIBLE_ROWS,
) -> str | None:
    """Select one choice while keeping scrollable context visible beside it.

    The context pane is read-only. Optional ``context_metadata`` entries are
    rendered as unnumbered lines above the numbered context list. The choice
    pane supports filtering and selection. ``Tab`` switches focus between the
    panes; ``Enter`` in the context pane moves focus to the choices, while
    ``Enter`` in the choice pane confirms the highlighted choice.

    Returns ``None`` when the user cancels the interaction.
    """
    if max_visible_rows < 1:
        raise ValueError("max_visible_rows must be at least 1")
    if not is_interactive():
        return None

    result = _run_context_selection(
        message,
        context_title,
        context_items,
        choices,
        max_visible_rows=max_visible_rows,
        multiple=False,
        exclusive_values=(),
        context_metadata=context_metadata,
        context_item_noun=context_item_noun,
        input_source=None,
        output_target=None,
    )
    return result[0] if result else None


def select_multiple_with_context(
    message: str,
    *,
    context_title: str,
    context_items: Sequence[str],
    choices: Sequence[str | Choice],
    context_metadata: Sequence[str] = (),
    context_item_noun: str = "item",
    exclusive_values: Sequence[str] = (),
    max_visible_rows: int = MAX_VISIBLE_ROWS,
) -> list[str] | None:
    """Select one or more choices while keeping scrollable context visible.

    Character choices use checkbox-style selection: ``Space`` toggles the
    highlighted character and ``Enter`` confirms the highlighted character plus
    any characters already toggled. Choices whose values appear in
    ``exclusive_values`` are actions instead; selecting one returns only that
    action. Optional ``context_metadata`` entries are rendered separately from
    the numbered context list.

    Returns ``None`` when the user cancels the interaction.
    """
    if max_visible_rows < 1:
        raise ValueError("max_visible_rows must be at least 1")
    if not is_interactive():
        return None

    return _run_context_selection(
        message,
        context_title,
        context_items,
        choices,
        max_visible_rows=max_visible_rows,
        multiple=True,
        exclusive_values=exclusive_values,
        context_metadata=context_metadata,
        context_item_noun=context_item_noun,
        input_source=None,
        output_target=None,
    )


def _normalize_shortcut_key(
    shortcut_key: str | bool | None,
) -> str | bool | None:
    """Return a Questionary-compatible shortcut key.

    Questionary's built-in shortcut registry contains lowercase letters and
    digits only, so uppercase keys are normalized before the prompt is built.
    """
    if not isinstance(shortcut_key, str):
        return shortcut_key

    normalized = shortcut_key.lower()
    if len(normalized) != 1 or not normalized.isascii() or not normalized.isalnum():
        raise ValueError(
            f"Invalid shortcut {shortcut_key!r}. Shortcuts must be a single "
            "letter or number."
        )
    return normalized


def _set_select_viewport(
    question: Any,
    item_count: int,
    max_visible_rows: int,
) -> None:
    """Constrain a Questionary selection list to a compact viewport."""
    visible_rows = max(1, min(max_visible_rows, item_count))
    for window in question.application.layout.find_all_windows():
        if isinstance(window.content, InquirerControl):
            window.height = Dimension.exact(visible_rows)


def _set_completion_viewport(question: Any, max_visible_rows: int) -> None:
    """Constrain a Questionary autocomplete completion menu."""
    for window in question.application.layout.find_all_windows():
        if isinstance(window.content, CompletionsMenuControl):
            window.height = Dimension(min=1, max=max_visible_rows)


def select(
    message: str,
    choices: Sequence[str | Choice],
    default: str | None = None,
    use_shortcuts: bool = False,
    *,
    max_visible_rows: int = MAX_VISIBLE_ROWS,
    use_search_filter: bool = False,
) -> str | None:
    """Ask the user to pick one option from a list.

    Uses ``questionary.autocomplete`` for type-to-filter (fuzzy search) by
    default. Set ``use_shortcuts=True`` for short lists where single-key
    selection is preferred. Set ``use_search_filter=True`` for a visible,
    scrollable selection list that filters as printable characters are typed.

    Accepts plain strings, :class:`questionary.Choice` (with metadata title
    and clean value), and :class:`questionary.Separator` for grouping.

    When ``default`` is set, the matching choice is annotated with
    `` (default)`` so the recommended option is visually obvious. At most
    ``max_visible_rows`` choices are visible at once; arrow keys roll through
    the remaining choices.

    Returns ``None`` if the user cancels (Ctrl+C / EOF).
    """
    if max_visible_rows < 1:
        raise ValueError("max_visible_rows must be at least 1")
    if use_shortcuts and use_search_filter:
        raise ValueError("use_shortcuts and use_search_filter cannot both be enabled")

    # Normalize choices and annotate the recommended option.
    normalized: list[str | Choice] = []
    for choice in choices:
        if isinstance(choice, Separator):
            normalized.append(choice)
            continue
        if isinstance(choice, Choice):
            title = choice.title
            if default is not None and choice.value == default:
                title = f"{title}  (default)"
            normalized.append(
                Choice(
                    title=title,
                    value=choice.value,
                    disabled=choice.disabled,
                    checked=choice.checked,
                    shortcut_key=_normalize_shortcut_key(choice.shortcut_key),
                    description=choice.description,
                )
            )
        else:
            title = choice
            if default is not None and choice == default:
                title = f"{title}  (default)"
            normalized.append(title)

    selectable_count = sum(
        1 for choice in normalized if not isinstance(choice, Separator)
    )

    try:
        if use_shortcuts:
            question = questionary.select(
                message,
                choices=normalized,
                default=default,
                use_shortcuts=True,
                style=CUSTOM_STYLE,
            )
            _set_select_viewport(question, selectable_count, max_visible_rows)
            return question.ask()

        if use_search_filter:
            question = questionary.select(
                message,
                choices=normalized,
                default=default,
                use_arrow_keys=True,
                use_jk_keys=False,
                use_emacs_keys=False,
                use_search_filter=True,
                style=CUSTOM_STYLE,
            )
            _set_select_viewport(question, selectable_count, max_visible_rows)
            return question.ask()

        completion_choices = [
            choice if isinstance(choice, str) else str(choice.value)
            for choice in normalized
            if not isinstance(choice, Separator)
        ]
        question = questionary.autocomplete(
            message,
            choices=completion_choices,
            default=default or "",
            style=CUSTOM_STYLE,
        )
        _set_completion_viewport(question, max_visible_rows)
        return question.ask()
    except (KeyboardInterrupt, EOFError):
        console.print()
        return None


# --------------------------------------------------------------------------- #
# Typer app factory                                                           #
# --------------------------------------------------------------------------- #


def create_app(help: str) -> typer.Typer:
    """Create a typer app with consistent project settings."""
    return typer.Typer(help=help)

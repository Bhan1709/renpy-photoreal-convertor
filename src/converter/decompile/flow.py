"""End-to-end generalized decompile flow."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Group  # type: ignore[attr-defined]
from rich.text import Text

from cli import (
    MAUVE,
    Choice,
    console,
    counted,
    error,
    info,
    is_interactive,
    kv,
    print_panel,
    rule,
    select,
    success,
)

from .config import write_config
from .constants import CONFIG_PATH, GAME_FOLDER, RENPY_GAMES, ROOT
from .decompile_files import decompile_rpyc_files_detailed
from .discovery import find_game_folder, set_game_folder_rel_path
from .extract import ArchiveExtraction, extract_all_rpa_detailed
from .scan import scan_image_categories
from .staging import copy_game_files


@dataclass(frozen=True)
class DecompileRun:
    """Structured result of one canonical decompile preparation run."""

    game_folder: Path | None = None
    source_game: str | None = None
    source_path: Path | None = None
    copied: int = 0
    extracted_archives: tuple[ArchiveExtraction, ...] = ()
    decompiled: int = 0
    config: Path | None = None
    dry_run: bool = False
    image_categories: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        """Return whether the requested preparation completed successfully."""
        if self.game_folder is None or self.errors:
            return False
        return self.dry_run or self.config is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the legacy dictionary summary representation."""
        archive_metadata: list[dict[str, str]] = []
        if self.game_folder is not None:
            for extraction in self.extracted_archives:
                if extraction.success:
                    archive_metadata.append(extraction.metadata(self.game_folder))
        return {
            "success": self.success,
            "game_folder": str(self.game_folder) if self.game_folder else None,
            "source_game": self.source_game,
            "source_path": str(self.source_path) if self.source_path else None,
            "copied": self.copied,
            "extracted_archives": archive_metadata,
            "decompiled": self.decompiled,
            "config": str(self.config) if self.config else None,
            "dry_run": self.dry_run,
            "image_categories": dict(self.image_categories),
            "errors": list(self.errors),
        }


def prepare_game(
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
    *,
    dry_run: bool = False,
) -> DecompileRun:
    """Prepare a staged, extracted, decompiled game and write its config.

    This is the non-interactive core shared by the decompile CLI and the
    end-to-end pipeline.  It never modifies the source game: all destructive
    work is performed in ``dest``.
    """
    game_folder = find_game_folder(renpy_games, exclude=dest)
    if game_folder is None:
        error(f"No \\game folder found under {renpy_games}")
        return DecompileRun(dry_run=dry_run)

    source_game_dir = game_folder.parent
    if dry_run:
        info("Dry run: relevant files would be copied into the staged folder.")
        info("Dry run: archives would be extracted and compiled scripts decompiled.")
        info("Dry run: no files or configuration would be written.")
        return DecompileRun(
            game_folder=game_folder,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            dry_run=True,
        )

    rule(f"Game folder: {game_folder}")
    kv("Source game", source_game_dir.name)

    errors: list[str] = []
    try:
        copied = copy_game_files(game_folder, dest)
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to stage game files: {exc}")
        return DecompileRun(
            game_folder=dest,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            errors=(str(exc),),
        )

    success(f"Copied {counted(len(copied), 'file')} into {dest}.")

    try:
        extracted = tuple(extract_all_rpa_detailed(dest))
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to extract archives: {exc}")
        extracted = ()
        errors.append(str(exc))

    for extraction in extracted:
        if extraction.error:
            errors.append(
                f"{extraction.archive.name}: {extraction.error}"
            )

    try:
        decompile_result = decompile_rpyc_files_detailed(dest)
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to decompile scripts: {exc}")
        decompile_result = None
        errors.append(str(exc))

    decompiled = decompile_result.succeeded if decompile_result is not None else 0
    if decompile_result is not None:
        for file_result in decompile_result.files:
            if not file_result.success:
                errors.append(
                    f"{file_result.source.name}: {file_result.error or 'unknown error'}"
                )

    if errors:
        error("Decompile preparation failed; config was not written.")
        return DecompileRun(
            game_folder=dest,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            copied=len(copied),
            extracted_archives=extracted,
            decompiled=decompiled,
            errors=tuple(errors),
        )

    try:
        image_categories = scan_image_categories(dest)
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to scan image categories: {exc}")
        return DecompileRun(
            game_folder=dest,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            copied=len(copied),
            extracted_archives=extracted,
            decompiled=decompiled,
            errors=(str(exc),),
        )

    try:
        config_out = write_config(
            dest,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            extracted_archives=extracted,
            image_categories=image_categories,
            config_path=config_path,
        )
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to write game configuration: {exc}")
        return DecompileRun(
            game_folder=dest,
            source_game=source_game_dir.name,
            source_path=source_game_dir,
            copied=len(copied),
            extracted_archives=extracted,
            decompiled=decompiled,
            image_categories=image_categories,
            errors=(str(exc),),
        )

    try:
        set_game_folder_rel_path(dest.relative_to(ROOT))
    except ValueError:
        set_game_folder_rel_path(dest)

    return DecompileRun(
        game_folder=dest,
        source_game=source_game_dir.name,
        source_path=source_game_dir,
        copied=len(copied),
        extracted_archives=extracted,
        decompiled=decompiled,
        config=config_out,
        image_categories=image_categories,
    )


def process_game(
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    r"""Run the full generalized decompile flow for one game.

    The core work is delegated to :func:`prepare_game`.  This compatibility
    wrapper keeps the interactive follow-up prompt and the historical
    dictionary summary API.
    """
    result = prepare_game(
        renpy_games=renpy_games,
        dest=dest,
        config_path=config_path,
        dry_run=dry_run,
    )
    summary = result.to_dict()

    if not result.success:
        return summary

    note = Text.from_markup(
        "[dim]Compose scans every script in the game folder; remove unwanted "
        "scripts there before composing.[/dim]"
    )

    console.print()
    print_panel(
        Group(
            Text("Next step: compose photoreal composites", style="bold blue"),
            Text.from_markup("  [cyan]mise run compose[/cyan]"),
            Text.from_markup(
                "[dim]Offers clean, compose, clean+compose, review, and apply.[/dim]"
            ),
            Text(""),
            note,
        ),
        title="Decompile flow complete",
        style=MAUVE,
    )
    console.print()

    if dry_run or not is_interactive():
        if not is_interactive():
            info("No interactive terminal detected. Run 'mise run compose' when ready.")
        return summary

    choice = select(
        "Continue to the compose menu?",
        choices=[
            Choice(
                title="Yes, open the compose menu",
                value="compose",
                shortcut_key="y",
            ),
            Choice(
                title="Exit (run 'mise run compose' later)",
                value="quit",
                shortcut_key="x",
            ),
        ],
        default="compose",
        use_shortcuts=True,
    )

    if choice is None:
        info("Cancelled. The compose step was not started.")
        return summary

    if choice == "compose":
        cmd = [sys.executable, "-m", "converter.compose"]
        info(f"Running: {' '.join(cmd)}")
        summary["compose_exit_code"] = subprocess.run(cmd, check=False).returncode
        return summary

    return summary


def process_all_games() -> None:
    r"""Backward-compatible entry point: process the first game found."""
    process_game()


__all__ = ["DecompileRun", "prepare_game", "process_all_games", "process_game"]

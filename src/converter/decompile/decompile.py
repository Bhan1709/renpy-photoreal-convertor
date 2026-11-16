"""Backward-compatible shim for :mod:`converter.decompile`.

All logic now lives in focused submodules.  This module re-exports the public
API so existing imports (e.g. from :mod:`converter.pipeline`) keep working::

    from src.converter.decompile.decompile import (
        decompile_rpyc_files,
        extract_rpa_file,
        find_game_folders,
        restore_decompile,
    )

Run directly as a script::

    python src/converter/decompile/decompile.py
"""

from __future__ import annotations

try:
    # Imported as part of the package.
    from . import (  # public re-exports
        CATEGORY_FOLDER_PATTERNS,
        CONFIG_PATH,
        COPY_EXTENSIONS,
        COPY_FILENAMES,
        GAME_FOLDER,
        IMAGE_EXTENSIONS,
        RENPY_GAMES,
        ROOT,
        copy_game_files,
        decompile_rpyc_files,
        extract_all_rpa,
        extract_rpa_file,
        filter_source_files,
        find_game_folder,
        find_game_folders,
        get_game_folder_rel_path,
        load_config,
        matches_exclusion,
        process_all_games,
        process_game,
        restore_decompile,
        scan_extracted_archives,
        scan_image_categories,
        scan_source_files,
        set_game_folder_rel_path,
        sync_exclusions,
        write_config,
    )
except ImportError:  # pragma: no cover - run as script
    import sys
    from pathlib import Path

    # Running as `python src/converter/decompile/decompile.py`: make the
    # repo root importable so absolute package imports resolve.
    _here = Path(__file__).resolve()
    _repo_root = _here.parents[3]  # .../decompile/decompile.py -> repo root
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

    from src.converter.decompile import (
        CATEGORY_FOLDER_PATTERNS,
        CONFIG_PATH,
        COPY_EXTENSIONS,
        COPY_FILENAMES,
        GAME_FOLDER,
        IMAGE_EXTENSIONS,
        RENPY_GAMES,
        ROOT,
        copy_game_files,
        decompile_rpyc_files,
        extract_all_rpa,
        extract_rpa_file,
        filter_source_files,
        find_game_folder,
        find_game_folders,
        get_game_folder_rel_path,
        load_config,
        matches_exclusion,
        process_all_games,
        process_game,
        restore_decompile,
        scan_extracted_archives,
        scan_image_categories,
        scan_source_files,
        set_game_folder_rel_path,
        sync_exclusions,
        write_config,
    )

__all__ = [
    "CATEGORY_FOLDER_PATTERNS",
    "CONFIG_PATH",
    "COPY_EXTENSIONS",
    "COPY_FILENAMES",
    "GAME_FOLDER",
    "IMAGE_EXTENSIONS",
    "RENPY_GAMES",
    "ROOT",
    "copy_game_files",
    "decompile_rpyc_files",
    "extract_all_rpa",
    "extract_rpa_file",
    "filter_source_files",
    "find_game_folder",
    "find_game_folders",
    "get_game_folder_rel_path",
    "load_config",
    "matches_exclusion",
    "process_all_games",
    "process_game",
    "restore_decompile",
    "scan_extracted_archives",
    "scan_image_categories",
    "scan_source_files",
    "set_game_folder_rel_path",
    "sync_exclusions",
    "write_config",
]


if __name__ == "__main__":
    process_game()

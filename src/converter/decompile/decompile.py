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
    from . import (  # noqa: F401  (public re-exports)
        CONFIG_PATH,
        COPY_EXTENSIONS,
        COPY_FILENAMES,
        GAME_FOLDER,
        IMAGE_EXTENSIONS,
        RENPY_GAMES,
        ROOT,
        CATEGORY_FOLDER_PATTERNS,
        find_game_folder,
        find_game_folders,
        set_game_folder_rel_path,
        get_game_folder_rel_path,
        copy_game_files,
        extract_rpa_file,
        extract_all_rpa,
        decompile_rpyc_files,
        scan_source_files,
        scan_extracted_archives,
        scan_image_categories,
        write_config,
        load_config,
        matches_exclusion,
        filter_source_files,
        sync_exclusions,
        process_game,
        process_all_games,
        restore_decompile,
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

    from src.converter.decompile import (  # noqa: E402,F401
        CONFIG_PATH,
        COPY_EXTENSIONS,
        COPY_FILENAMES,
        GAME_FOLDER,
        IMAGE_EXTENSIONS,
        RENPY_GAMES,
        ROOT,
        CATEGORY_FOLDER_PATTERNS,
        find_game_folder,
        find_game_folders,
        set_game_folder_rel_path,
        get_game_folder_rel_path,
        copy_game_files,
        extract_rpa_file,
        extract_all_rpa,
        decompile_rpyc_files,
        scan_source_files,
        scan_extracted_archives,
        scan_image_categories,
        write_config,
        load_config,
        matches_exclusion,
        filter_source_files,
        sync_exclusions,
        process_game,
        process_all_games,
        restore_decompile,
    )

__all__ = [
    "CONFIG_PATH",
    "COPY_EXTENSIONS",
    "COPY_FILENAMES",
    "GAME_FOLDER",
    "IMAGE_EXTENSIONS",
    "RENPY_GAMES",
    "ROOT",
    "CATEGORY_FOLDER_PATTERNS",
    "find_game_folder",
    "find_game_folders",
    "set_game_folder_rel_path",
    "get_game_folder_rel_path",
    "copy_game_files",
    "extract_rpa_file",
    "extract_all_rpa",
    "decompile_rpyc_files",
    "scan_source_files",
    "scan_extracted_archives",
    "scan_image_categories",
    "write_config",
    "load_config",
    "matches_exclusion",
    "filter_source_files",
    "sync_exclusions",
    "process_game",
    "process_all_games",
    "restore_decompile",
]


if __name__ == "__main__":
    process_game()

"""Decompile subpackage: generalized Ren'Py decompile & extract.

Public API::

    from converter.decompile import (
        find_game_folder,
        find_game_folders,
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
        set_game_folder_rel_path,
        get_game_folder_rel_path,
    )
"""

from __future__ import annotations

from .constants import (
    CONFIG_PATH,
    COPY_EXTENSIONS,
    COPY_FILENAMES,
    GAME_FOLDER,
    IMAGE_EXTENSIONS,
    RENPY_GAMES,
    ROOT,
    CATEGORY_FOLDER_PATTERNS,
)
from .discovery import (
    find_game_folder,
    find_game_folders,
    set_game_folder_rel_path,
    get_game_folder_rel_path,
)
from .staging import copy_game_files
from .extract import extract_rpa_file, extract_all_rpa
from .decompile_files import decompile_rpyc_files
from .scan import (
    scan_source_files,
    scan_extracted_archives,
    scan_image_categories,
    _image_mode,
)
from .config import write_config, load_config
from .exclusions import matches_exclusion, filter_source_files, sync_exclusions
from .flow import process_game, process_all_games
from .restore import restore_decompile

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

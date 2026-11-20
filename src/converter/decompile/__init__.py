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
        prepare_game,
        process_game,
        process_all_games,
        restore_decompile,
        set_game_folder_rel_path,
        get_game_folder_rel_path,
    )
"""

from __future__ import annotations

from .config import load_config, write_config
from .constants import (
    CATEGORY_FOLDER_PATTERNS,
    CONFIG_PATH,
    COPY_EXTENSIONS,
    COPY_FILENAMES,
    GAME_FOLDER,
    IMAGE_EXTENSIONS,
    RENPY_GAMES,
    ROOT,
)
from .decompile_files import (
    DecompileFileResult,
    DecompileFilesResult,
    decompile_rpyc_files,
    decompile_rpyc_files_detailed,
)
from .discovery import (
    find_game_folder,
    find_game_folders,
    get_game_folder_rel_path,
    set_game_folder_rel_path,
)
from .extract import (
    ArchiveExtraction,
    extract_all_rpa,
    extract_all_rpa_detailed,
    extract_rpa_file,
    extract_rpa_file_detailed,
)
from .flow import DecompileRun, prepare_game, process_all_games, process_game
from .restore import restore_decompile
from .scan import (
    scan_extracted_archives,
    scan_image_categories,
    scan_source_files,
)
from .staging import copy_game_files

__all__ = [
    "CATEGORY_FOLDER_PATTERNS",
    "CONFIG_PATH",
    "COPY_EXTENSIONS",
    "COPY_FILENAMES",
    "GAME_FOLDER",
    "IMAGE_EXTENSIONS",
    "RENPY_GAMES",
    "ROOT",
    "ArchiveExtraction",
    "DecompileFileResult",
    "DecompileFilesResult",
    "DecompileRun",
    "copy_game_files",
    "decompile_rpyc_files",
    "decompile_rpyc_files_detailed",
    "extract_all_rpa",
    "extract_all_rpa_detailed",
    "extract_rpa_file",
    "extract_rpa_file_detailed",
    "find_game_folder",
    "find_game_folders",
    "get_game_folder_rel_path",
    "load_config",
    "prepare_game",
    "process_all_games",
    "process_game",
    "restore_decompile",
    "scan_extracted_archives",
    "scan_image_categories",
    "scan_source_files",
    "set_game_folder_rel_path",
    "write_config",
]

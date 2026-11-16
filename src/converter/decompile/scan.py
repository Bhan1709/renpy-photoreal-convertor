"""Scanning: source files, extracted archives, and image categories."""

from __future__ import annotations

from pathlib import Path

from .constants import CATEGORY_FOLDER_PATTERNS, IMAGE_EXTENSIONS


def scan_source_files(game_folder: Path) -> dict[str, list[str]]:
    r"""Return relative paths of all ``.rpy`` and ``_ren.py`` files.

    These relative paths are the "matching patterns" recorded in
    ``config.json`` for downstream processing.
    """
    rpy_files: list[str] = []
    ren_py_files: list[str] = []
    for path in sorted(game_folder.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(game_folder).as_posix()
        if path.name == "_ren.py":
            ren_py_files.append(rel)
        elif path.suffix == ".rpy":
            rpy_files.append(rel)
    return {"rpy": rpy_files, "ren_py": ren_py_files}


def scan_extracted_archives(game_folder: Path) -> list[dict[str, str]]:
    r"""Return info about extracted archive folders under ``game_folder``.

    For every ``.rpa`` file found, records the relative path of the archive
    and its extracted sibling directory (named after the ``.rpa`` stem).

    Returns a list of dicts with keys ``archive`` and ``extracted_folder``,
    both relative to ``game_folder`` (forward-slash).
    """
    extracted: list[dict[str, str]] = []
    for rpa_file in sorted(game_folder.rglob("*.rpa")):
        extract_dir = rpa_file.parent / rpa_file.stem
        if extract_dir.is_dir():
            extracted.append({
                "archive": rpa_file.relative_to(game_folder).as_posix(),
                "extracted_folder": extract_dir.relative_to(game_folder).as_posix(),
            })
    return extracted


def _image_mode(path: Path) -> str | None:
    r"""Return the Pillow color mode of an image without loading pixels.

    ``Image.open()`` is lazy: it reads the header only.  ``.mode`` is
    available from the header and does not force pixel decoding into memory.
    """
    from PIL import Image

    try:
        with Image.open(path) as im:
            return im.mode
    except Exception:  # noqa: BLE001
        return None


def scan_image_categories(game_folder: Path) -> dict:
    r"""Detect image category folders under ``game_folder``.

    Walks the game folder looking for directories whose names match the
    Ren'Py category folder patterns (case-insensitive).  The first match per
    category wins.

    Returns::

        {
            "classification_method": "folder_based",
            "image_categories": {
                "bg": "archive/bgs",
                "char": "archive/chars",
                "gui": "archive/gui",
                "scene": "archive/xxx",
            },
        }

    If no category folders are found, falls back to classifying each image by
    its Pillow color mode (RGBA → char, RGB → scene)::

        {
            "classification_method": "mode_based",
            "image_categories": {
                "char": ["chars/alice.png", ...],
                "scene": ["scenes/room1.png", ...],
            },
        }
    """
    # --- Folder-based detection -------------------------------------
    found: dict[str, str] = {}
    for item in game_folder.rglob("*"):
        if not item.is_dir():
            continue
        name_lower = item.name.lower()
        for category, patterns in CATEGORY_FOLDER_PATTERNS.items():
            if category in found:
                continue
            if name_lower in patterns:
                found[category] = item.relative_to(game_folder).as_posix()
                break

    if found:
        return {
            "classification_method": "folder_based",
            "image_categories": found,
        }

    # --- Mode-based fallback ----------------------------------------
    char_images: list[str] = []
    scene_images: list[str] = []
    for item in sorted(game_folder.rglob("*")):
        if not item.is_file():
            continue
        if item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        mode = _image_mode(item)
        if mode == "RGBA":
            char_images.append(item.relative_to(game_folder).as_posix())
        elif mode == "RGB":
            scene_images.append(item.relative_to(game_folder).as_posix())

    return {
        "classification_method": "mode_based",
        "image_categories": {
            "char": char_images,
            "scene": scene_images,
        },
    }

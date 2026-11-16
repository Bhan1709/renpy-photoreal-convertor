r"""Generalized Ren'Py decompile & extract subpackage.

Ren'Py games come in all shapes and sizes, but they all ship a ``\game``
folder.  This subpackage implements a generalized, repeatable flow:

  1. Recursively search ``src/renpy_game`` for the first ``\game`` folder.
  2. Copy the relevant files (``.rpa``, ``.rpyc``, ``.rpy``, ``_ren.py``)
     from that game folder into a consolidated ``src/renpy_game/game``
     folder, preserving relative directory structure.
  3. Extract every ``.rpa`` alongside itself
     (``game/archive.rpa`` -> ``game/archive/``).
  4. Decompile every ``.rpyc`` (recursively) into a sibling ``.rpy``.
  5. Write ``src/converter/config.json`` storing matching patterns for all
     ``.rpy`` and ``_ren.py`` files plus an ``exclusion`` parameter the user
     can use to exclude patterns from downstream processing.

The subpackage depends on ``rpa-toolkit``.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path

ROOT = Path("src")
RENPY_GAMES = ROOT / "renpy_game"
GAME_FOLDER = RENPY_GAMES / "game"
CONFIG_PATH = ROOT / "converter" / "config.json"

# File kinds we consolidate when staging a game.
COPY_EXTENSIONS = {".rpa", ".rpyc", ".rpy"}
COPY_FILENAMES = {"_ren.py"}

# Image file extensions we care about for category classification.
IMAGE_EXTENSIONS = {".png", ".webp", ".avif", ".jpeg", ".jpg"}

# Ren'Py folder name patterns per image category (matched case-insensitively).
# "images" is deliberately omitted: it is ambiguous (used for both char and
# scene) so it is safer to fall back to mode-based classification when only
# "images" exists.
CATEGORY_FOLDER_PATTERNS: dict[str, set[str]] = {
    "bg": {"bgs", "bg", "background", "backgrounds"},
    "gui": {"gui", "ui", "interface"},
    "char": {"chars", "char", "character", "characters"},
    "scene": {"scene", "scenes", "xxx"},
}

# Cache for the located game folder relative path (kept for backward
# compatibility so other subpackages can still query it).
_game_folder_rel_path: Path | None = None


def set_game_folder_rel_path(rel_path: Path) -> None:
    r"""Cache the relative path of the \game folder for other subpackages."""
    global _game_folder_rel_path
    _game_folder_rel_path = rel_path


def get_game_folder_rel_path() -> Path | None:
    r"""Get the cached relative path of the \game folder."""
    return _game_folder_rel_path


def find_game_folder(renpy_games: Path = RENPY_GAMES) -> Path | None:
    r"""Return the first directory named ``game`` found under ``renpy_games``.

    Ren'Py games come in all shapes, but they all ship a ``\game`` folder.
    We recursively walk ``src/renpy_game`` and return the first match so the
    rest of the flow operates on a single, well-known location.

    The consolidated output folder (``src/renpy_game/game``) is deliberately
    excluded so re-runs do not treat their own output as a source game.
    """
    if not renpy_games.exists():
        print(f"No {renpy_games} directory found")
        return None

    try:
        dest = GAME_FOLDER.resolve()
    except Exception:  # pragma: no cover - defensive
        dest = GAME_FOLDER

    matches: list[Path] = []
    for item in renpy_games.rglob("*"):
        if not (item.is_dir() and item.name == "game"):
            continue
        try:
            resolved = item.resolve()
        except Exception:  # pragma: no cover - defensive
            resolved = item
        if resolved == dest:
            # Skip our own consolidated output folder on re-runs.
            continue
        matches.append(item)

    if not matches:
        return None

    matches.sort()
    return matches[0]


def find_game_folders() -> list[tuple[Path, Path, Path]]:
    r"""Backward-compatible wrapper around :func:`find_game_folder`.

    Returns a single-element list of ``(game_dir, rel_path, target_folder)``
    tuples for the first game folder found, mirroring the legacy API used by
    :mod:`converter.pipeline`.  Prefer :func:`find_game_folder` / :func:`process_game`
    for the generalized consolidated-folder flow.
    """
    game_folder = find_game_folder()
    if game_folder is None:
        return []
    game_dir = game_folder.parent
    rel_path = game_folder.relative_to(ROOT)
    return [(game_dir, rel_path, game_folder)]


def _should_copy(path: Path) -> bool:
    return path.suffix in COPY_EXTENSIONS or path.name in COPY_FILENAMES


def copy_game_files(
    game_folder: Path, dest: Path = GAME_FOLDER
) -> list[Path]:
    r"""Copy relevant files from ``game_folder`` into ``dest``.

    Relative directory structure is preserved, so
    ``game/archive.rpa`` lands at ``dest/archive.rpa`` and
    ``game/tl/None/common.rpyc`` lands at ``dest/tl/None/common.rpyc``.
    ``dest`` is recreated cleanly on every call.

    Returns the list of copied destination paths.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for path in sorted(game_folder.rglob("*")):
        if not path.is_file():
            continue
        if not _should_copy(path):
            continue
        rel = path.relative_to(game_folder)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)

    return copied


def extract_rpa_file(rpa_file: Path) -> None:
    r"""Extract a single ``.rpa`` alongside itself.

    ``game/archive.rpa`` -> ``game/archive/`` with the archive's contents.
    The original ``.rpa`` file is preserved.
    """
    from rpatool.archive import RenPyArchive

    try:
        extract_dir = rpa_file.parent / rpa_file.stem
        extract_dir.mkdir(parents=True, exist_ok=True)

        with RenPyArchive(rpa_file, verbose=False) as rpa:
            files = rpa.list()
            if not files:
                print(f"  ! Archive is empty: {rpa_file.name}")
                return

            print(f"  Extracting {rpa_file.name} -> {extract_dir}")
            count = 0
            for filename in files:
                data = rpa.read(filename)
                target = extract_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                count += 1

            print(f"  [OK] Extracted {count} file(s) from {rpa_file.name} "
                  f"into {extract_dir}")
    except Exception as e:  # noqa: BLE001
        print(f"  [XX] Failed to extract {rpa_file.name}: {e}")


def extract_all_rpa(game_folder: Path) -> list[dict[str, str]]:
    r"""Extract every ``.rpa`` under ``game_folder``.

    Returns a list of dicts with keys ``archive`` (relative path of the
    ``.rpa`` file) and ``extracted_folder`` (relative path to the extracted
    directory alongside it).
    """
    rpa_files = sorted(game_folder.rglob("*.rpa"))
    if not rpa_files:
        print(f"No .rpa files found in {game_folder}")
        return []

    print(f"Extracting {len(rpa_files)} .rpa file(s) into {game_folder}")
    extracted: list[dict[str, str]] = []
    for rpa_file in rpa_files:
        extract_dir = rpa_file.parent / rpa_file.stem
        extract_rpa_file(rpa_file)
        extracted.append({
            "archive": rpa_file.relative_to(game_folder).as_posix(),
            "extracted_folder": extract_dir.relative_to(game_folder).as_posix(),
        })
    return extracted


def decompile_rpyc_files(game_folder: Path) -> int:
    r"""Decompile every ``.rpyc`` under ``game_folder`` to a sibling ``.rpy``.

    Uses ``rpa-toolkit``.  Returns the number of files successfully
    decompiled.
    """
    from rpatool import decompile_rpyc_file

    rpyc_files = sorted(game_folder.rglob("*.rpyc"))
    if not rpyc_files:
        print(f"No .rpyc files found in {game_folder}")
        return 0

    print(f"Decompiling {len(rpyc_files)} .rpyc file(s) in {game_folder}")
    ok = err = fail = 0
    for rpyc_file in rpyc_files:
        try:
            output_path = rpyc_file.with_suffix(".rpy")
            result = decompile_rpyc_file(
                rpyc_file, output_path=output_path, overwrite=True
            )
            if result.success:
                print(f"  [OK] Decompiled: {rpyc_file.name} -> {output_path.name}")
                ok += 1
            else:
                print(f"  [ERR] Error decompiling {rpyc_file.name}: {result.error}")
                err += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [XX] Failed to decompile {rpyc_file.name}: {e}")
            fail += 1

    print(f"  Decompile done: {ok} ok, {err} error(s), {fail} failed")
    return ok


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


def write_config(
    game_folder: Path,
    source_game: str,
    source_path: Path,
    extracted_archives: list[dict[str, str]] | None = None,
    image_categories: dict | None = None,
    config_path: Path = CONFIG_PATH,
) -> Path:
    r"""Write ``config.json`` with matching patterns and an exclusion parameter.

    The config records every discovered ``.rpy`` and ``_ren.py`` file as a
    matching pattern, the extracted ``.rpa`` archive folders, the image
    category naming map, plus an ``exclusion`` block the user can populate
    with glob/substring patterns to exclude from downstream processing.
    """
    source_files = scan_source_files(game_folder)

    config = {
        "schema": 1,
        "game_folder": str(game_folder),
        "source_game": source_game,
        "source_path": str(source_path),
        "rpy_files": source_files["rpy"],
        "ren_py_files": source_files["ren_py"],
        "extracted_archives": extracted_archives or [],
        "image_categories": image_categories or {},
        "exclusion": {
            "patterns": [],
            "note": "Glob/substring patterns to exclude from downstream processing",
        },
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"  [OK] Wrote config: {config_path}")
    return config_path


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    r"""Load the generated ``config.json``.

    Returns the parsed JSON dict, or an empty dict if the file is missing
    or invalid (so callers can degrade gracefully).
    """
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
        print(f"  [WARN] Could not load config {config_path}: {exc}")
        return {}


def matches_exclusion(path: str, patterns: list[str]) -> bool:
    r"""Return True if ``path`` matches any exclusion pattern.

    A pattern matches if it is a substring of ``path`` (e.g. ``archive/``
    matches ``archive/cabins.rpy``) or if ``path`` glob-matches the pattern.
    Matching is case-sensitive on the path as stored (forward-slash, relative
    to the consolidated game folder).
    """
    if not patterns:
        return False
    for pattern in patterns:
        if not pattern:
            continue
        if pattern in path:
            return True
        try:
            if fnmatch.fnmatchcase(path, pattern):
                return True
        except Exception:  # pragma: no cover - defensive
            pass
    return False


def filter_source_files(
    config_path: Path = CONFIG_PATH,
) -> dict[str, list[str]]:
    r"""Load ``config.json`` and return source files minus excluded ones.

    Reads the ``rpy_files`` and ``ren_py_files`` matching patterns recorded
    by :func:`write_config` and removes any path that matches an exclusion
    pattern from ``config["exclusion"]["patterns"]``.

    Returns a dict with keys ``"rpy"``, ``"ren_py"`` and ``"extracted_archives"``.
    ``"extracted_archives"`` is the list of ``{"archive", "extracted_folder"}``
    dicts recorded by :func:`write_config`.  Returns empty lists if the config
    is missing.
    """
    config = load_config(config_path)
    rpy_files = config.get("rpy_files", [])
    ren_py_files = config.get("ren_py_files", [])
    extracted_archives = config.get("extracted_archives", []) or []
    exclusion = config.get("exclusion", {}) or {}
    patterns = exclusion.get("patterns", []) or []

    included_rpy = [p for p in rpy_files if not matches_exclusion(p, patterns)]
    included_ren_py = [p for p in ren_py_files if not matches_exclusion(p, patterns)]

    return {
        "rpy": included_rpy,
        "ren_py": included_ren_py,
        "extracted_archives": extracted_archives,
    }


def sync_exclusions(config_path: Path = CONFIG_PATH) -> dict:
    r"""Persist exclusions into ``config.json``.

    Reads ``config.json`` and removes any entry from ``rpy_files`` and
    ``ren_py_files`` that matches a pattern in ``exclusion.patterns``, then
    writes the updated config back.  This is the persistent counterpart of
    :func:`filter_source_files` (which only returns the filtered lists in
    memory).

    Returns a dict with the before/after counts for ``rpy`` and ``ren_py``.
    Returns an empty dict if the config is missing or invalid.
    """
    config = load_config(config_path)
    if not config:
        print(f"Config not found or invalid: {config_path}")
        return {}

    exclusion = config.get("exclusion", {}) or {}
    patterns = exclusion.get("patterns", []) or []

    before_rpy = config.get("rpy_files", [])
    before_ren_py = config.get("ren_py_files", [])

    config["rpy_files"] = [
        p for p in before_rpy if not matches_exclusion(p, patterns)
    ]
    config["ren_py_files"] = [
        p for p in before_ren_py if not matches_exclusion(p, patterns)
    ]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {
        "rpy_before": len(before_rpy),
        "rpy_after": len(config["rpy_files"]),
        "ren_py_before": len(before_ren_py),
        "ren_py_after": len(config["ren_py_files"]),
    }


def process_game(
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
    *,
    dry_run: bool = False,
) -> dict:
    r"""Run the full generalized decompile flow for one game.

    Steps:

      1. Find the first ``\game`` folder under ``renpy_games``.
      2. Copy relevant files into ``dest`` (``src/renpy_game/game``).
      3. Extract every ``.rpa`` alongside itself.
      4. Decompile every ``.rpyc`` into a sibling ``.rpy``.
      5. Write ``config.json`` with matching patterns + exclusion parameter.

    Returns a summary dict.
    """
    summary: dict = {
        "game_folder": None,
        "source_game": None,
        "source_path": None,
        "copied": 0,
        "extracted_archives": [],
        "decompiled": 0,
        "config": None,
        "dry_run": dry_run,
    }

    game_folder = find_game_folder(renpy_games)
    if game_folder is None:
        print(f"No \\game folder found under {renpy_games}")
        return summary

    game_dir = game_folder.parent
    summary["game_folder"] = str(game_folder)
    summary["source_game"] = game_dir.name
    summary["source_path"] = str(game_dir)

    print(f"\n=== Game folder: {game_folder} ===")
    print(f"Source game: {game_dir.name}")

    if dry_run:
        print(f"[dry-run] would copy relevant files into {dest}")
    else:
        copied = copy_game_files(game_folder, dest)
        summary["copied"] = len(copied)
        print(f"  [OK] Copied {len(copied)} file(s) into {dest}")
        game_folder = dest  # operate on the consolidated copy from here on

    if dry_run:
        print("[dry-run] would extract .rpa archives and decompile .rpyc files")
    else:
        extracted = extract_all_rpa(game_folder)
        summary["extracted_archives"] = extracted
        summary["decompiled"] = decompile_rpyc_files(game_folder)
        image_categories = scan_image_categories(game_folder)
        summary["image_categories"] = image_categories
        config_out = write_config(
            game_folder,
            source_game=game_dir.name,
            source_path=game_dir,
            extracted_archives=extracted,
            image_categories=image_categories,
            config_path=config_path,
        )
        summary["config"] = str(config_out)
        try:
            set_game_folder_rel_path(game_folder.relative_to(ROOT))
        except ValueError:
            set_game_folder_rel_path(game_folder)

    print("\n=== Decompile flow complete ===")
    return summary


def process_all_games() -> None:
    r"""Backward-compatible entry point: process the first game found."""
    process_game()


def restore_decompile(
    target_folder: Path, *, dry_run: bool = False
) -> dict[str, str]:
    r"""Undo a previous decompile of a game folder.

    Removes generated ``.rpy`` files that have a ``.rpyc`` sibling, and
    removes extracted archive directories (sibling dirs named after ``.rpa``
    stems), restoring the folder to its pre-decompile state.

    Returns a dict mapping path -> status
    (``"removed"``, ``"would_remove"``, or ``"skipped: ..."``).
    """
    results: dict[str, str] = {}

    # Remove generated .rpy files that have a corresponding .rpyc sibling.
    for rpyc_file in sorted(target_folder.rglob("*.rpyc")):
        rpy_file = rpyc_file.with_suffix(".rpy")
        if rpy_file.is_file():
            key = str(rpy_file)
            if dry_run:
                results[key] = "would_remove"
            else:
                try:
                    rpy_file.unlink()
                    results[key] = "removed"
                except Exception as exc:  # noqa: BLE001
                    results[key] = f"skipped: {exc}"

    # Remove extracted archive directories (sibling dirs named after .rpa stems).
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_dir = rpa_file.parent / rpa_file.stem
        if extract_dir.is_dir():
            key = str(extract_dir)
            if dry_run:
                results[key] = "would_remove"
            else:
                try:
                    shutil.rmtree(extract_dir)
                    results[key] = "removed"
                except Exception as exc:  # noqa: BLE001
                    results[key] = f"skipped: {exc}"

    return results


if __name__ == "__main__":
    process_game()

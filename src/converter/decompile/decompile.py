"""Decompile and extract Ren'Py games from src\renpy_game directory.

Provides reusable game folder location caching for other subpackages.
"""

import pathlib

# Cache for the game folder relative location, set after first lookup
_game_folder_rel_path: pathlib.Path | None = None


def set_game_folder_rel_path(rel_path: pathlib.Path) -> None:
    r"""Cache the relative path of the \game folder for other subpackages.

    This should be called after find_game_folders() to make the game folder
    location available to other converter subpackages.

    Args:
        rel_path: Path relative to the project root (src/), e.g.,
            Path("renpy_game/CampMourningWood-0.25.0.1/game")
    """
    global _game_folder_rel_path
    _game_folder_rel_path = rel_path


def get_game_folder_rel_path() -> pathlib.Path | None:
    r"""Get the cached relative path of the \game folder.

    Returns:
        The relative path to the \game folder if set, None otherwise.
        Other subpackages should call set_game_folder_rel_path() first.
    """
    return _game_folder_rel_path


ROOT = pathlib.Path("src")
RENPY_GAMES = ROOT / "renpy_game"


def find_game_folders():
    r"""Find all game folders under src/renpy_game.

    Looks for \game subfolder within each game directory under
    src/renpy_game. Caches the relative path of the first game folder
    found via set_game_folder_rel_path() for other subpackages to use.

    Returns:
        List of tuples: (game_dir, rel_path, target_folder)
        - game_dir: Absolute path to the game directory (e.g., src/renpy_game/my_game)
        - rel_path: Relative path from project root to the \game subfolder
        - target_folder: Absolute path to the \game subfolder itself
    """
    global _game_folder_rel_path

    if not RENPY_GAMES.exists():
        print(f"No {RENPY_GAMES} directory found")
        return []

    game_folders = []
    found_first = False

    for item in RENPY_GAMES.iterdir():
        if item.is_dir():
            # Look for \game subfolder within each game directory
            game_subfolder = item / "game"
            if game_subfolder.exists():
                rel_path = game_subfolder.relative_to(ROOT)
                game_folders.append((item, rel_path, game_subfolder))

                # Cache the relative path of the first game folder found
                # Other subpackages can call get_game_folder_rel_path() to retrieve it
                if not found_first:
                    set_game_folder_rel_path(rel_path)
                    found_first = True
            else:
                # No \game subfolder, use the game directory itself
                rel_path = item.relative_to(ROOT)
                game_folders.append((item, rel_path, item))

    return game_folders


def decompile_rpyc_files(game_folder: pathlib.Path):
    """Decompile all .rpyc files in the game folder.

    Uses rpa-toolkit to decompile .rpyc files to .rpy source code.
    """
    from rpatool import decompile_rpyc_file

    rpyc_files = list(game_folder.rglob("*.rpyc"))

    if not rpyc_files:
        print(f"No .rpyc files found in {game_folder}")
        return

    print(f"Decompiling {len(rpyc_files)} .rpyc files in {game_folder}")

    for rpyc_file in rpyc_files:
        try:
            output_path = rpyc_file.with_suffix(".rpy")
            result = decompile_rpyc_file(rpyc_file, output_path=output_path, overwrite=True)
            if result.success:
                print(f"  [OK] Decompiled: {rpyc_file.name} -> {output_path.name}")
            else:
                print(f"  [ERR] Error decompiling {rpyc_file.name}: {result.error}")
        except Exception as e:
            print(f"  [XX] Failed to decompile {rpyc_file.name}: {e}")


def extract_rpa_file(rpa_file: pathlib.Path):
    """Extract a single .rpa file into a directory alongside the .rpa file.

    Extracts Ren'Py RPA archive contents into a directory named after the .rpa file,
    placed in the same directory as the .rpa file. This preserves the relative path
    structure for easier mapping later.

    For example, if the .rpa file is at:
        src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive.rpa
    The extracted directory will be at:
        src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game/archive/

    Args:
        rpa_file: Path to the .rpa file to extract
    """
    from pathlib import Path
    from rpatool.archive import RenPyArchive

    try:
        # The extracted directory goes in the same directory as the .rpa file,
        # named after the .rpa file stem (without .rpa extension)
        extract_dir = rpa_file.parent / rpa_file.stem

        # Create the directory if it doesn't exist
        extract_dir.mkdir(parents=True, exist_ok=True)

        with RenPyArchive(rpa_file, verbose=False) as rpa:
            files = rpa.list()
            if not files:
                print(f"  ! Archive is empty: {rpa_file.name}")
                return

            print(f"  Extracting {rpa_file.name} -> {extract_dir}")

            extracted_count = 0
            for filename in files:
                # Extract each file from the archive
                data = rpa.read(filename)
                target = extract_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                extracted_count += 1

            print(f"  [OK] Extracted {extracted_count} files from {rpa_file.name} "
                  f"into {extract_dir}")
    except Exception as e:
        print(f"  [XX] Failed to extract {rpa_file.name}: {e}")


def process_all_games():
    """Process all games: decompile .rpyc and extract .rpa.

    .rpa files are extracted into a directory alongside the .rpa file,
    preserving the relative path structure for easier mapping later.
    """
    game_folders = find_game_folders()

    if not game_folders:
        print("No game folders found.")
        return

    for game_dir, rel_path, target_folder in game_folders:
        print(f"\n=== Processing game at relative path: {rel_path} ===")
        print(f"Target folder: {target_folder}")

        # Decompile .rpyc files in the target folder
        decompile_rpyc_files(target_folder)

        # Extract .rpa files in the target folder
        # Extract alongside the .rpa file, preserving directory structure
        rpa_files = list(target_folder.rglob("*.rpa"))

        if not rpa_files:
            print(f"No .rpa files found in {target_folder}")
        else:
            print(f"Extracting {len(rpa_files)} .rpa files into "
                  f"{target_folder} (alongside each .rpa file)")
            for rpa_file in rpa_files:
                extract_rpa_file(rpa_file)

    print("\n=== Done processing all games ===")


def restore_decompile(
    target_folder: pathlib.Path, *, dry_run: bool = False
) -> dict[str, str]:
    """Undo a previous decompile of a game folder.

    Removes the outputs produced by :func:`decompile_rpyc_files` and
    :func:`extract_rpa_file`, restoring the game to its original compiled
    state (``.rpyc`` files and ``.rpa`` archives):

    * ``.rpy`` files that have a corresponding ``.rpyc`` sibling (generated by
      decompile) are removed.
    * Extracted archive directories (a directory named after a ``.rpa`` stem,
      created alongside the ``.rpa`` file) are removed.

    Args:
        target_folder: The ``\\game`` folder to restore.
        dry_run: If True, report what would be removed without deleting.

    Returns:
        A dict mapping removed paths to a status string
        (``"removed"``, ``"would_remove"``, or ``"skipped"``).
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
                    import shutil

                    shutil.rmtree(extract_dir)
                    results[key] = "removed"
                except Exception as exc:  # noqa: BLE001
                    results[key] = f"skipped: {exc}"

    return results


if __name__ == "__main__":
    process_all_games()
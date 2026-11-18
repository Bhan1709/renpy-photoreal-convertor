"""RPA archive extraction."""

from __future__ import annotations

from pathlib import Path

from cli import counted, error, info, loading, success


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
                info(f"Archive is empty: {rpa_file.name}")
                return

            info(f"Extracting {rpa_file.name} -> {extract_dir}")
            with loading(f"Extracting {rpa_file.name}..."):
                count = 0
                for filename in files:
                    data = rpa.read(filename)
                    target = extract_dir / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    count += 1

        success(
            f"Extracted {counted(count, 'file')} from {rpa_file.name} "
            f"into {extract_dir}."
        )
    except Exception as e:  # noqa: BLE001
        error(f"Failed to extract {rpa_file.name}: {e}")


def extract_all_rpa(game_folder: Path) -> list[dict[str, str]]:
    r"""Extract every ``.rpa`` under ``game_folder``.

    Returns a list of dicts with keys ``archive`` (relative path of the
    ``.rpa`` file) and ``extracted_folder`` (relative path to the extracted
    directory alongside it).
    """
    rpa_files = sorted(game_folder.rglob("*.rpa"))
    if not rpa_files:
        info(f"No .rpa files found in {game_folder}")
        return []

    info(f"Extracting {counted(len(rpa_files), '.rpa file')} into {game_folder}")
    extracted: list[dict[str, str]] = []
    for rpa_file in rpa_files:
        extract_dir = rpa_file.parent / rpa_file.stem
        extract_rpa_file(rpa_file)
        extracted.append(
            {
                "archive": rpa_file.relative_to(game_folder).as_posix(),
                "extracted_folder": extract_dir.relative_to(game_folder).as_posix(),
            }
        )
    return extracted

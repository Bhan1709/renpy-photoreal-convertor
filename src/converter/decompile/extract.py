"""RPA archive extraction with structured, inspectable results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli import counted, error, info, loading, success


@dataclass(frozen=True)
class ArchiveExtraction:
    """Outcome of extracting one Ren'Py archive."""

    archive: Path
    extracted_folder: Path
    files_extracted: int = 0
    error: str | None = None

    @property
    def success(self) -> bool:
        """Return whether the archive was extracted without an error."""
        return self.error is None

    def metadata(self, game_folder: Path) -> dict[str, str]:
        """Return JSON-compatible metadata relative to ``game_folder``."""
        root = game_folder.resolve()
        archive = self.archive.resolve()
        extracted = self.extracted_folder.resolve()
        return {
            "archive": archive.relative_to(root).as_posix(),
            "extracted_folder": extracted.relative_to(root).as_posix(),
        }


def _safe_member_target(extract_dir: Path, filename: str) -> Path:
    """Resolve an archive member and reject paths outside the extraction root."""
    member = Path(filename)
    if member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe archive member path: {filename!r}")

    target = (extract_dir / member).resolve()
    root = extract_dir.resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"archive member escapes extraction root: {filename!r}")
    return target


def extract_rpa_file_detailed(rpa_file: Path) -> ArchiveExtraction:
    r"""Extract a single ``.rpa`` alongside itself and return its outcome.

    ``game/archive.rpa`` -> ``game/archive/`` with the archive's contents.
    The original ``.rpa`` file is preserved.  Failures are returned in the
    result rather than being silently reported as successful extraction.
    """
    from rpatool.archive import RenPyArchive

    extract_dir = (rpa_file.parent / rpa_file.stem).resolve()
    count = 0
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)

        with RenPyArchive(rpa_file, verbose=False) as rpa:
            files = rpa.list()
            if not files:
                info(f"Archive is empty: {rpa_file.name}")
                return ArchiveExtraction(rpa_file, extract_dir)

            info(f"Extracting {rpa_file.name} -> {extract_dir}")
            with loading(f"Extracting {rpa_file.name}..."):
                for filename in files:
                    data = rpa.read(filename)
                    target = _safe_member_target(extract_dir, filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    count += 1

        success(
            f"Extracted {counted(count, 'file')} from {rpa_file.name} "
            f"into {extract_dir}."
        )
        return ArchiveExtraction(rpa_file, extract_dir, files_extracted=count)
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to extract {rpa_file.name}: {exc}")
        return ArchiveExtraction(
            rpa_file,
            extract_dir,
            files_extracted=count,
            error=str(exc),
        )


def extract_rpa_file(rpa_file: Path) -> None:
    """Extract a single archive using the legacy fire-and-report API."""
    extract_rpa_file_detailed(rpa_file)


def extract_all_rpa_detailed(game_folder: Path) -> list[ArchiveExtraction]:
    r"""Extract every ``.rpa`` under ``game_folder`` with structured results.

    Returns one result per archive so the caller can distinguish successful
    extraction from failure instead of recording failed archives as
    successfully extracted.
    """
    rpa_files = sorted(game_folder.rglob("*.rpa"))
    if not rpa_files:
        info(f"No .rpa files found in {game_folder}")
        return []

    info(f"Extracting {counted(len(rpa_files), '.rpa file')} into {game_folder}")
    return [extract_rpa_file_detailed(rpa_file) for rpa_file in rpa_files]


def extract_all_rpa(game_folder: Path) -> list[dict[str, str]]:
    r"""Extract every ``.rpa`` and return legacy metadata dictionaries.

    Failed archives are omitted.  New orchestration code should use
    :func:`extract_all_rpa_detailed` to retain failure details.
    """
    return [
        extraction.metadata(game_folder)
        for extraction in extract_all_rpa_detailed(game_folder)
        if extraction.success
    ]


__all__ = [
    "ArchiveExtraction",
    "extract_all_rpa",
    "extract_all_rpa_detailed",
    "extract_rpa_file",
    "extract_rpa_file_detailed",
]

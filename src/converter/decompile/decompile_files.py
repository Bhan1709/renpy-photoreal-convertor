"""Ren'Py script decompilation with structured per-file results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli import counted, error, info, loading, success


@dataclass(frozen=True)
class DecompileFileResult:
    """Outcome of decompiling one compiled Ren'Py script."""

    source: Path
    output: Path
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class DecompileFilesResult:
    """Aggregate outcome of decompiling all compiled scripts in a folder."""

    files: tuple[DecompileFileResult, ...] = ()

    @property
    def succeeded(self) -> int:
        """Return the number of successfully decompiled files."""
        return sum(1 for result in self.files if result.success)

    @property
    def failed(self) -> int:
        """Return the number of failed decompile operations."""
        return sum(1 for result in self.files if not result.success)


def _compiled_scripts(game_folder: Path) -> list[Path]:
    """Return supported compiled script files in deterministic order."""
    scripts = list(game_folder.rglob("*.rpyc"))
    scripts.extend(game_folder.rglob("*.rpymc"))
    return sorted(scripts)


def _output_path(source: Path) -> Path:
    """Return the readable output path corresponding to a compiled script."""
    suffix = ".rpym" if source.suffix.lower() == ".rpymc" else ".rpy"
    return source.with_suffix(suffix)


def decompile_rpyc_files_detailed(game_folder: Path) -> DecompileFilesResult:
    """Decompile every supported compiled script under ``game_folder``."""
    from rpatool import decompile_rpyc_file

    compiled_scripts = _compiled_scripts(game_folder)
    if not compiled_scripts:
        info(f"No .rpyc or .rpymc files found in {game_folder}")
        return DecompileFilesResult()

    info(
        f"Decompiling {counted(len(compiled_scripts), 'compiled script')} "
        f"in {game_folder}"
    )
    results: list[DecompileFileResult] = []
    for compiled_script in compiled_scripts:
        output_path = _output_path(compiled_script)
        try:
            with loading(f"Decompiling {compiled_script.name}..."):
                result = decompile_rpyc_file(
                    compiled_script,
                    output_path=output_path,
                    overwrite=True,
                )
            if result.success:
                success(
                    f"Decompiled: {compiled_script.name} -> {output_path.name}"
                )
                results.append(
                    DecompileFileResult(compiled_script, output_path, success=True)
                )
            else:
                message = str(result.error or "unknown decompile error")
                error(f"Could not decompile {compiled_script.name}: {message}")
                results.append(
                    DecompileFileResult(
                        compiled_script,
                        output_path,
                        success=False,
                        error=message,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            error(f"Failed to decompile {compiled_script.name}: {exc}")
            results.append(
                DecompileFileResult(
                    compiled_script,
                    output_path,
                    success=False,
                    error=str(exc),
                )
            )

    succeeded = sum(1 for result in results if result.success)
    failed = len(results) - succeeded
    success(
        f"Decompile complete: {succeeded} succeeded, "
        f"{counted(failed, 'error')} failed."
    )
    return DecompileFilesResult(files=tuple(results))


def decompile_rpyc_files(game_folder: Path) -> int:
    r"""Decompile every compiled script and return the success count.

    This compatibility wrapper retains the original integer API.  New code
    should use :func:`decompile_rpyc_files_detailed` when it needs per-file
    failure details.
    """
    return decompile_rpyc_files_detailed(game_folder).succeeded


__all__ = [
    "DecompileFileResult",
    "DecompileFilesResult",
    "decompile_rpyc_files",
    "decompile_rpyc_files_detailed",
]

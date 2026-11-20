"""Discovery of a bundled Ren'Py runtime and its native launcher."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import NativeDiscoveryError

_VERSION_RE = re.compile(r"version\s*=\s*['\"]([^'\"]+)['\"]")


@dataclass(frozen=True)
class RuntimeLayout:
    """A validated Ren'Py runtime and the command used to launch it."""

    root: Path
    launcher: Path
    version: str
    python_executable: Path | None
    use_shell_launcher: bool

    def command(
        self,
        *,
        basedir: Path,
        command: str,
        savedir: Path,
    ) -> list[str]:
        """Build the native command line for one compose-stage invocation.

        The bundled interpreter is preferred over the shell launcher because it
        keeps stdout/stderr attached to the captured pipes and avoids the
        GUI-subsystem launcher swallowing diagnostics.
        """
        arguments = [str(basedir), command, "--savedir", str(savedir)]
        if self.python_executable is not None:
            return [
                str(self.python_executable),
                str(self.launcher),
                *arguments,
            ]
        if self.use_shell_launcher:
            shell = self.launcher.with_suffix(".exe" if os.name == "nt" else ".sh")
            return [str(shell), *arguments]
        raise NativeDiscoveryError(
            f"No usable launcher was found for {self.launcher}"
        )


def discover_runtime(vn_root: Path) -> RuntimeLayout:
    """Discover the bundled Ren'Py runtime rooted at ``vn_root``.

    ``vn_root`` is the Ren'Py project/base directory containing ``renpy/`` and
    ``lib/``.  The game itself may be supplied separately to the stage.
    """
    root = Path(vn_root).resolve()
    if not root.is_dir():
        raise NativeDiscoveryError(f"Ren'Py runtime directory does not exist: {root}")

    renpy_dir = root / "renpy"
    lib_dir = root / "lib"
    if not renpy_dir.is_dir():
        raise NativeDiscoveryError(
            f"Ren'Py runtime is missing its 'renpy' directory: {root}"
        )
    if not lib_dir.is_dir():
        raise NativeDiscoveryError(
            f"Ren'Py runtime is missing its 'lib' directory: {root}"
        )

    launcher = _find_launcher(root)
    version = _read_version(renpy_dir)
    python_executable = _find_python(root)
    use_shell_launcher = _should_use_shell_launcher(launcher)

    return RuntimeLayout(
        root=root,
        launcher=launcher,
        version=version,
        python_executable=python_executable,
        use_shell_launcher=use_shell_launcher,
    )


def _find_launcher(root: Path) -> Path:
    candidates = sorted(root.glob("*.py"))
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "renpy.bootstrap.bootstrap" in text:
            return candidate

    if candidates:
        return candidates[0]

    raise NativeDiscoveryError(
        f"No Ren'Py Python launcher was found in {root}"
    )


def _read_version(renpy_dir: Path) -> str:
    version_file = renpy_dir / "vc_version.py"
    try:
        text = version_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    match = _VERSION_RE.search(text)
    return match.group(1) if match else "unknown"


def _find_python(root: Path) -> Path | None:
    lib = root / "lib"
    patterns: list[str]
    if sys.platform == "win32":
        patterns = [
            "py3-windows-x86_64/python.exe",
            "py2-windows-x86_64/python.exe",
            "py3-windows-i686/python.exe",
            "py2-windows-i686/python.exe",
        ]
    elif sys.platform == "darwin":
        patterns = [
            "py3-mac-universal/python",
            "py2-mac-universal/python",
        ]
    else:
        patterns = [
            "py3-linux-x86_64/python",
            "py2-linux-x86_64/python",
            "py3-linux-i686/python",
            "py2-linux-i686/python",
        ]

    for relative in patterns:
        candidate = lib / relative
        if candidate.is_file():
            return candidate

    for candidate in sorted(lib.glob("py*/python*")):
        if candidate.is_file() and candidate.suffix.lower() in {"", ".exe"}:
            return candidate
    return None


def _should_use_shell_launcher(launcher: Path) -> bool:
    if os.name == "nt":
        executable = launcher.with_suffix(".exe")
        return executable.is_file()

    shell_launcher = launcher.with_suffix(".sh")
    return shell_launcher.is_file()


__all__ = ["RuntimeLayout", "discover_runtime"]

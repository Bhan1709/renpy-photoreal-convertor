"""Subprocess execution for native Ren'Py compose batches."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import NativeExecutionError
from .constants import DEFAULT_PROCESS_TIMEOUT_SECONDS, DEFAULT_RENDERER
from .discovery import RuntimeLayout

_PROGRESS_RE = re.compile(r"render (\d+)/\d+")
_BATCH_START_RE = re.compile(r"batch \d+ start offset=\d+ selected=\d+ total=(\d+)")

# How often (seconds) the host polls the child's progress log while a batch runs.
_POLL_INTERVAL_SECONDS: float = 0.2


@dataclass(frozen=True)
class ProcessResult:
    """Captured output from one native Ren'Py compose subprocess."""

    returncode: int
    stdout: str
    stderr: str
    command: tuple[str, ...]


def _require_workspace_path(
    path: Path,
    basedir: Path,
    *,
    label: str,
) -> None:
    """Require one native runtime path to stay inside its disposable root."""
    try:
        path.resolve().relative_to(basedir.resolve())
    except ValueError as exc:
        raise NativeExecutionError(
            f"Native {label} must be inside the disposable workspace: {path}"
        ) from exc


def run_native_batch(
    *,
    layout: RuntimeLayout,
    basedir: Path,
    savedir: Path,
    config_path: Path,
    result_path: Path,
    command: str,
    timeout: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    progress_path: Path | str | None = None,
    progress_cb: Callable[[int], None] | None = None,
    total_cb: Callable[[int], None] | None = None,
) -> ProcessResult:
    """Run one headless Ren'Py compose batch and capture its complete output.

    Unlike the previous implementation, this does not block on
    ``subprocess.communicate()`` for the whole batch.  Instead it polls the
    child's progress log while the process runs and invokes ``progress_cb``
    with the number of states rendered so far, so a host-side progress bar can
    advance in real time.

    Stdout/stderr are written to files inside ``basedir`` rather than captured
    through pipes, to avoid pipe-buffer deadlock while the host polls.
    """
    command_line = layout.command(
        basedir=basedir,
        command=command,
        savedir=savedir,
    )
    _require_workspace_path(result_path, basedir, label="result file")
    _require_workspace_path(config_path, basedir, label="configuration file")
    _require_workspace_path(savedir, basedir, label="save directory")
    environment = os.environ.copy()
    environment.pop("RENPY_SEARCHPATH", None)
    environment["RENPY_RENDERER"] = DEFAULT_RENDERER
    environment["SDL_VIDEODRIVER"] = "dummy"
    environment["SDL_AUDIODRIVER"] = "dummy"
    environment["PYGAME_SDL2_AVOID_GL"] = "1"
    environment["RENPY_LOG_BASE"] = str(basedir / "logs")
    environment["RENPY_PATH_TO_SAVES"] = str(basedir / "save-root")
    environment["RENPY_MULTIPERSISTENT"] = str(basedir / "multipersistent")
    environment["RENPY_DISABLE_BACKUPS"] = (
        "I take responsibility for this."
    )
    environment["RENPY_COMPOSITE_CONFIG"] = str(config_path)
    environment["RENPY_COMPOSITE_OUTPUT"] = str(result_path)
    environment["PYTHONIOENCODING"] = "utf-8"

    # Keep every write inside the disposable workspace so cleanup removes it.
    for name in ("logs", "save-root", "multipersistent"):
        (basedir / name).mkdir(parents=True, exist_ok=True)

    # Never let a child inherit a result from an earlier attempt.  The native
    # stage normally clears this path too; doing it here protects direct
    # callers of the subprocess helper as well.  If the stale file cannot be
    # removed, abort before launching the child rather than risk accepting an
    # unrelated result.
    try:
        result_path.unlink(missing_ok=True)
    except OSError as exc:
        raise NativeExecutionError(
            f"Could not clear native result file before launch: {result_path}: {exc}"
        ) from exc

    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    stdout_path = basedir / "batch_stdout.log"
    stderr_path = basedir / "batch_stderr.log"

    with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
        if os.name == "nt":
            process = subprocess.Popen(
                command_line,
                cwd=str(basedir),
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creation_flags,
            )
        else:
            # A dedicated process group lets timeout cleanup terminate the
            # complete Ren'Py child tree without signalling the host group.
            process = subprocess.Popen(
                command_line,
                cwd=str(basedir),
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )

        try:
            start_time = time.monotonic()
            pp = Path(progress_path) if progress_path else None
            # Seed last_size with any pre-existing progress-log content so we
            # only report progress produced by this batch.
            last_size = pp.stat().st_size if pp and pp.is_file() else 0
            last_rendered = 0
            last_reported = 0

            while True:
                if process.poll() is not None:
                    break

                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    _terminate_process_tree(process)
                    stdout = (
                        stdout_path.read_text(encoding="utf-8", errors="replace")
                        if stdout_path.is_file()
                        else ""
                    )
                    stderr = (
                        stderr_path.read_text(encoding="utf-8", errors="replace")
                        if stderr_path.is_file()
                        else ""
                    )
                    raise NativeExecutionError(
                        f"Native Ren'Py compose batch timed out after {timeout} seconds.\n"
                        f"Command: {_format_command(command_line)}\n"
                        f"{stdout}\n{stderr}"
                    )

                if pp is not None and pp.is_file():
                    try:
                        size = pp.stat().st_size
                        if size > last_size:
                            text = pp.read_text(encoding="utf-8", errors="replace")
                            last_size = size
                            for line in text.splitlines():
                                match = _PROGRESS_RE.search(line)
                                if match:
                                    last_rendered = max(
                                        last_rendered, int(match.group(1))
                                    )
                                batch_match = _BATCH_START_RE.search(line)
                                if batch_match and total_cb is not None:
                                    total_cb(int(batch_match.group(1)))
                            if (
                                progress_cb is not None
                                and last_rendered > last_reported
                            ):
                                progress_cb(last_rendered)
                                last_reported = last_rendered
                    except OSError:
                        pass

                time.sleep(_POLL_INTERVAL_SECONDS)

            # One final read after the process exits, in case the last progress
            # line was written between the last poll and process exit.
            if pp is not None and pp.is_file():
                try:
                    text = pp.read_text(encoding="utf-8", errors="replace")
                    for line in text.splitlines():
                        match = _PROGRESS_RE.search(line)
                        if match:
                            last_rendered = max(
                                last_rendered, int(match.group(1))
                            )
                        batch_match = _BATCH_START_RE.search(line)
                        if batch_match and total_cb is not None:
                            total_cb(int(batch_match.group(1)))
                    if progress_cb is not None and last_rendered > last_reported:
                        progress_cb(last_rendered)
                except OSError:
                    pass
        except BaseException:
            _terminate_process_tree(process)
            raise

    # Files are closed; read their contents from disk.
    stdout = (
        stdout_path.read_text(encoding="utf-8", errors="replace")
        if stdout_path.is_file()
        else ""
    )
    stderr = (
        stderr_path.read_text(encoding="utf-8", errors="replace")
        if stderr_path.is_file()
        else ""
    )

    result = ProcessResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        command=tuple(command_line),
    )
    if result.returncode != 0:
        raise NativeExecutionError(
            f"Native Ren'Py compose batch failed with exit code "
            f"{result.returncode}.\n"
            f"Command: {_format_command(command_line)}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except (OSError, ProcessLookupError, AttributeError):
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)


__all__ = ["ProcessResult", "run_native_batch"]

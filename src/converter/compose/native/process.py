"""Subprocess execution for native Ren'Py compose batches."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from ..errors import NativeExecutionError
from .constants import (
    DEFAULT_PROCESS_TIMEOUT_SECONDS,
    DEFAULT_RENDERER,
    NATIVE_WORKER_COMMAND,
    NATIVE_WORKER_STARTUP_TIMEOUT_SECONDS,
)
from .discovery import RuntimeLayout

_PROGRESS_RE = re.compile(r"render (\d+)/\d+")
_BATCH_START_RE = re.compile(r"batch \d+ start offset=\d+ selected=\d+ total=(\d+)")

# How often (seconds) the host polls the child's progress log while a batch runs.
_POLL_INTERVAL_SECONDS: float = 0.2

# The Ren'Py child executes the game's own Python code.  Keep its environment
# explicit so API keys, tokens, Python import paths, and unrelated Ren'Py
# settings are not inherited from the converter host.
_NATIVE_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_ARCHITEW6432",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    }
)


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


def _native_environment(
    *,
    basedir: Path,
    savedir: Path,
    config_path: Path,
    result_path: Path,
) -> dict[str, str]:
    """Build the isolated environment for one native Ren'Py process."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _NATIVE_ENV_ALLOWLIST
    }
    environment["RENPY_RENDERER"] = DEFAULT_RENDERER
    environment["SDL_VIDEODRIVER"] = "dummy"
    environment["SDL_AUDIODRIVER"] = "dummy"
    environment["PYGAME_SDL2_AVOID_GL"] = "1"
    environment["RENPY_LOG_BASE"] = str(basedir / "logs")
    environment["RENPY_PATH_TO_SAVES"] = str(basedir / "save-root")
    environment["RENPY_MULTIPERSISTENT"] = str(basedir / "multipersistent")
    environment["RENPY_DISABLE_BACKUPS"] = "I take responsibility for this."
    environment["RENPY_COMPOSITE_CONFIG"] = str(config_path)
    environment["RENPY_COMPOSITE_OUTPUT"] = str(result_path)
    environment["TEMP"] = str(basedir / "tmp")
    environment["TMP"] = str(basedir / "tmp")
    environment["TMPDIR"] = str(basedir / "tmp")
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _is_link_like(path: Path) -> bool:
    """Return whether one path is a symbolic link or Windows junction."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def _clear_worker_files(directory: Path) -> None:
    """Remove stale flat worker files without following directory aliases."""
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise NativeExecutionError(
            f"Could not inspect native worker directory {directory}: {exc}"
        ) from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=False):
                continue
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise NativeExecutionError(
                f"Could not clear stale native worker file {path}: {exc}"
            ) from exc


def _prepare_runtime_directories(basedir: Path) -> None:
    """Keep every child write inside the disposable workspace."""
    for name in ("logs", "save-root", "multipersistent", "tmp"):
        (basedir / name).mkdir(parents=True, exist_ok=True)


def _launch_process(
    command_line: list[str],
    *,
    basedir: Path,
    environment: dict[str, str],
    stdout_file: Any,
    stderr_file: Any,
) -> subprocess.Popen:
    """Launch one native process in its own process group."""
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            command_line,
            cwd=str(basedir),
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=creation_flags,
        )

    # A dedicated process group lets timeout cleanup terminate the complete
    # Ren'Py child tree without signalling the host group.
    return subprocess.Popen(
        command_line,
        cwd=str(basedir),
        env=environment,
        stdout=stdout_file,
        stderr=stderr_file,
        start_new_session=True,
    )


def _read_progress(
    progress_path: Path | None,
    *,
    last_size: int,
    last_rendered: int,
    last_reported: int,
    progress_cb: Callable[[int], None] | None,
    total_cb: Callable[[int], None] | None,
) -> tuple[int, int, int]:
    """Read newly appended progress lines and return polling state."""
    if progress_path is None or not progress_path.is_file():
        return last_size, last_rendered, last_reported
    try:
        size = progress_path.stat().st_size
        if size <= last_size:
            return last_size, last_rendered, last_reported
        text = progress_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return last_size, last_rendered, last_reported

    for line in text.splitlines():
        match = _PROGRESS_RE.search(line)
        if match:
            last_rendered = max(last_rendered, int(match.group(1)))
        batch_match = _BATCH_START_RE.search(line)
        if batch_match and total_cb is not None:
            total_cb(int(batch_match.group(1)))
    if progress_cb is not None and last_rendered > last_reported:
        progress_cb(last_rendered)
        last_reported = last_rendered
    return size, last_rendered, last_reported


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
    progress_file = Path(progress_path) if progress_path else None
    if progress_file is not None:
        _require_workspace_path(
            progress_file,
            basedir,
            label="progress file",
        )
        if _is_link_like(progress_file):
            raise NativeExecutionError(
                f"Native progress file must not be a symbolic link or junction: "
                f"{progress_file}"
            )
    environment = _native_environment(
        basedir=basedir,
        savedir=savedir,
        config_path=config_path,
        result_path=result_path,
    )
    _prepare_runtime_directories(basedir)

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

    stdout_path = basedir / "batch_stdout.log"
    stderr_path = basedir / "batch_stderr.log"

    with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
        process = _launch_process(
            command_line,
            basedir=basedir,
            environment=environment,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
        )

        try:
            start_time = time.monotonic()
            pp = progress_file
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

                last_size, last_rendered, last_reported = _read_progress(
                    pp,
                    last_size=last_size,
                    last_rendered=last_rendered,
                    last_reported=last_reported,
                    progress_cb=progress_cb,
                    total_cb=total_cb,
                )
                time.sleep(_POLL_INTERVAL_SECONDS)

            # One final read after the process exits, in case the last progress
            # line was written between the last poll and process exit.
            last_size, last_rendered, last_reported = _read_progress(
                pp,
                last_size=last_size,
                last_rendered=last_rendered,
                last_reported=last_reported,
                progress_cb=progress_cb,
                total_cb=total_cb,
            )
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


class PersistentNativeWorker:
    """One long-lived Ren'Py process that renders queued native jobs.

    Jobs and completion markers are exchanged through files inside the
    disposable workspace.  File boundaries make each job independently
    identifiable and allow the host to fall back to the one-shot subprocess
    path if this optional worker mode is unavailable in a runtime.
    """

    def __init__(
        self,
        *,
        layout: RuntimeLayout,
        basedir: Path,
        savedir: Path,
        timeout: int = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> None:
        self.layout = layout
        self.basedir = basedir
        self.savedir = savedir
        self.timeout = timeout
        self.root = basedir / "worker"
        self.jobs_dir = self.root / "jobs"
        self.results_dir = self.root / "results"
        self.stop_path = self.root / "stop"
        self.ready_path = self.root / "ready"
        self.config_path = self.root / "worker-config.json"
        self.stdout_path = basedir / "worker_stdout.log"
        self.stderr_path = basedir / "worker_stderr.log"
        self.process: subprocess.Popen | None = None
        self._stdout_file: Any = None
        self._stderr_file: Any = None
        self._submission_sequence = 0

    def start(self) -> None:
        """Start the worker process and wait for it to enter its job loop."""
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.process = None
            self._close_output_files()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _prepare_runtime_directories(self.basedir)
        _require_workspace_path(self.config_path, self.basedir, label="worker config")
        _require_workspace_path(self.jobs_dir, self.basedir, label="worker jobs")
        _require_workspace_path(self.results_dir, self.basedir, label="worker results")
        _require_workspace_path(self.stop_path, self.basedir, label="worker stop file")
        _require_workspace_path(self.ready_path, self.basedir, label="worker ready file")
        for path in (
            self.config_path,
            self.jobs_dir,
            self.results_dir,
            self.stdout_path,
            self.stderr_path,
        ):
            if _is_link_like(path):
                raise NativeExecutionError(
                    "Native worker path must not be a symbolic link or junction: "
                    f"{path}"
                )
        for marker in (self.stop_path, self.ready_path):
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                raise NativeExecutionError(
                    f"Could not clear native worker marker {marker}: {exc}"
                ) from exc
        # A restarted worker must not replay a stale queue entry.  Submission
        # names also carry a unique nonce, so this cleanup is defense in depth
        # rather than the only replay guard.
        _clear_worker_files(self.jobs_dir)
        _clear_worker_files(self.results_dir)

        config = {
            "jobs_dir": str(self.jobs_dir),
            "results_dir": str(self.results_dir),
            "stop_path": str(self.stop_path),
            "ready_path": str(self.ready_path),
        }
        self.config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        command_line = self.layout.command(
            basedir=self.basedir,
            command=NATIVE_WORKER_COMMAND,
            savedir=self.savedir,
        )
        environment = _native_environment(
            basedir=self.basedir,
            savedir=self.savedir,
            config_path=self.config_path,
            result_path=self.results_dir / "worker-output.json",
        )
        # The worker owns these handles for its entire lifetime, so they
        # intentionally outlive this method's local scope.
        self._stdout_file = open(self.stdout_path, "wb")  # noqa: SIM115
        self._stderr_file = open(self.stderr_path, "wb")  # noqa: SIM115
        try:
            self.process = _launch_process(
                command_line,
                basedir=self.basedir,
                environment=environment,
                stdout_file=self._stdout_file,
                stderr_file=self._stderr_file,
            )
        except Exception:
            self._close_output_files()
            raise

        deadline = time.monotonic() + NATIVE_WORKER_STARTUP_TIMEOUT_SECONDS
        while not self.ready_path.is_file():
            if self.process.poll() is not None:
                stdout = self._read_output(self.stdout_path)
                stderr = self._read_output(self.stderr_path)
                self.abort()
                raise NativeExecutionError(
                    "Native Ren'Py persistent worker exited before becoming "
                    f"ready.\n{stdout}\n{stderr}"
                )
            if time.monotonic() >= deadline:
                stdout = self._read_output(self.stdout_path)
                stderr = self._read_output(self.stderr_path)
                self.abort()
                raise NativeExecutionError(
                    "Native Ren'Py persistent worker did not become ready within "
                    f"{NATIVE_WORKER_STARTUP_TIMEOUT_SECONDS} seconds.\n"
                    f"{stdout}\n{stderr}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    def run_job(
        self,
        job_id: int,
        config: dict[str, Any],
        *,
        progress_path: Path,
        timeout: int | None = None,
        progress_cb: Callable[[int], None] | None = None,
        total_cb: Callable[[int], None] | None = None,
    ) -> Path:
        """Submit one job and wait for its completion marker."""
        if self.process is None:
            self.start()
        assert self.process is not None
        _require_workspace_path(
            progress_path,
            self.basedir,
            label="progress file",
        )
        if _is_link_like(progress_path):
            raise NativeExecutionError(
                "Native progress file must not be a symbolic link or junction: "
                f"{progress_path}"
            )
        if job_id < 0:
            raise NativeExecutionError(
                f"Native worker job_id must be non-negative: {job_id}"
            )

        self._submission_sequence += 1
        token = (
            f"{job_id:06d}-{self._submission_sequence:06d}-{uuid.uuid4().hex}"
        )
        job_path = self.jobs_dir / f"job-{token}.json"
        done_path = self.jobs_dir / f"job-{token}.done"
        result_path = self.results_dir / f"result-{token}.jsonl"
        for path in (job_path, done_path, result_path, Path(f"{job_path}.tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise NativeExecutionError(
                    f"Could not clear native worker job path {path}: {exc}"
                ) from exc

        job_config = dict(config)
        # ``job_id`` is the logical native batch identity, while the queue
        # token above identifies this worker submission.  Preserve an explicit
        # zero-valued batch identity instead of treating it as a false-y
        # fallback to the queue sequence.
        job_config["job_id"] = (
            int(job_config["job_id"]) if "job_id" in job_config else int(job_id)
        )
        job_config["attempt"] = max(
            1,
            int(job_config["attempt"])
            if "attempt" in job_config
            else int(job_config.get("attempt") or 1),
        )
        job_config["result_path"] = str(result_path)
        job_config["progress_path"] = str(progress_path)
        job_config["submission_id"] = token
        temporary = Path(f"{job_path}.tmp")
        temporary.write_text(
            json.dumps(job_config, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, job_path)

        effective_timeout = self.timeout if timeout is None else timeout
        start_time = time.monotonic()
        last_size = progress_path.stat().st_size if progress_path.is_file() else 0
        last_rendered = 0
        last_reported = 0
        while True:
            if done_path.is_file():
                break
            if self.process.poll() is not None:
                stdout = self._read_output(self.stdout_path)
                stderr = self._read_output(self.stderr_path)
                raise NativeExecutionError(
                    "Native Ren'Py persistent worker exited before completing "
                    f"job {job_id}.\n{stdout}\n{stderr}"
                )
            elapsed = time.monotonic() - start_time
            if elapsed >= effective_timeout:
                self.abort()
                stdout = self._read_output(self.stdout_path)
                stderr = self._read_output(self.stderr_path)
                raise NativeExecutionError(
                    "Native Ren'Py persistent worker timed out after "
                    f"{effective_timeout} seconds on job {job_id}.\n"
                    f"{stdout}\n{stderr}"
                )
            last_size, last_rendered, last_reported = _read_progress(
                progress_path,
                last_size=last_size,
                last_rendered=last_rendered,
                last_reported=last_reported,
                progress_cb=progress_cb,
                total_cb=total_cb,
            )
            time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            done = json.loads(done_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeExecutionError(
                f"Could not read native worker completion marker {done_path}: {exc}"
            ) from exc
        if not isinstance(done, dict) or done.get("status") != "ok":
            # The job result contains the structured error (including a native
            # limit kind).  Let the protocol reader classify it when present;
            # otherwise the worker lifecycle itself failed.
            if result_path.is_file():
                return result_path
            message = (
                str(done.get("message") or "")
                if isinstance(done, dict)
                else ""
            )
            stdout = self._read_output(self.stdout_path)
            stderr = self._read_output(self.stderr_path)
            raise NativeExecutionError(
                f"Native Ren'Py persistent worker failed job {job_id}.\n"
                f"{message}\n{stdout}\n{stderr}"
            )
        return result_path

    def close(self) -> None:
        """Stop the worker cleanly and terminate it if it does not exit."""
        process = self.process
        if process is not None and process.poll() is None:
            try:
                self.stop_path.write_text("stop\n", encoding="utf-8")
            except OSError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
        self.process = None
        self._close_output_files()

    def abort(self) -> None:
        """Terminate the worker and its child process tree immediately."""
        if self.process is not None:
            _terminate_process_tree(self.process)
        self.process = None
        self._close_output_files()

    def _close_output_files(self) -> None:
        for handle_name in ("_stdout_file", "_stderr_file"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)

    @staticmethod
    def _read_output(path: Path) -> str:
        return (
            path.read_text(encoding="utf-8", errors="replace")
            if path.is_file()
            else ""
        )

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            self.close()
        else:
            self.abort()


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


__all__ = [
    "PersistentNativeWorker",
    "ProcessResult",
    "run_native_batch",
]

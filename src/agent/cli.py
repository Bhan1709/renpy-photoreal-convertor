"""Command-line launcher for OpenCode model profiles (typer + rich)."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import typer

from cli import (
    Choice,
    Separator,
    create_app,
    error,
    info,
    kv,
    loading,
    note,
    print_table,
    rule,
    select,
    step,
    success,
    warning,
)

from .config import ConfigError, Profile, load_profile, load_profiles
from .constants import (
    PROXY_HEALTH_PATH,
    PROXY_HOST,
    PROXY_LOG_FILENAME,
    PROXY_MODULE,
    PROXY_POLL_INTERVAL_SECONDS,
    PROXY_PORT,
    PROXY_SERVICE_NAME,
    PROXY_START_TIMEOUT_SECONDS,
)
from .monitor import ConversationMonitor

logger = logging.getLogger(__name__)

PROJECT_CONFIG_SCHEMA = "https://opencode.ai/config.json"
OPENAI_COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible"
MAX_OUTPUT_TOKENS = 32768
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"

# How often (seconds) the launcher checks on OpenCode. Waiting in short slices
# keeps Ctrl+C responsive while a full-screen TUI is running.
CHILD_POLL_INTERVAL_SECONDS = 0.25
# How long OpenCode gets to exit on its own after Ctrl+C before it is stopped.
CHILD_SHUTDOWN_GRACE_SECONDS = 5.0
CHILD_KILL_TIMEOUT_SECONDS = 5.0
# (DWORD) -10, the STD_INPUT_HANDLE pseudo-handle.
STD_INPUT_HANDLE = 0xFFFFFFF6

app = create_app(
    "Launch OpenCode with a configured model profile and back up its conversation."
)


# --------------------------------------------------------------------------- #
# Proxy health / lifecycle helpers (unchanged logic, rich output)             #
# --------------------------------------------------------------------------- #


def _proxy_health() -> dict[str, Any] | None:
    """Return the running proxy's health payload, or ``None`` if it is absent."""
    url = f"http://{PROXY_HOST}:{PROXY_PORT}{PROXY_HEALTH_PATH}"
    try:
        response = httpx.get(url, timeout=1.0, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("service") != PROXY_SERVICE_NAME:
        return None
    return payload


def _proxy_is_healthy() -> bool:
    """Return whether a healthy NVIDIA proxy is already listening."""
    return _proxy_health() is not None


def _proxy_log_path() -> Path:
    """Return the temp log file used by the proxy process."""
    return Path(tempfile.gettempdir()) / PROXY_LOG_FILENAME


def _port_in_use() -> bool:
    """Return whether something is listening on the proxy port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((PROXY_HOST, PROXY_PORT)) == 0


def _wait_for_proxy(
    timeout: float = PROXY_START_TIMEOUT_SECONDS,
    interval: float = PROXY_POLL_INTERVAL_SECONDS,
) -> bool:
    """Wait until the local NVIDIA proxy reports healthy."""
    deadline = time.monotonic() + timeout
    with loading("Waiting for the NVIDIA proxy to become healthy..."):
        while time.monotonic() < deadline:
            if _proxy_is_healthy():
                return True
            time.sleep(interval)
    return False


def _proxy_popen_kwargs() -> dict[str, Any]:
    """Return platform flags that let the proxy outlive this launcher."""
    if os.name == "nt":
        # The venv's python.exe is a GUI-subsystem redirector, so launching it
        # with DETACHED_PROCESS makes the real interpreter allocate a visible
        # console window. CREATE_NO_WINDOW keeps it headless instead.
        # getattr keeps this type-checkable on non-Windows platforms where
        # these flags do not exist; the branch only runs on Windows.
        return {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def _start_proxy() -> subprocess.Popen[bytes]:
    """Start a detached proxy process, logging its output to a temp file."""
    log_path = _proxy_log_path()
    with log_path.open("ab") as log_file:
        return cast(
            subprocess.Popen[bytes],
            subprocess.Popen(
                [sys.executable, "-m", PROXY_MODULE],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **_proxy_popen_kwargs(),
            ),
        )


def _stop_proxy() -> int:
    """Stop a running NVIDIA proxy by PID."""
    rule("Stop Proxy")
    payload = _proxy_health()
    if payload is None:
        if _port_in_use():
            warning(
                f"Port {PROXY_PORT} is in use, but no NVIDIA proxy answered the "
                f"health check. Close the process using it (an old proxy from an "
                f"earlier version may still be running)."
            )
            return 1
        info("No NVIDIA proxy is running.")
        return 0

    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        error("The running NVIDIA proxy did not report a valid PID.")
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        error(f"Could not stop the NVIDIA proxy (pid {pid}): {exc}")
        return 1

    deadline = time.monotonic() + PROXY_START_TIMEOUT_SECONDS
    with loading("Waiting for the NVIDIA proxy to stop..."):
        while time.monotonic() < deadline:
            if _proxy_health() is None:
                success(f"Stopped the NVIDIA proxy (pid {pid}).")
                return 0
            time.sleep(PROXY_POLL_INTERVAL_SECONDS)

    warning(f"Sent a stop signal to pid {pid}, but the proxy is still responding.")
    return 1


def _console_api() -> tuple[Any, Any] | None:
    """Return ``(kernel32, ctypes.wintypes)`` for console calls on Windows."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - wintypes ships with CPython
        return None

    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - only reachable off Windows
        return None

    kernel32 = windll.kernel32
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    return kernel32, wintypes


def _console_input_mode() -> int | None:
    """Return the console input mode, or ``None`` when stdin is not a console."""
    api = _console_api()
    if api is None:
        return None
    kernel32, wintypes = api
    handle = kernel32.GetStdHandle(wintypes.DWORD(STD_INPUT_HANDLE))
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    return int(mode.value)


def _restore_console_input_mode(mode: int | None) -> None:
    """Restore a console input mode captured before OpenCode was launched.

    OpenCode's TUI switches the console to raw input, which also turns off
    ``ENABLE_PROCESSED_INPUT``: Ctrl+C stops producing a console control event
    and becomes a plain byte. If the TUI dies without undoing that (crash,
    forced kill), the shell left behind looks frozen -- no echo, and Ctrl+C is
    ignored. Restoring the saved mode makes the terminal usable again.
    """
    if mode is None:
        return
    api = _console_api()
    if api is None:
        return
    kernel32, wintypes = api
    handle = kernel32.GetStdHandle(wintypes.DWORD(STD_INPUT_HANDLE))
    kernel32.SetConsoleMode(handle, wintypes.DWORD(mode))


def _poll_child(process: subprocess.Popen[bytes], timeout: float) -> int | None:
    """Wait up to ``timeout`` seconds for the child, polling in short slices."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            return process.wait(timeout=min(CHILD_POLL_INTERVAL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                return None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop the child and every process it spawned.

    ``opencode`` on Windows is a small launcher shim that starts the real TUI as
    a child, so killing only ``process`` can orphan the TUI in raw console mode.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=CHILD_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_for_child(process: subprocess.Popen[bytes]) -> int:
    """Wait for the child while keeping Ctrl+C responsive.

    ``subprocess.run`` waits through ``Popen.wait()``, which blocks in
    ``WaitForSingleObject(INFINITE)`` on Windows. A pending Ctrl+C is then only
    delivered once the child exits, so a full-screen TUI that ignores Ctrl+C
    leaves the launcher stuck and the terminal in raw mode. Polling in short
    slices lets ``KeyboardInterrupt`` be raised while the child still runs.
    """
    try:
        while True:
            code = _poll_child(process, CHILD_POLL_INTERVAL_SECONDS)
            if code is not None:
                return code
    except KeyboardInterrupt:
        pass

    if process.poll() is not None:
        raise KeyboardInterrupt

    warning(
        "Ctrl+C received; waiting for OpenCode to exit "
        "(press Ctrl+C again to force quit)..."
    )
    try:
        code = _poll_child(process, CHILD_SHUTDOWN_GRACE_SECONDS)
    except KeyboardInterrupt:
        code = None

    if code is None:
        warning("OpenCode did not exit; stopping it.")
        _terminate_process_tree(process)
    raise KeyboardInterrupt


def _install_interrupt_handlers() -> None:
    """Make Ctrl+Break abort the launcher even while a TUI owns the console.

    OpenCode turns off ``ENABLE_PROCESSED_INPUT``, so Ctrl+C reaches the TUI as
    a byte and never becomes a console control event for the launcher.
    Ctrl+Break is always delivered, which gives the launcher a way out of a
    wedged TUI.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:  # pragma: no cover - SIGBREAK is Windows-only
        return
    signal.signal(sigbreak, signal.default_int_handler)


def _run_command(command: Sequence[str]) -> int:
    """Run OpenCode in the foreground, restoring the console on the way out."""
    console_mode = _console_input_mode()
    try:
        process = subprocess.Popen(list(command))
    except OSError as exc:
        error(f"Could not run {command[0]!r}: {exc}")
        return 1

    try:
        return _wait_for_child(process)
    finally:
        _restore_console_input_mode(console_mode)


def _model_selector(profile: Profile) -> str:
    """Return the ``provider/model`` selector OpenCode expects."""
    return f"{profile.provider}/{profile.model_name}"


def _project_config_path() -> Path:
    """Return the project OpenCode config to update, preferring existing files."""
    for filename in ("opencode.jsonc", "opencode.json"):
        path = Path.cwd() / filename
        if path.exists():
            return path
    return Path.cwd() / "opencode.json"


def _ensure_object(parent: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    """Return a nested JSON object, rejecting non-object values."""
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise ConfigError(
            f"Project config {path} has an invalid {key!r} value (expected an object)."
        )
    return value


def _read_project_config(path: Path) -> dict[str, Any]:
    """Read an existing project config, or return an empty object."""
    if not path.exists():
        return {}

    try:
        raw_config = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Project config {path} could not be read: {exc}") from exc

    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Project config {path} contains comments or invalid JSON; "
            "convert it to plain JSON or add the model manually."
        ) from exc

    if not isinstance(config, dict):
        raise ConfigError(f"Project config {path} must contain a JSON object.")
    return config


def _write_project_config(profile: Profile) -> Path:
    """Write the profile's model and provider settings to the project config."""
    path = _project_config_path()
    config = _read_project_config(path)
    selector = _model_selector(profile)
    provider_id, model_id = selector.split("/", 1)

    config.setdefault("$schema", PROJECT_CONFIG_SCHEMA)
    config["model"] = selector

    providers = _ensure_object(config, "providers", path)
    provider = _ensure_object(providers, provider_id, path)

    if provider_id == "nvidia":
        provider.setdefault("name", "NVIDIA NIM (Proxy)")
        provider["package"] = OPENAI_COMPATIBLE_PACKAGE
        settings = _ensure_object(provider, "settings", path)
        settings["baseURL"] = f"http://{PROXY_HOST}:{PROXY_PORT}/v1"
        settings.setdefault("apiKey", "local-proxy")
        settings.setdefault("timeout", 600000)
    elif provider_id == "ollama":
        provider.setdefault("name", "Ollama")
        settings = _ensure_object(provider, "settings", path)
        settings.setdefault("baseURL", OLLAMA_BASE_URL)
        settings.setdefault("apiKey", "ollama")

    models = _ensure_object(provider, "models", path)
    model = _ensure_object(models, model_id, path)
    model.setdefault("name", model_id)
    capabilities = _ensure_object(model, "capabilities", path)
    capabilities.setdefault("tools", True)
    model["limit"] = {
        "context": profile.context_limit,
        "output": min(MAX_OUTPUT_TOKENS, profile.context_limit),
    }

    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _run_with_nvidia_proxy(profile: Profile) -> int:
    """Start or reuse the rate-limit proxy, then launch OpenCode through it."""
    proxy_process: subprocess.Popen[bytes] | None = None

    if _proxy_is_healthy():
        info(f"Reusing existing NVIDIA proxy on http://{PROXY_HOST}:{PROXY_PORT}...")
    else:
        if _port_in_use():
            error(
                f"Port {PROXY_PORT} is already in use, but it is not a healthy "
                f"NVIDIA proxy. Close the process using it, then retry."
            )
            return 1

        info(
            f"Starting local NVIDIA rate-limit proxy on http://{PROXY_HOST}:{PROXY_PORT}..."
        )
        try:
            proxy_process = _start_proxy()
        except OSError as exc:
            error(f"Could not start the NVIDIA proxy: {exc}")
            return 1

        if not _wait_for_proxy() and not _proxy_is_healthy():
            if proxy_process.poll() is None:
                proxy_process.terminate()
            error(
                f"Local NVIDIA proxy failed to become healthy within "
                f"{PROXY_START_TIMEOUT_SECONDS:.0f} seconds. See "
                f"{_proxy_log_path()} for details."
            )
            return 1
        # Another launcher won the race to bind the port; reuse its proxy.

        if proxy_process.poll() is None:
            # The proxy intentionally outlives this launcher. Clearing the
            # return code detaches it from this Popen handle so Python does not
            # warn about a subprocess that is still running at shutdown.
            proxy_process.returncode = 0

    try:
        info(f"Launching OpenCode [{profile.key}] -> {_model_selector(profile)}...")
        return _run_command(["opencode"])
    finally:
        note(
            f"NVIDIA proxy left running at http://{PROXY_HOST}:{PROXY_PORT} for reuse "
            f"(stop it with `mise run agent stop-proxy` or `python -m agent stop-proxy`)."
        )


def _run_with_ollama(profile: Profile) -> int:
    """Launch OpenCode with the Ollama-backed profile."""
    info(
        f"Launching OpenCode via Ollama [{profile.key}] -> {_model_selector(profile)}..."
    )
    return _run_command(["opencode"])


def _print_profiles() -> int:
    try:
        profiles = load_profiles()
    except ConfigError as exc:
        error(str(exc))
        return 1

    rows: list[list[str]] = []
    for key in sorted(profiles):
        profile = profiles[key]
        rows.append(
            [
                key,
                profile.provider,
                profile.model_name,
                f"{profile.context_limit:,}",
            ]
        )
    print_table(
        "Available model profiles",
        ["Key", "Provider", "Model", "Context limit"],
        rows,
    )
    return 0


# --------------------------------------------------------------------------- #
# Typer command                                                               #
# --------------------------------------------------------------------------- #


@app.command()
def run(
    profile: str | None = typer.Argument(
        None,
        help=(
            "Model profile key from config.json (for example nvidia-glm or "
            "qwen-3-vl-32k), or a maintenance action: stop-proxy / list-profiles."
        ),
    ),
    list_profiles: bool = typer.Option(
        False, "--list-profiles", help="List the available profiles and exit."
    ),
    stop_proxy: bool = typer.Option(
        False, "--stop-proxy", help="Stop the running NVIDIA proxy and exit."
    ),
) -> None:
    """Launch OpenCode with a model profile, or run a maintenance action.

    With no profile, an interactive picker is shown. The ``stop-proxy`` and
    ``list-profiles`` actions can be passed as either a flag or a positional
    argument, so ``mise run agent stop-proxy`` and
    ``python -m agent --stop-proxy`` both work.
    """
    if list_profiles or profile == "list-profiles":
        raise typer.Exit(_print_profiles())
    if stop_proxy or profile == "stop-proxy":
        raise typer.Exit(_stop_proxy())

    rule("Agent launcher")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not profile:
        # No profile given: interactively pick one from the available list.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            error(
                "No model profile was provided, and this terminal does not support "
                "interactive prompts. Pass a profile key, for example: "
                "`python -m agent qwen-3-vl-32k`."
            )
            raise typer.Exit(1)

        try:
            profiles = load_profiles()
        except ConfigError as exc:
            error(str(exc))
            raise typer.Exit(1)

        if not profiles:
            error("No model profiles are defined in config.json.")
            raise typer.Exit(1)

        # Build grouped, metadata-rich choices for the profile picker.
        provider_order = {"ollama": 0, "nvidia": 1}
        sorted_keys = sorted(
            profiles,
            key=lambda k: (provider_order.get(profiles[k].provider, 99), k),
        )
        choices: list[str | Choice] = []
        current_provider = None
        for key in sorted_keys:
            prof = profiles[key]
            if prof.provider != current_provider:
                current_provider = prof.provider
                choices.append(Separator(f"--- {current_provider.upper()} ---"))
            # Format context limit (e.g. 131072 -> 128k, 1048576 -> 1M)
            ctx_limit = prof.context_limit
            if ctx_limit >= 1_000_000:
                ctx_str = f"{ctx_limit // 1_000_000}M ctx"
            elif ctx_limit >= 1_000:
                ctx_str = f"{ctx_limit // 1_000}k ctx"
            else:
                ctx_str = f"{ctx_limit} ctx"
            vision = "Vision" if "vl" in prof.model_name.lower() else "Text"
            choices.append(
                Choice(
                    title=f"{key}  [{vision} | {ctx_str}]",
                    value=key,
                )
            )

        selected = select(
            "Choose a model profile",
            choices=choices,
            default=min(profiles),
            use_shortcuts=True,
        )
        if selected is None:
            info("No model profile was selected. Cancelled.")
            raise typer.Exit(1)
        assert selected is not None
        profile = selected
        kv("Profile", profile)

    try:
        profile_obj = load_profile(profile)
    except ConfigError as exc:
        error(str(exc))
        raise typer.Exit(1)

    if profile_obj.provider == "nvidia" and not os.environ.get("NVIDIA_API_KEY"):
        error("NVIDIA_API_KEY environment variable is not set.")
        raise typer.Exit(1)

    try:
        config_path = _write_project_config(profile_obj)
    except (OSError, ConfigError) as exc:
        error(f"Could not update project config: {exc}")
        raise typer.Exit(1)
    info(f"Updated the project config with the selected model: {config_path}")

    step("1", f"Launching OpenCode with profile [{profile_obj.key}]")

    monitor = ConversationMonitor(
        profile_key=profile_obj.key,
        model_name=profile_obj.model_name,
        context_limit=profile_obj.context_limit,
    )
    monitor.start()

    try:
        _install_interrupt_handlers()
        if profile_obj.provider == "nvidia":
            code = _run_with_nvidia_proxy(profile_obj)
        else:
            code = _run_with_ollama(profile_obj)
    except KeyboardInterrupt:
        info("Session interrupted. Goodbye.")
        raise typer.Exit(130)
    finally:
        monitor.stop()
    raise typer.Exit(code)

"""Command-line launcher for OpenCode model profiles."""

from __future__ import annotations

import argparse
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


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="opencode-monitor",
        description="Launch OpenCode with a configured model profile and back up its conversation.",
    )
    parser.add_argument(
        "profile",
        nargs="?",
        help="Profile key from config.json (for example: nvidia-glm or qwen-3-vl-32k).",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List the available profiles and exit.",
    )
    parser.add_argument(
        "--stop-proxy",
        action="store_true",
        help="Stop the running NVIDIA proxy and exit.",
    )
    return parser


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
    payload = _proxy_health()
    if payload is None:
        if _port_in_use():
            logger.warning(
                "⚠️ Port %d is in use, but no NVIDIA proxy answered the health "
                "check. Close the process using it (an old proxy from an earlier "
                "version may still be running).",
                PROXY_PORT,
            )
            return 1
        logger.info("No NVIDIA proxy is running.")
        return 0

    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        logger.error("❌ Error: The running NVIDIA proxy did not report a valid PID.")
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.error("❌ Error: Could not stop the NVIDIA proxy (pid %d): %s", pid, exc)
        return 1

    deadline = time.monotonic() + PROXY_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _proxy_health() is None:
            logger.info("🛑 Stopped the NVIDIA proxy (pid %d).", pid)
            return 0
        time.sleep(PROXY_POLL_INTERVAL_SECONDS)

    logger.warning(
        "⚠️ Sent a stop signal to pid %d, but the proxy is still responding.", pid
    )
    return 1


def _run_command(command: Sequence[str]) -> int:
    """Run a subprocess and return its exit code."""
    try:
        return subprocess.run(list(command), check=False).returncode
    except OSError as exc:
        logger.error("❌ Error: Could not run %r: %s", command[0], exc)
        return 1


def _model_selector(profile: Profile) -> str:
    """Return the ``provider/model`` selector OpenCode expects.

    ``Profile.model_name`` is the upstream model ID (for example
    ``nvidia/nemotron-3.5-lightning-30b-a3b``), so the provider prefix is
    always added. Upstream IDs can themselves start with ``nvidia/``.
    """
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
        logger.info(
            "♻️ Reusing existing NVIDIA proxy on http://%s:%d...",
            PROXY_HOST,
            PROXY_PORT,
        )
    else:
        if _port_in_use():
            logger.error(
                "❌ Error: Port %d is already in use, but it is not a healthy "
                "NVIDIA proxy. Close the process using it, then retry.",
                PROXY_PORT,
            )
            return 1

        logger.info(
            "🛡️ Starting local NVIDIA rate-limit proxy on http://%s:%d...",
            PROXY_HOST,
            PROXY_PORT,
        )
        try:
            proxy_process = _start_proxy()
        except OSError as exc:
            logger.error("❌ Error: Could not start the NVIDIA proxy: %s", exc)
            return 1

        if not _wait_for_proxy() and not _proxy_is_healthy():
            if proxy_process.poll() is None:
                proxy_process.terminate()
            logger.error(
                "❌ Error: Local NVIDIA proxy failed to become healthy within "
                "%.0f seconds. See %s for details.",
                PROXY_START_TIMEOUT_SECONDS,
                _proxy_log_path(),
            )
            return 1
        # Another launcher won the race to bind the port; reuse its proxy.

        if proxy_process.poll() is None:
            # The proxy intentionally outlives this launcher. Clearing the
            # return code detaches it from this Popen handle so Python does not
            # warn about a subprocess that is still running at shutdown.
            proxy_process.returncode = 0

    try:
        logger.info(
            "🚀 Launching OpenCode [%s] -> %s...", profile.key, _model_selector(profile)
        )
        return _run_command(["opencode"])
    finally:
        logger.info(
            "ℹ️ NVIDIA proxy left running at http://%s:%d for reuse "
            "(stop it with `mise run stop-proxy` or "
            "`python -m agent --stop-proxy`).",
            PROXY_HOST,
            PROXY_PORT,
        )


def _run_with_ollama(profile: Profile) -> int:
    """Launch OpenCode with the Ollama-backed profile."""
    logger.info(
        "🚀 Launching OpenCode via Ollama [%s] -> %s...",
        profile.key,
        _model_selector(profile),
    )
    return _run_command(["opencode"])


def _print_profiles() -> int:
    try:
        profiles = load_profiles()
    except ConfigError as exc:
        logger.error("❌ Error: %s", exc)
        return 1

    for key in sorted(profiles):
        profile = profiles[key]
        print(
            f"{key}\t{profile.provider}\t{profile.model_name}\t{profile.context_limit}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the profile launcher and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.list_profiles:
        return _print_profiles()
    if args.stop_proxy:
        return _stop_proxy()
    if args.profile is None:
        parser.error(
            "a profile key is required unless --list-profiles or --stop-proxy is used"
        )

    try:
        profile = load_profile(args.profile)
    except ConfigError as exc:
        logger.error("❌ Error: %s", exc)
        return 1

    if profile.provider == "nvidia" and not os.environ.get("NVIDIA_API_KEY"):
        logger.error("❌ Error: NVIDIA_API_KEY environment variable is not set.")
        return 1

    try:
        config_path = _write_project_config(profile)
    except (OSError, ConfigError) as exc:
        logger.error("❌ Error: Could not update project config: %s", exc)
        return 1
    logger.info("🗒️ Selected model in project config: %s", config_path)

    monitor = ConversationMonitor(
        profile_key=profile.key,
        model_name=profile.model_name,
        context_limit=profile.context_limit,
    )
    monitor.start()

    try:
        if profile.provider == "nvidia":
            return _run_with_nvidia_proxy(profile)
        return _run_with_ollama(profile)
    except KeyboardInterrupt:
        logger.info("\n👋 Session interrupted by user.")
        return 130
    finally:
        monitor.stop()


if __name__ == "__main__":
    raise SystemExit(main())

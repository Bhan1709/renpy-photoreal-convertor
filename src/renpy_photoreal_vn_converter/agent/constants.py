"""Shared constants for the agent subpackage."""

from __future__ import annotations

from typing import Final

PROXY_HOST: Final = "127.0.0.1"
PROXY_PORT: Final = 8000
PROXY_MODULE: Final = f"{__package__}.nvidia_proxy"
PROXY_START_TIMEOUT_SECONDS: Final = 10.0
PROXY_POLL_INTERVAL_SECONDS: Final = 0.2
PROXY_HEALTH_PATH: Final = "/_nvidia_proxy/health"
PROXY_SERVICE_NAME: Final = "nvidia-proxy"
PROXY_LOG_FILENAME: Final = "opencode-nvidia-proxy.log"

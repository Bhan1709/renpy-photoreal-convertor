"""Local rate-limiting reverse proxy for NVIDIA's hosted models."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .constants import (
    PROXY_HEALTH_PATH,
    PROXY_HOST,
    PROXY_PORT,
    PROXY_SERVICE_NAME,
)

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = os.environ.get(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com"
).rstrip("/")
MIN_INTERVAL_SECONDS = 1.65  # ~36.3 RPM to stay safely below the 40 RPM limit.
MAX_RETRIES = 5
REQUEST_TIMEOUT_SECONDS = 600.0
_EXCLUDED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class RateLimiter:
    """Serialize requests and enforce a minimum interval between them."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait_time = self._min_interval - (
                time.monotonic() - self._last_request_time
            )
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._last_request_time = time.monotonic()


def _retry_after_seconds(value: str | None, attempt: int) -> float:
    """Parse a Retry-After header, falling back to linear backoff."""
    fallback = float(5 * attempt)
    if value is None:
        return fallback
    try:
        return max(float(value), 0.0)
    except ValueError:
        return fallback


def _stream_response(upstream: httpx.Response) -> StreamingResponse:
    """Convert a streamed upstream response into a Starlette streaming response."""
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _EXCLUDED_RESPONSE_HEADERS
    }

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


def create_app(
    *,
    base_url: str = NVIDIA_BASE_URL,
    min_interval: float = MIN_INTERVAL_SECONDS,
) -> FastAPI:
    """Create the FastAPI application for the proxy."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        app.state.rate_limiter = RateLimiter(min_interval)
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="NVIDIA Rate-Limit Proxy", lifespan=lifespan)

    @app.get(PROXY_HEALTH_PATH)
    async def health() -> JSONResponse:
        """Identify this process so launchers can reuse a running proxy."""
        return JSONResponse(
            {
                "status": "ok",
                "service": PROXY_SERVICE_NAME,
                "pid": os.getpid(),
            }
        )

    @app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
    async def proxy(request: Request, path: str) -> Response:
        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            logger.error("NVIDIA_API_KEY is not set; rejecting proxy request.")
            return JSONResponse({"error": "NVIDIA_API_KEY is not set"}, status_code=500)

        target_url = f"{base_url}/{path}"
        if request.url.query:
            target_url = f"{target_url}?{request.url.query}"

        body = await request.body()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": request.headers.get("content-type", "application/json"),
            "Accept": request.headers.get("accept", "text/event-stream"),
        }

        client: httpx.AsyncClient = request.app.state.client
        rate_limiter: RateLimiter = request.app.state.rate_limiter

        for attempt in range(1, MAX_RETRIES + 1):
            await rate_limiter.acquire()

            upstream_request = client.build_request(
                method=request.method,
                url=target_url,
                content=body,
                headers=headers,
            )
            upstream = await client.send(upstream_request, stream=True)

            if upstream.status_code != 429:
                return _stream_response(upstream)

            retry_after = _retry_after_seconds(
                upstream.headers.get("retry-after"), attempt
            )
            await upstream.aclose()

            if attempt == MAX_RETRIES:
                break

            logger.warning(
                "Rate limited by NVIDIA; retrying in %.1f seconds (attempt %d/%d).",
                retry_after,
                attempt,
                MAX_RETRIES,
            )
            await asyncio.sleep(retry_after)

        return JSONResponse({"error": "Max retries exceeded"}, status_code=429)

    return app


app = create_app()


def run() -> None:
    """Run the proxy with Uvicorn."""
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning")


if __name__ == "__main__":
    run()

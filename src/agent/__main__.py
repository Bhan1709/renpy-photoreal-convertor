"""Allow running the agent launcher with ``python -m agent``."""

from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    raise SystemExit(app())

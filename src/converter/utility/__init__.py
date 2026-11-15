"""Shared utility modules for the converter subpackages.

Provides dependency-free helpers (progress reporting, timing, …) that can be
reused across subpackages (decompile, compose, …) without pulling in
third-party libraries such as tqdm.
"""

from __future__ import annotations

import sys
import time
from typing import Callable


class ProgressBar:
    """A single-line progress bar with elapsed time and ETA.

    Writes to ``stderr`` so it does not interfere with stdout-based JSON
    summaries.  Each call reuses the same line via carriage return (``\\r``),
    so no new lines are emitted until the progress is complete.

    Example::

        bar = ProgressBar(total=100, width=28, label="indexing script uses")
        for i in range(100):
            # ... do work ...
            bar.update(i + 1)
        bar.finish()
    """

    def __init__(self, total: int, width: int = 28, label: str = "", stream=None):
        self.total = max(total, 1)
        self.width = width
        self.label = label
        self.stream = stream or sys.stderr
        self._start = time.monotonic()
        self._last_done = 0

    def _format(self, done: int) -> str:
        ratio = done / self.total
        filled = int(self.width * ratio)
        bar = ("=" * filled).ljust(self.width, ".")
        pct = ratio * 100.0
        elapsed = time.monotonic() - self._start
        if done > 0 and elapsed > 0:
            rate = done / elapsed
            eta = (self.total - done) / rate
            eta_str = f"ETA {eta:5.1f}s"
        else:
            eta_str = "ETA --:--"
        elapsed_str = f"elapsed {elapsed:5.1f}s"
        return (
            f"\r[{bar}] {pct:6.1f}% {done}/{self.total} "
            f"{elapsed_str} {eta_str} {self.label}"
        )

    def update(self, done: int, label: str | None = None) -> None:
        """Render progress at ``done`` (count of completed items)."""
        if label is not None:
            self.label = label
        self.stream.write(self._format(done))
        self.stream.flush()
        self._last_done = done

    def finish(self) -> None:
        """Print the final line and a trailing newline."""
        self.stream.write(self._format(self.total) + "\n")
        self.stream.flush()


def make_progress_bar(total: int, width: int = 28, label: str = "") -> Callable[[int, str | None], None]:
    """Return a ``progress(done, label=None)`` callback backed by :class:`ProgressBar`."""
    bar = ProgressBar(total=total, width=width, label=label)

    def progress(done: int, label: str | None = None) -> None:
        bar.update(done, label=label)
        if done >= bar.total:
            bar.finish()

    return progress


# Module-level default instance (28 chars wide) for drop-in use.
progress = make_progress_bar(total=0)

__all__ = ["ProgressBar", "make_progress_bar", "progress"]

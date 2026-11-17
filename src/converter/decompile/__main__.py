"""Allow ``python -m converter.decompile`` as a shortcut for the decompile CLI."""

from __future__ import annotations

import sys

from .decompile import app

if __name__ == "__main__":
    sys.exit(app())

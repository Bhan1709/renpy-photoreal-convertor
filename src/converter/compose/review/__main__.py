"""Allow ``python -m converter.compose.review`` as a shortcut for the review CLI."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

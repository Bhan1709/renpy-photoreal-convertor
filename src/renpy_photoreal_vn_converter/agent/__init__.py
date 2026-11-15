"""OpenCode agent orchestration for the Ren'Py photoreal VN converter.

The subpackage bundles model profiles, a context-window monitor, and a local
rate-limiting proxy for NVIDIA's hosted models. It can be run directly with::

    python -m renpy_photoreal_vn_converter.agent <profile>

Profiles are defined in ``config.json`` next to this module.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

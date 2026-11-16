"""Exclusion pattern handling for config.json source files."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import load_config


def matches_exclusion(path: str, patterns: list[str]) -> bool:
    r"""Return True if ``path`` matches any exclusion pattern.

    A pattern matches if it is a substring of ``path`` (e.g. ``archive/``
    matches ``archive/cabins.rpy``) or if ``path`` glob-matches the pattern.
    Matching is case-sensitive on the path as stored (forward-slash, relative
    to the consolidated game folder).
    """
    if not patterns:
        return False
    for pattern in patterns:
        if not pattern:
            continue
        if pattern in path:
            return True
        try:
            if fnmatch.fnmatchcase(path, pattern):
                return True
        except Exception:  # pragma: no cover - defensive
            pass
    return False


def filter_source_files(
    config_path: Path = Path("src/converter/config.json"),
) -> dict[str, list[str]]:
    r"""Load ``config.json`` and return source files minus excluded ones.

    Reads the ``rpy_files`` and ``ren_py_files`` matching patterns recorded
    by :func:`converter.decompile.config.write_config` and removes any path
    that matches an exclusion pattern from ``config["exclusion"]["patterns"]``.

    Returns a dict with keys ``"rpy"``, ``"ren_py"`` and ``"extracted_archives"``.
    ``"extracted_archives"`` is the list of ``{"archive", "extracted_folder"}``
    dicts recorded by :func:`write_config`.  Returns empty lists if the config
    is missing.
    """
    config = load_config(config_path)
    rpy_files = config.get("rpy_files", [])
    ren_py_files = config.get("ren_py_files", [])
    extracted_archives = config.get("extracted_archives", []) or []
    exclusion = config.get("exclusion", {}) or {}
    patterns = exclusion.get("patterns", []) or []

    included_rpy = [p for p in rpy_files if not matches_exclusion(p, patterns)]
    included_ren_py = [p for p in ren_py_files if not matches_exclusion(p, patterns)]

    return {
        "rpy": included_rpy,
        "ren_py": included_ren_py,
        "extracted_archives": extracted_archives,
    }


def sync_exclusions(config_path: Path = Path("src/converter/config.json")) -> dict:
    r"""Persist exclusions into ``config.json``.

    Reads ``config.json`` and removes any entry from ``rpy_files`` and
    ``ren_py_files`` that matches a pattern in ``exclusion.patterns``, then
    writes the updated config back.  This is the persistent counterpart of
    :func:`filter_source_files` (which only returns the filtered lists in
    memory).

    Returns a dict with the before/after counts for ``rpy`` and ``ren_py``.
    Returns an empty dict if the config is missing or invalid.
    """
    config = load_config(config_path)
    if not config:
        print(f"Config not found or invalid: {config_path}")
        return {}

    exclusion = config.get("exclusion", {}) or {}
    patterns = exclusion.get("patterns", []) or []

    before_rpy = config.get("rpy_files", [])
    before_ren_py = config.get("ren_py_files", [])

    config["rpy_files"] = [
        p for p in before_rpy if not matches_exclusion(p, patterns)
    ]
    config["ren_py_files"] = [
        p for p in before_ren_py if not matches_exclusion(p, patterns)
    ]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {
        "rpy_before": len(before_rpy),
        "rpy_after": len(config["rpy_files"]),
        "ren_py_before": len(before_ren_py),
        "ren_py_after": len(config["ren_py_files"]),
    }

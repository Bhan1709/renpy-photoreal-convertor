"""Exclusion pattern handling for config.json source files."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from cli import error, info, kv, prompt_text, rule, success

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
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def filter_source_files(
    config_path: Path = Path("src/converter/config.json"),
) -> dict[str, list[str]]:
    r"""Load ``config.json`` and return source files minus excluded ones.

    Reads the ``rpy_files`` and ``ren_py_files`` matching patterns recorded
    by :func:`converter.decompile.config.write_config` and removes any path
    that matches an exclusion pattern from ``config["exclusion"]["script"]["patterns"]``.

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
    script_exc = exclusion.get("script", {}) or {}
    patterns = script_exc.get("patterns", []) or []

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
    ``ren_py_files`` that matches a pattern in ``exclusion.script.patterns``,
    then writes the updated config back.  This is the persistent counterpart of
    :func:`filter_source_files` (which only returns the filtered lists in
    memory).

    Returns a dict with the before/after counts for ``rpy`` and ``ren_py``.
    Returns an empty dict if the config is missing or invalid.
    """
    config = load_config(config_path)
    if not config:
        error(f"Config not found or invalid: {config_path}")
        return {}

    exclusion = config.get("exclusion", {}) or {}
    script_exc = exclusion.get("script", {}) or {}
    patterns = script_exc.get("patterns", []) or []

    before_rpy = config.get("rpy_files", [])
    before_ren_py = config.get("ren_py_files", [])

    config["rpy_files"] = [p for p in before_rpy if not matches_exclusion(p, patterns)]
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


def _format_patterns(patterns: list[str]) -> str:
    """Return a readable, comma-separated representation of patterns."""
    return ", ".join(patterns) if patterns else "(none)"


def prompt_exclusion_patterns(
    config_path: Path = Path("src/converter/config.json"),
) -> bool:
    r"""Interactively add script and image exclusion patterns to ``config.json``.

    Shows the current ``exclusion.script.patterns`` and
    ``exclusion.image.patterns``, asks for comma-separated additions for each
    (pressing Enter keeps the current list), and writes the file back when
    anything changed.

    Script exclusions filter entries from ``rpy_files``/``ren_py_files`` (the
    include set used by ``parser.files()`` and the character associator).
    Image exclusions filter layers whose source path matches a pattern, so
    those image folders are not scanned during compose.

    Returns ``True`` if ``config.json`` was updated, ``False`` otherwise.
    """
    if not config_path.is_file():
        error(f"Config not found: {config_path}")
        info("Run decompile first to generate it.")
        return False

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Could not read config {config_path}: {exc}")
        return False

    exclusion = data.get("exclusion", {}) or {}
    script_current = list(exclusion.get("script", {}).get("patterns", []) or [])
    image_current = list(exclusion.get("image", {}).get("patterns", []) or [])

    rule("Exclusion patterns")
    kv("Current script exclusions", _format_patterns(script_current))
    raw = prompt_text(
        "Add script exclusions (comma-separated; press Enter to keep the current list)",
        default="",
    )
    if raw is None:
        info("Exclusion update cancelled. No changes were made.")
        return False
    script_new = [p.strip() for p in raw.split(",") if p.strip()]
    if script_new:
        script_current = sorted(set(script_current) | set(script_new))

    kv("Current image exclusions", _format_patterns(image_current))
    raw = prompt_text(
        "Add image exclusions (comma-separated; press Enter to keep the current list)",
        default="",
    )
    if raw is None:
        info("Exclusion update cancelled. No changes were made.")
        return False
    image_new = [p.strip() for p in raw.split(",") if p.strip()]
    if image_new:
        image_current = sorted(set(image_current) | set(image_new))

    if not script_new and not image_new:
        info("No new patterns entered. The existing exclusions were kept.")
        return False

    data["exclusion"] = {
        "script": {
            "patterns": script_current,
            "note": (
                "Patterns matched against rpy_files/ren_py_files entries; "
                "matching entries are removed from the scan list"
            ),
        },
        "image": {
            "patterns": image_current,
            "note": (
                "Patterns matched against image source paths; layers whose "
                "source starts with a matching folder are excluded from "
                "compose scanning"
            ),
        },
    }
    try:
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        error(f"Could not write config {config_path}: {exc}")
        return False

    success("Updated exclusion patterns in config.json.")
    return True

"""Safe pruning of stale converter-generated image files.

Pruning is deliberately separate from manifest publication.  Callers invoke
it only after the current manifest bundle has been committed successfully.  The
published, digest-verified bundle is the authority for generated-image
references; live scripts are an additional conservative retention guard.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ...utility.manifest_bundle import (
    manifest_bundle_lock,
    recover_manifest_bundle,
)
from ...utility.manifest_paths import MANIFEST_BUNDLE_VERSION
from ...utility.safe_paths import (
    UnsafePathError,
    resolve_absolute_under_root,
    resolve_game_output_path,
    validate_generated_image_relative,
)
from ..config import GameConfig
from ..errors import ManifestError
from ..manifest import ManifestWriter
from ..parsing import live_script_paths

_GENERATED_ROOTS = ("static", "dynamic_review")
_IMAGE_SUFFIXES = frozenset({".png"})
_QUOTED_PATH_RE = re.compile(r"[\"']([^\"'\r\n]+)[\"']")


@dataclass(frozen=True)
class PruneResult:
    """Summary of one stale-image pruning pass."""

    removed: tuple[Path, ...] = ()
    skipped: tuple[Path, ...] = ()


def _generated_relative(path: Path, output_root: Path) -> Path | None:
    """Return a valid generated-image-relative path, or ``None``."""
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(output_root)
        return validate_generated_image_relative(relative)
    except (OSError, UnsafePathError, ValueError):
        return None


def _manifest_references(
    config: GameConfig,
    manifest: object,
) -> tuple[set[Path], set[Path]]:
    """Return validated bundle references and any missing referenced files."""
    paths = config.resolved_paths()
    output_root = paths["output_dir"].resolve()
    excluded_roots = (
        paths["report_dir"].resolve(),
        (output_root / "rejected").resolve(),
    )
    relative_paths: list[str] = []

    composites = getattr(manifest, "composites_list", [])
    for record in composites:
        relative = getattr(record, "composite_path", None)
        if not isinstance(relative, str) or not relative:
            raise ManifestError(
                "Published manifest contains an invalid composite_path"
            )
        relative_paths.append(relative)

    dynamic_states = getattr(manifest, "dynamic_states", None)
    if not isinstance(dynamic_states, list):
        raise ManifestError("Published manifest dynamic_states must be a list")
    for state in dynamic_states:
        if not isinstance(state, dict):
            raise ManifestError(
                "Published manifest contains an invalid dynamic state"
            )
        generated_states = state.get("generated_states", [])
        if not isinstance(generated_states, list):
            raise ManifestError(
                "Published manifest dynamic state generated_states must be a list"
            )
        for entry in generated_states:
            if not isinstance(entry, dict):
                raise ManifestError(
                    "Published manifest contains an invalid generated state"
                )
            relative = entry.get("composite_path")
            if not isinstance(relative, str) or not relative:
                raise ManifestError(
                    "Published manifest contains an invalid generated composite_path"
                )
            relative_paths.append(relative)

    references: set[Path] = set()
    missing: set[Path] = set()
    for relative in relative_paths:
        try:
            resolved = resolve_game_output_path(
                config.game_root,
                output_root,
                relative,
                excluded_roots=excluded_roots,
            )
        except UnsafePathError as exc:
            raise ManifestError(
                f"Published manifest contains an unsafe composite path: {relative!r}"
            ) from exc
        if not resolved.is_file():
            missing.add(resolved)
        references.add(resolved)
    return references, missing


def _live_script_references(
    config: GameConfig,
    output_root: Path,
) -> set[Path]:
    """Return generated images named by live scripts or image declarations.

    This scan is intentionally conservative.  It may retain an extra image,
    but it never authorizes deletion by itself.
    """
    references: set[Path] = set()
    game_root = config.game_root.resolve()
    try:
        scripts = live_script_paths(config)
    except (OSError, UnsafePathError) as exc:
        raise ManifestError(
            f"Could not enumerate live scripts for stale-image pruning: {exc}"
        ) from exc

    declaration = game_root / "composite_images.rpy"
    if declaration.is_file():
        scripts.append(declaration)

    seen_scripts: set[Path] = set()
    for script in scripts:
        try:
            resolved_script = script.resolve()
            resolved_script.relative_to(game_root)
        except (OSError, ValueError) as exc:
            raise ManifestError(
                f"Live script escapes the game root during stale-image pruning: "
                f"{script}"
            ) from exc
        if resolved_script in seen_scripts:
            continue
        seen_scripts.add(resolved_script)
        if not resolved_script.is_file():
            continue
        try:
            text = resolved_script.read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
        except OSError as exc:
            raise ManifestError(
                f"Could not read live script during stale-image pruning: "
                f"{resolved_script}: {exc}"
            ) from exc

        for match in _QUOTED_PATH_RE.finditer(text):
            value = match.group(1).strip()
            if not value:
                continue
            candidate = Path(value)
            candidates: tuple[Path, ...]
            if candidate.is_absolute():
                candidates = (candidate,)
            else:
                candidates = (
                    resolved_script.parent / candidate,
                    game_root / candidate,
                    game_root.parent / candidate,
                )
            for path in candidates:
                try:
                    resolved = resolve_absolute_under_root(
                        output_root,
                        path,
                        field_name="live generated image path",
                    )
                except UnsafePathError:
                    continue
                relative = _generated_relative(resolved, output_root)
                if relative is not None:
                    references.add(resolved)
                    break

    return references


def _iter_generated_pngs(root: Path) -> list[Path]:
    """Return regular PNG files below one generated root without following links."""
    result: list[Path] = []
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        return result

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink() or path.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if path.suffix.lower() in _IMAGE_SUFFIXES:
                result.append(path)

    visit(root)
    return result


def prune_stale_generated_images(
    config: GameConfig,
    *,
    manifest_index_path: Path | None = None,
    report_dir: Path | None = None,
) -> PruneResult:
    """Remove unreferenced generated PNGs after successful publication.

    The current published bundle and live script references are both
    authoritative retention sources.  Bundle/schema/reference validation is
    completed before the first unlink, so a malformed or inconsistent bundle
    cannot authorize destructive cleanup.
    """
    paths = config.resolved_paths()
    index_path = Path(manifest_index_path or paths["manifest_index_path"])
    reports_path = Path(report_dir or paths["report_dir"])
    writer = ManifestWriter(
        config,
        manifest_index_path=index_path,
        report_dir=reports_path,
    )

    with manifest_bundle_lock(index_path, report_dir=reports_path):
        recover_manifest_bundle(index_path, reports_path)
        manifest = writer._load_all_unlocked()
        try:
            index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"Could not read published manifest index {index_path}: {exc}"
            ) from exc
        if not isinstance(index_payload, dict):
            raise ManifestError("Published manifest index must contain an object")
        if index_payload.get("bundle_version") != MANIFEST_BUNDLE_VERSION:
            raise ManifestError(
                "Stale-image pruning requires manifest bundle version "
                f"{MANIFEST_BUNDLE_VERSION}"
            )
        if not isinstance(index_payload.get("category_digests"), dict):
            raise ManifestError(
                "Stale-image pruning requires published category_digests metadata"
            )

        output_root = paths["output_dir"].resolve()
        manifest_refs, missing_refs = _manifest_references(config, manifest)
        if missing_refs:
            # The bundle is not a complete description of the generated output
            # set.  Apply already reports missing generated files as a normal
            # condition, so pruning fails closed without turning that existing
            # apply contract into a fatal error.  No stale file is removed
            # until every published reference exists.
            return PruneResult(
                skipped=tuple(
                    sorted(missing_refs, key=lambda path: str(path).casefold())
                )
            )
        live_refs = _live_script_references(config, output_root)
        references = manifest_refs | live_refs
        removed: list[Path] = []
        skipped: list[Path] = []

        for status_root in _GENERATED_ROOTS:
            root = output_root / status_root
            for path in _iter_generated_pngs(root):
                try:
                    resolved = path.resolve()
                    relative = resolved.relative_to(output_root)
                    validate_generated_image_relative(relative)
                except (OSError, UnsafePathError, ValueError):
                    # Only valid status/family/file PNGs are converter-owned
                    # pruning candidates.  Preserve every other path.
                    continue
                if resolved in references:
                    continue
                try:
                    path.unlink()
                except OSError:
                    skipped.append(path)
                    continue
                removed.append(path)

    return PruneResult(
        removed=tuple(sorted(removed, key=lambda path: str(path).casefold())),
        skipped=tuple(sorted(set(skipped), key=lambda path: str(path).casefold())),
    )


__all__ = ["PruneResult", "prune_stale_generated_images"]

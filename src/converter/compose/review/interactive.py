"""Interactive review CLI for scene character associations and speaker resolution (questionary + rich).

Reviews unmapped speakers and scenes missing character associations one at a
time. Speaker and scene review keep contextual information and character
choices visible together, with Tab switching focus between the two panes.

Invoked from:

    mise run compose          # menu -> Review composites
    python -m converter.compose review

Run from the repository root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Group

from cli import (
    MAUVE,
    Choice,
    command,
    confirm,
    console,
    counted,
    create_app,
    error,
    info,
    is_interactive,
    kv,
    print_panel,
    print_summary,
    prompt_text,
    rule,
    select_multiple_with_context,
    select_with_context,
    success,
    warning,
)

from ...utility import atomic_write_json_files
from ...utility.atomic import atomic_copy_file, atomic_write_json, json_payload_digest
from ...utility.manifest_bundle import (
    BUNDLE_JOURNAL_NAME,
    BUNDLE_OWNER_REGISTRY_NAME,
    ManifestBundlePathError,
    bundle_journal_metadata,
    bundle_journal_path,
    bundle_paths,
    manifest_bundle_lock,
    recover_manifest_bundle,
    register_bundle_owner,
    validate_manifest_index_path,
)
from ...utility.manifest_paths import (
    MANIFEST_BUNDLE_VERSION,
    ManifestIntegrityError,
    verify_category_manifest_digests,
)
from ...utility.safe_paths import (
    UnsafePathError,
    resolve_game_output_path,
    resolve_under_root,
    safe_relative_path,
)
from ..config import GameConfig, load_config
from ..errors import ManifestError
from ..manifest import (
    CHARACTERS_FILE,
    KNOWN_FAMILIES,
    ManifestWriter,
    _load_characters,
    resolve_category_manifest_paths,
)
from ..review_instructions import manual_scene_instructions, manual_speaker_instructions

app = create_app(
    "Interactively review unmapped speakers and scenes missing character association."
)

REJECTED_SUBFOLDER = "rejected"
REJECTED_PNGS_SUBFOLDER = "pngs"

_KEEP_SPEAKER = "__keep_speaker_as_is__"
_CUSTOM_SPEAKER = "__enter_custom_speaker__"
_REMOVE_SPEAKER = "__remove_speaker_dialogs__"

_KEEP_SCENE_UNASSOCIATED = "__keep_scene_unassociated__"
_REMOVE_SCENE_FROM_PROCESSING = "__remove_scene_from_processing__"
_CUSTOM_SCENE_CHARACTERS = "__enter_custom_scene_characters__"

ReviewFinalizer = Callable[[], None]


def _rejected_record_path(
    rejected_dir: Path,
    prefix: str,
    identity: str,
) -> Path:
    """Return a safe, collision-resistant path for one rejected-item record."""
    identity_text = str(identity)
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", identity_text).strip("._")
    label = label[:80] or "item"
    digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:12]
    return rejected_dir / f"{prefix}_{label}_{digest}.json"


def _validated_rejected_dir(
    config: GameConfig,
    rejected_dir: Path,
) -> tuple[Path, Path]:
    """Return the resolved rejected and composite-output roots.

    The rejected directory must be the canonical ``<output>/rejected`` path.
    Resolving it first prevents a symlinked directory from redirecting review
    records outside the configured composite output.
    """
    output_root = config.resolved_paths()["output_dir"].resolve()
    canonical = (output_root / REJECTED_SUBFOLDER).resolve()
    actual = rejected_dir.resolve()
    if actual != canonical or not actual.is_relative_to(output_root):
        raise ManifestError(
            f"Rejected-item directory escapes the composite output: {rejected_dir}"
        )
    return output_root, actual


# --------------------------------------------------------------------------- #
# Clickable path helpers                                                      #
# --------------------------------------------------------------------------- #


def _is_tty() -> bool:
    """Return True when stdout is attached to an interactive terminal."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _clickable(path: Path | str, base: Path | None = None) -> str:
    """Return a plain, copyable path for ``path``.

    ``path`` may be relative to ``base`` (defaults to CWD).  Returns the
    resolved absolute path as a plain string so output stays readable in any
    terminal (no OSC 8 escape sequences that can render as raw ``]8;;`` text
    when not interpreted).
    """
    p = Path(path)
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    return str(p.resolve())


# --------------------------------------------------------------------------- #
# Config / data loading                                                       #
# --------------------------------------------------------------------------- #


def _load_known_char_folders(config: GameConfig, reports_dir: Path) -> set[str]:
    """Return the set of known character names from characters.json.

    The file is seeded during compose from Character() declarations and is the
    source of truth for character names thereafter.
    """
    return set(_load_characters(reports_dir).keys())


def _known_character_entries(
    char_aliases: dict[str, dict],
) -> list[tuple[str, str]]:
    """Return ``(canonical_name, display_label)`` entries for known characters."""
    entries: list[tuple[str, str]] = []
    for name in sorted(char_aliases, key=str.casefold):
        aliases = sorted(
            (str(alias) for alias in (char_aliases[name].get("aliases", []) or [])),
            key=str.casefold,
        )
        if aliases:
            label = f"{name}  [aliases: {', '.join(aliases)}]"
        else:
            label = name
        entries.append((name, label))
    return entries


def _known_character_choices(char_aliases: dict[str, dict]) -> list[Choice]:
    """Return selectable known-character choices with canonical values."""
    return [
        Choice(title=label, value=name)
        for name, label in _known_character_entries(char_aliases)
    ]


def _speaker_mapping_choices(
    speaker: str,
    char_aliases: dict[str, dict],
) -> list[Choice]:
    """Return speaker-mapping choices, including custom and unchanged options."""
    return [
        Choice(
            title=f"Keep '{speaker}' name as-is (leave unmapped)",
            value=_KEEP_SPEAKER,
        ),
        Choice(
            title="Enter a custom character name...",
            value=_CUSTOM_SPEAKER,
        ),
        Choice(
            title=f"Remove '{speaker}' dialogs from these composites",
            value=_REMOVE_SPEAKER,
        ),
        *_known_character_choices(char_aliases),
    ]


def _scene_review_choices(
    scene_name: str,
    char_aliases: dict[str, dict],
) -> list[Choice]:
    """Return scene-review actions and selectable known characters."""
    return [
        Choice(
            title=(
                f"Keep scene '{scene_name}' without character association "
                "(remove from pending review only)"
            ),
            value=_KEEP_SCENE_UNASSOCIATED,
        ),
        Choice(
            title=(
                "Remove scene from downstream processing (remove from manifest_scene)"
            ),
            value=_REMOVE_SCENE_FROM_PROCESSING,
        ),
        Choice(
            title="Enter custom character name(s)...",
            value=_CUSTOM_SCENE_CHARACTERS,
        ),
        *_known_character_choices(char_aliases),
    ]


def _resolve_speaker(
    speaker: str,
    char_aliases: dict[str, dict],
    config: GameConfig,
) -> str | None:
    """Resolve a speaker name to a canonical character name.

    Checks:
      1. Direct character name match (case-insensitive).
      2. Player alias (``you``, ``y``) -> ``player_folder`` when configured.
      3. Alias match against characters.json.

    Returns None when the speaker cannot be resolved.
    """
    speaker_lower = speaker.strip().lower()
    if not speaker_lower:
        return None

    # 1. Direct character name match.
    for char_name in char_aliases:
        if char_name.lower() == speaker_lower:
            return char_name

    # 2. Player alias maps to player_folder.
    player_folder = config.player_folder
    player_aliases = set(config.player_aliases)
    if player_folder and speaker_lower in {a.lower() for a in player_aliases}:
        return player_folder

    # 3. Alias match against characters.json.
    for char_name, info_ in char_aliases.items():
        aliases = {a.lower() for a in info_.get("aliases", [])}
        if speaker_lower in aliases:
            return char_name

    return None


# --------------------------------------------------------------------------- #
# Manifest load / save helpers                                                #
# --------------------------------------------------------------------------- #


def _manifest_index_path(reports_dir: Path, manifest_index_path: Path | None) -> Path:
    """Return the explicit index path or its conventional report sibling."""
    index_path = manifest_index_path or (reports_dir / "manifest_index.json")
    try:
        validate_manifest_index_path(index_path, reports_dir)
    except ManifestBundlePathError as exc:
        raise ManifestError(str(exc)) from exc
    return index_path


def _category_manifest_paths(
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> dict[str, Path]:
    """Resolve category paths from the manifest index, with a legacy fallback."""
    index_path = _manifest_index_path(reports_dir, manifest_index_path)
    if not index_path.is_file():
        return {
            category: reports_dir / f"manifest_{category}.json"
            for category in KNOWN_FAMILIES
        }

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"Could not read manifest index {index_path}: {exc}"
        ) from exc
    referenced = resolve_category_manifest_paths(index, index_path, reports_dir)
    return {
        category: referenced.get(
            category,
            reports_dir / f"manifest_{category}.json",
        )
        for category in KNOWN_FAMILIES
    }


def _load_all_category_manifests(
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> dict[str, dict]:
    """Load every referenced category manifest into memory.

    Missing files yield an empty category, while unreadable files are errors;
    corrupt review data must never be silently replaced with empty data.
    """
    index_path = _manifest_index_path(reports_dir, manifest_index_path)
    all_data: dict[str, dict] = {}
    with manifest_bundle_lock(index_path, report_dir=reports_dir):
        recover_manifest_bundle(index_path, reports_dir)
        category_paths = _category_manifest_paths(reports_dir, manifest_index_path)
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ManifestError(
                    f"Could not read manifest index {index_path}: {exc}"
                ) from exc
            referenced = resolve_category_manifest_paths(
                index,
                index_path,
                reports_dir,
            )
            try:
                verify_category_manifest_digests(
                    index,
                    referenced,
                    report_dir=reports_dir,
                    manifest_index_path=index_path,
                )
            except ManifestIntegrityError as exc:
                raise ManifestError(str(exc)) from exc

        for category, category_path in category_paths.items():
            if category_path.is_file():
                try:
                    all_data[category] = json.loads(
                        category_path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    raise ManifestError(
                        f"Could not read manifest category file {category_path}: {exc}"
                    ) from exc
                if not isinstance(all_data[category], dict):
                    raise ManifestError(
                        f"Manifest category file must contain an object: {category_path}"
                    )
                composites = all_data[category].get("composites", [])
                if not isinstance(composites, list):
                    raise ManifestError(
                        f"Manifest category 'composites' must be a list: {category_path}"
                    )
                for composite in composites:
                    if not isinstance(composite, dict):
                        raise ManifestError(
                            f"Manifest category contains an invalid composite: {category_path}"
                        )
            else:
                all_data[category] = {
                    "category": category,
                    "composites": [],
                    "summary": {"composites": 0},
                }
    return all_data


def _prepare_category_update(
    reports_dir: Path,
    all_data: dict[str, dict],
    index_path: Path,
) -> tuple[dict[Path, dict], dict | None, list[Path]]:
    """Prepare category payloads and an updated index for one review action."""
    category_paths = _category_manifest_paths(reports_dir, index_path)

    def comparable(path: Path) -> Path:
        return Path(os.path.abspath(os.path.normpath(os.fspath(path))))

    index_target = comparable(index_path)
    auxiliary_paths = {
        reports_dir / CHARACTERS_FILE,
        reports_dir / "review_pending.json",
        reports_dir / BUNDLE_JOURNAL_NAME,
        reports_dir / BUNDLE_OWNER_REGISTRY_NAME,
    }
    auxiliary_targets = {comparable(path) for path in auxiliary_paths}
    resolved_auxiliary_targets = {path.resolve() for path in auxiliary_paths}
    resolved_index_target = index_path.resolve()
    if (
        index_target in auxiliary_targets
        or resolved_index_target in resolved_auxiliary_targets
    ):
        raise ManifestError(
            f"Manifest index path collides with a bundle file: {index_path}"
        )

    category_targets: dict[Path, str] = {}
    report_root = reports_dir.resolve()
    for category, category_path in category_paths.items():
        resolved_category = category_path.resolve()
        if not resolved_category.is_relative_to(report_root):
            raise ManifestError(
                f"Manifest category {category!r} escapes the report directory: "
                f"{category_path}"
            )
        target = comparable(category_path)
        if target == index_target or resolved_category == resolved_index_target:
            raise ManifestError(
                f"Manifest category {category!r} references the manifest index: "
                f"{category_path}"
            )
        if (
            target in auxiliary_targets
            or resolved_category in resolved_auxiliary_targets
        ):
            raise ManifestError(
                f"Manifest category {category!r} collides with a bundle file: "
                f"{category_path}"
            )
        previous = category_targets.get(resolved_category)
        if previous is not None:
            raise ManifestError(
                "Manifest categories reference the same file: "
                f"{previous!r} and {category!r} -> {category_path}"
            )
        category_targets[resolved_category] = category

    transaction: dict[Path, dict] = {}
    category_payloads: dict[str, dict] = {}
    for category, category_data in all_data.items():
        if category not in category_paths:
            continue
        transaction[category_paths[category]] = category_data
        category_payloads[category] = category_data

    index_payload: dict | None = None
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ManifestError(
                f"Could not read manifest index {index_path}: {exc}"
            ) from exc
        if not isinstance(index, dict):
            raise ManifestError(f"Manifest index must contain an object: {index_path}")

        has_digest_metadata = "category_digests" in index
        previous_digests = index.get("category_digests")
        if has_digest_metadata:
            if not isinstance(previous_digests, dict):
                raise ManifestError(
                    "Manifest index 'category_digests' must be an object"
                )
            try:
                verify_category_manifest_digests(
                    index,
                    category_paths,
                    report_dir=reports_dir,
                    manifest_index_path=index_path,
                )
            except ManifestIntegrityError as exc:
                raise ManifestError(str(exc)) from exc
            digests = dict(previous_digests)
            for category, category_data in category_payloads.items():
                digests[category] = json_payload_digest(category_data)
            index["category_digests"] = digests
        elif all(
            category in category_payloads
            and category in category_paths
            and category_paths[category].is_file()
            for category in KNOWN_FAMILIES
        ):
            # A legacy index may be upgraded to the digest-aware format only
            # when every canonical category is available.  Partial upgrades
            # would make the otherwise-optional metadata mandatory for
            # categories that do not exist.
            index["bundle_version"] = MANIFEST_BUNDLE_VERSION
            index["category_digests"] = {
                category: json_payload_digest(category_payloads[category])
                for category in KNOWN_FAMILIES
            }
        index_payload = index

    allowed_paths = [
        *bundle_paths(index_path, reports_dir),
        *category_paths.values(),
    ]
    return transaction, index_payload, allowed_paths


def _save_all_category_manifests(
    reports_dir: Path,
    all_data: dict[str, dict],
    manifest_index_path: Path | None = None,
) -> None:
    """Atomically write every category manifest and refresh index digests."""
    index_path = _manifest_index_path(reports_dir, manifest_index_path)
    with manifest_bundle_lock(index_path, report_dir=reports_dir):
        recover_manifest_bundle(index_path, reports_dir)
        transaction, index_payload, allowed_paths = _prepare_category_update(
            reports_dir,
            all_data,
            index_path,
        )
        if index_payload is None:
            raise ManifestError(f"Manifest index not found: {index_path}")
        transaction[index_path] = index_payload
        register_bundle_owner(index_path, reports_dir)
        atomic_write_json_files(
            transaction,
            journal_path=bundle_journal_path(index_path, reports_dir),
            allowed_paths=allowed_paths,
            journal_metadata=bundle_journal_metadata(index_path, reports_dir),
        )


def _save_review_bundle(
    reports_dir: Path,
    all_data: dict[str, dict],
    *,
    pending: dict,
    characters: dict[str, dict],
    manifest_index_path: Path | None = None,
) -> None:
    """Publish one logical review action as a single bundle transaction.

    Category records, the character catalog, pending-review state, and the
    refreshed index are staged together.  The index remains the final
    replacement, so consumers observe either the complete old bundle or the
    complete new bundle.
    """
    index_path = _manifest_index_path(reports_dir, manifest_index_path)
    with manifest_bundle_lock(index_path, report_dir=reports_dir):
        recover_manifest_bundle(index_path, reports_dir)
        transaction, index_payload, allowed_paths = _prepare_category_update(
            reports_dir,
            all_data,
            index_path,
        )
        transaction[reports_dir / CHARACTERS_FILE] = characters
        transaction[reports_dir / "review_pending.json"] = pending
        if index_payload is None:
            raise ManifestError(f"Manifest index not found: {index_path}")
        transaction[index_path] = index_payload
        register_bundle_owner(index_path, reports_dir)
        atomic_write_json_files(
            transaction,
            journal_path=bundle_journal_path(index_path, reports_dir),
            allowed_paths=allowed_paths,
            journal_metadata=bundle_journal_metadata(index_path, reports_dir),
        )


def _save_pending_review(
    reports_dir: Path,
    pending: dict,
    manifest_index_path: Path | None = None,
) -> None:
    """Atomically write the pending review data back to disk."""
    index_path = _manifest_index_path(reports_dir, manifest_index_path)
    with manifest_bundle_lock(index_path, report_dir=reports_dir):
        recover_manifest_bundle(index_path, reports_dir)
        if not index_path.is_file():
            raise ManifestError(f"Manifest index not found: {index_path}")
        try:
            index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"Could not read manifest index {index_path}: {exc}"
            ) from exc
        if not isinstance(index_payload, dict):
            raise ManifestError(f"Manifest index must contain an object: {index_path}")
        transaction = {
            reports_dir / "review_pending.json": pending,
            index_path: index_payload,
        }
        register_bundle_owner(index_path, reports_dir)
        atomic_write_json_files(
            transaction,
            journal_path=bundle_journal_path(index_path, reports_dir),
            allowed_paths=bundle_paths(index_path, reports_dir),
            journal_metadata=bundle_journal_metadata(index_path, reports_dir),
        )


# --------------------------------------------------------------------------- #
# Speaker dialog manipulation                                                 #
# --------------------------------------------------------------------------- #


def _remove_speaker_from_dialogs(composite: dict, speaker: str) -> dict:
    """Return a copy of ``composite`` with dialog entries for ``speaker`` removed."""
    new_locs: list[dict] = []
    for loc in composite.get("usage_locations", []):
        new_loc = dict(loc)
        dialog_context = loc.get("dialog_context")
        if dialog_context:
            remaining = [d for d in dialog_context if d.get("speaker") != speaker]
            new_loc["dialog_context"] = remaining if remaining else None
        new_locs.append(new_loc)
    new_comp = dict(composite)
    new_comp["usage_locations"] = new_locs
    return new_comp


def _replace_speaker_in_dialogs(
    composite: dict,
    old_speaker: str,
    new_name: str,
) -> dict:
    """Return a copy of ``composite`` with ``old_speaker`` renamed to ``new_name``
    in dialog context entries."""
    new_locs: list[dict] = []
    for loc in composite.get("usage_locations", []):
        new_loc = dict(loc)
        dialog_context = loc.get("dialog_context")
        if dialog_context:
            remaining = [
                {**d, "speaker": new_name} if d.get("speaker") == old_speaker else d
                for d in dialog_context
            ]
            new_loc["dialog_context"] = remaining if remaining else None
        new_locs.append(new_loc)
    new_comp = dict(composite)
    new_comp["usage_locations"] = new_locs
    return new_comp


def _apply_speaker_mapping_to_manifests(
    all_data: dict[str, dict],
    composite_paths: list[str],
    old_speaker: str,
    new_speaker: str,
) -> None:
    """Update manifests when a speaker is mapped to a character.

    For each composite in ``composite_paths``:
      * Replaces ``old_speaker`` with ``new_speaker`` in dialog_context entries.
      * Removes ``old_speaker`` from unmapped_speakers (clears the field if empty).

    Writes all category manifests back to disk.
    """
    target_set = set(composite_paths)
    for cat_data in all_data.values():
        new_list: list[dict] = []
        for comp in cat_data.get("composites", []):
            if comp.get("composite_path") not in target_set:
                new_list.append(comp)
                continue
            new_comp = _replace_speaker_in_dialogs(comp, old_speaker, new_speaker)
            unmapped = new_comp.get("unmapped_speakers")
            if unmapped:
                remaining = [s for s in unmapped if s != old_speaker]
                new_comp["unmapped_speakers"] = remaining if remaining else None
            new_list.append(new_comp)
        cat_data["composites"] = new_list
        cat_data["summary"] = {"composites": len(new_list)}


def _map_speaker_to_character(
    speaker: str,
    resolved: str,
    char_aliases: dict[str, dict],
    composite_paths: list[str],
    all_data: dict[str, dict],
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> None:
    """Map ``speaker`` to a canonical character and persist the alias."""
    aliases_list = char_aliases[resolved].setdefault("aliases", [])
    speaker_key = speaker.strip().lower()
    if speaker_key not in {alias.lower() for alias in aliases_list}:
        aliases_list.append(speaker_key)
        char_aliases[resolved]["aliases"] = sorted(aliases_list)
    _apply_speaker_mapping_to_manifests(
        all_data,
        composite_paths,
        speaker,
        resolved,
    )


def _keep_unverified_speaker_name(
    speaker: str,
    custom_name: str,
    composite_paths: list[str],
    all_data: dict[str, dict],
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> None:
    """Rename speaker dialogs to an unverified custom name and save manifests."""
    target_set = set(composite_paths)
    for cat_data in all_data.values():
        new_list: list[dict] = []
        for comp in cat_data.get("composites", []):
            if comp.get("composite_path") not in target_set:
                new_list.append(comp)
                continue
            new_list.append(_replace_speaker_in_dialogs(comp, speaker, custom_name))
        cat_data["composites"] = new_list
        cat_data["summary"] = {"composites": len(new_list)}


# --------------------------------------------------------------------------- #
# Speaker rejection                                                           #
# --------------------------------------------------------------------------- #


def _reject_speaker(
    config: GameConfig,
    speaker: str,
    composite_paths: list[str],
    reports_dir: Path,
    rejected_dir: Path,
    all_data: dict[str, dict],
    manifest_index_path: Path | None = None,
) -> ReviewFinalizer:
    """Prepare a speaker rejection and return its post-commit finalizer.

    The manifest mutation stays in memory until the review bundle commits.
    The rejected audit record is written only after that commit succeeds, so a
    failed publication cannot leave a false rejection record behind.
    """
    speaker = str(speaker)
    composite_paths = [str(path) for path in composite_paths]
    # Remove dialogs for this speaker from every affected composite in memory.
    target_set = set(composite_paths)
    for cat_data in all_data.values():
        new_list: list[dict] = []
        for comp in cat_data.get("composites", []):
            if comp.get("composite_path") not in target_set:
                new_list.append(comp)
                continue
            new_comp = _remove_speaker_from_dialogs(comp, speaker)
            # Clear unmapped_speakers since the speaker's dialogs are removed.
            unmapped = new_comp.get("unmapped_speakers")
            if unmapped:
                remaining = [s for s in unmapped if s != speaker]
                new_comp["unmapped_speakers"] = remaining if remaining else None
            new_list.append(new_comp)
        cat_data["composites"] = new_list
        cat_data["summary"] = {"composites": len(new_list)}

    rejected_record = {
        "speaker": speaker,
        "reason": "speaker_not_matched",
        "composites": sorted(composite_paths),
    }
    _output_root, rejected_root = _validated_rejected_dir(config, rejected_dir)
    rejected_path = _rejected_record_path(rejected_root, "speaker", speaker)
    try:
        rejected_path = resolve_under_root(
            rejected_root,
            rejected_path.name,
            field_name="rejected record path",
        )
    except UnsafePathError as exc:
        raise ManifestError(str(exc)) from exc

    def finalize() -> None:
        atomic_write_json(rejected_path, rejected_record)
        info(
            f"Rejected speaker '{speaker}': dialogs removed, record written to "
            f"{rejected_path}"
        )

    return finalize


# --------------------------------------------------------------------------- #
# Scene rejection                                                             #
# --------------------------------------------------------------------------- #


def _reject_scene(
    config: GameConfig,
    composite_path: str,
    composite_name: str,
    reports_dir: Path,
    rejected_dir: Path,
    all_data: dict[str, dict],
    manifest_index_path: Path | None = None,
) -> ReviewFinalizer:
    """Prepare a scene rejection and return its post-commit finalizer.

    The source PNG remains in place until the manifest bundle commits.  The
    finalizer first copies the PNG into the rejected area, then writes the
    audit record, and only then removes the source.  A failed bundle commit
    therefore leaves the old manifest and its PNG mutually consistent.
    """
    composite_path = str(composite_path)
    composite_name = str(composite_name)
    output_root, rejected_root = _validated_rejected_dir(config, rejected_dir)
    try:
        relative_source = safe_relative_path(
            composite_path,
            field_name="composite path",
        )
        lexical_source = config.game_root.parent / relative_source
        source_png = resolve_game_output_path(
            config.game_root,
            output_root,
            composite_path,
            excluded_roots=(
                reports_dir,
                rejected_root,
            ),
        )
    except UnsafePathError as exc:
        raise ManifestError(str(exc)) from exc
    if lexical_source.is_symlink():
        raise ManifestError(
            f"Refusing to reject a symlinked composite image: {composite_path}"
        )

    try:
        rejected_png = resolve_under_root(
            output_root,
            Path(REJECTED_SUBFOLDER) / REJECTED_PNGS_SUBFOLDER / source_png.name,
            field_name="rejected PNG path",
        )
    except UnsafePathError as exc:
        raise ManifestError(str(exc)) from exc

    # Remove the composite from all category manifests in memory.
    target_path = composite_path
    for cat_data in all_data.values():
        cat_data["composites"] = [
            comp
            for comp in cat_data.get("composites", [])
            if comp.get("composite_path") != target_path
        ]
        cat_data["summary"] = {
            "composites": len(cat_data["composites"]),
        }

    rejected_record = {
        "composite_path": composite_path,
        "name": composite_name,
        "reason": "scene_not_associated",
    }
    rejected_path = _rejected_record_path(rejected_root, "scene", composite_name)
    try:
        rejected_path = resolve_under_root(
            rejected_root,
            rejected_path.name,
            field_name="rejected record path",
        )
    except UnsafePathError as exc:
        raise ManifestError(str(exc)) from exc

    def finalize() -> None:
        if source_png.is_file():
            atomic_copy_file(source_png, rejected_png)
            info(f"Copied rejected PNG to {rejected_png}")
        else:
            warning(f"PNG not found at {source_png} (may already be moved)")
        atomic_write_json(rejected_path, rejected_record)
        if source_png.is_file():
            source_png.unlink()
        info(f"Rejected scene '{composite_name}': record written to {rejected_path}")

    return finalize


# --------------------------------------------------------------------------- #
# Character assignment to a scene composite                                   #
# --------------------------------------------------------------------------- #


def _append_characters_to_scene_composite(
    all_data: dict[str, dict],
    composite_path: str,
    character_folders: list[str],
) -> list[str] | None:
    """Append character associations to the matching scene manifest entry.

    Only the corresponding ``manifest_scene.json`` record is updated. Existing
    character records and their evidence are preserved, while each new
    character is appended with ``manual_review`` evidence. The scene is marked
    as associated and its unresolved speakers are cleared.

    Returns the character names that were newly appended. Returns ``None``
    when the matching scene entry does not exist, so callers do not remove the
    scene from pending review.
    """
    scene_data = all_data.get("scene")
    if scene_data is None:
        warning("Scene manifest is missing; character association was not saved.")
        return None

    scene_composites = scene_data.get("composites", [])
    for index, composite in enumerate(scene_composites):
        if composite.get("composite_path") != composite_path:
            continue

        comp = dict(composite)
        existing_entries = [
            dict(entry)
            for entry in (comp.get("characters") or [])
            if isinstance(entry, dict)
        ]
        existing_names = {
            str(entry.get("character"))
            for entry in existing_entries
            if entry.get("character")
        }

        appended: list[str] = []
        for character_folder in character_folders:
            name = character_folder.strip()
            if not name or name in existing_names:
                continue
            existing_entries.append({"character": name, "evidence": ["manual_review"]})
            existing_names.add(name)
            appended.append(name)

        comp["characters"] = existing_entries
        comp["character_association_status"] = "associated"
        comp["unmapped_speakers"] = None
        scene_composites[index] = comp
        return appended

    warning(
        f"Scene '{composite_path}' not found in manifest_scene.json; "
        "character association was not saved."
    )
    return None


# --------------------------------------------------------------------------- #
# Phase 1: unmapped speakers                                                  #
# --------------------------------------------------------------------------- #


def _process_unmapped_speakers(
    config: GameConfig,
    pending: dict,
    rejected_dir: Path,
    char_aliases: dict[str, dict],
    all_data: dict[str, dict],
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> dict[str, dict]:
    """Interactively review each unmapped speaker one at a time."""
    unmapped = pending.get("unmapped_speakers", [])
    if not unmapped:
        info("No unmapped speakers need review.")
        return char_aliases

    for entry in list(unmapped):
        speaker = entry["speaker"]
        composite_paths = entry["composites"]

        console.print()
        console.print(f"[cyan]Speaker:[/cyan] [bold]{speaker}[/bold]")
        kv("Used by", counted(len(composite_paths), "composite"))
        kv("Known characters", counted(len(char_aliases), "character"))
        composite_paths_display = [
            _clickable(comp_path, config.game_root.parent)
            for comp_path in composite_paths
        ]
        character_choice = select_with_context(
            f"Map speaker '{speaker}' to a character",
            context_title=f"Related composites for '{speaker}'",
            context_items=composite_paths_display,
            choices=_speaker_mapping_choices(speaker, char_aliases),
        )
        if character_choice is None:
            info("Review cancelled. Your completed changes were kept.")
            return char_aliases

        outcome = "kept"
        finalizer: ReviewFinalizer | None = None
        if character_choice == _KEEP_SPEAKER:
            pass
        elif character_choice == _REMOVE_SPEAKER:
            finalizer = _reject_speaker(
                config,
                speaker,
                composite_paths,
                reports_dir,
                rejected_dir,
                all_data,
                manifest_index_path,
            )
            outcome = "removed"
        elif character_choice == _CUSTOM_SPEAKER:
            char_name = prompt_text(
                f"Enter a custom character name for '{speaker}' "
                "(leave blank to keep the speaker name as-is)",
                default="",
            )
            if char_name is None:
                info("Review cancelled. Your completed changes were kept.")
                return char_aliases

            char_name = char_name.strip()
            if not char_name:
                pass
            else:
                resolved = _resolve_speaker(char_name, char_aliases, config)
                if resolved:
                    _map_speaker_to_character(
                        speaker,
                        resolved,
                        char_aliases,
                        composite_paths,
                        all_data,
                        reports_dir,
                        manifest_index_path,
                    )
                    outcome = "mapped"
                else:
                    warning(f"'{char_name}' is not a known character.")
                    answer = confirm(
                        f"Remove dialog associations for '{speaker}'?", default=False
                    )
                    if answer is None:
                        info("Review cancelled. Your completed changes were kept.")
                        return char_aliases
                    if answer:
                        finalizer = _reject_speaker(
                            config,
                            speaker,
                            composite_paths,
                            reports_dir,
                            rejected_dir,
                            all_data,
                            manifest_index_path,
                        )
                        outcome = "removed"
                    else:
                        _keep_unverified_speaker_name(
                            speaker,
                            char_name,
                            composite_paths,
                            all_data,
                            reports_dir,
                            manifest_index_path,
                        )
                        outcome = "renamed"
        else:
            _map_speaker_to_character(
                speaker,
                character_choice,
                char_aliases,
                composite_paths,
                all_data,
                reports_dir,
                manifest_index_path,
            )
            outcome = "mapped"

        # The category, alias catalog, pending state, and refreshed index form
        # one logical review action and are therefore published together.
        pending["unmapped_speakers"] = [
            e
            for e in pending.get("unmapped_speakers", [])
            if e.get("speaker") != speaker
        ]
        _save_review_bundle(
            reports_dir,
            all_data,
            pending=pending,
            characters=char_aliases,
            manifest_index_path=manifest_index_path,
        )
        if finalizer is not None:
            finalizer()

        if outcome == "mapped":
            success(f"Mapped '{speaker}' to a character and added the alias.")
        elif outcome == "removed":
            info(f"Removed '{speaker}' from the affected composites.")
        elif outcome == "renamed":
            info(f"Updated '{speaker}' to the selected unverified name.")
        else:
            info(f"Kept '{speaker}' name as-is. No composite changes were made.")

    return char_aliases


# --------------------------------------------------------------------------- #
# Phase 2: scenes missing character association                               #
# --------------------------------------------------------------------------- #


def _process_scenes_missing_chars(
    config: GameConfig,
    pending: dict,
    rejected_dir: Path,
    all_data: dict[str, dict],
    reports_dir: Path,
    manifest_index_path: Path | None = None,
) -> None:
    """Interactively review each scene missing character association."""
    scenes = pending.get("scenes", [])
    if not scenes:
        info("All scene composites have character associations.")
        return

    char_aliases = _load_characters(reports_dir)

    for entry in list(scenes):
        composite_path = entry["composite_path"]
        name = entry["name"]

        console.print()
        console.print(f"[cyan]Scene:[/cyan] [bold]{name}[/bold]")
        composite_display = _clickable(composite_path, config.game_root.parent)
        definition = f"{entry['definition']['script']}:{entry['definition']['line']}"
        usage_locations = [
            f"{loc['script']}:{loc['line']}  {loc['statement']}"
            for loc in entry.get("usage_locations", [])
        ]
        context_items = [definition, *usage_locations]
        finalizer: ReviewFinalizer | None = None

        while True:
            selections = select_multiple_with_context(
                f"Associate character(s) with scene '{name}'",
                context_title="Defined at",
                context_metadata=[f"Composite: {composite_display}"],
                context_item_noun="location",
                context_items=context_items,
                choices=_scene_review_choices(name, char_aliases),
                exclusive_values=(
                    _KEEP_SCENE_UNASSOCIATED,
                    _REMOVE_SCENE_FROM_PROCESSING,
                    _CUSTOM_SCENE_CHARACTERS,
                ),
            )
            if selections is None:
                info("Review cancelled. Your completed changes were kept.")
                return

            outcome_message = ""
            if selections == [_KEEP_SCENE_UNASSOCIATED]:
                outcome_message = (
                    f"Kept '{name}' without character associations. "
                    "Only its pending-review entry was removed."
                )
                break

            if selections == [_REMOVE_SCENE_FROM_PROCESSING]:
                finalizer = _reject_scene(
                    config,
                    composite_path,
                    name,
                    reports_dir,
                    rejected_dir,
                    all_data,
                    manifest_index_path,
                )
                outcome_message = f"Removed scene '{name}' from processing."
                break

            if selections == [_CUSTOM_SCENE_CHARACTERS]:
                char_name = prompt_text(
                    f"Enter one or more character names for '{name}' "
                    "(comma-separated). Leave blank to keep the scene unassociated.",
                    default="",
                )
                if char_name is None:
                    info("Review cancelled. Your completed changes were kept.")
                    return

                names = [name.strip() for name in char_name.split(",") if name.strip()]
                if not names:
                    outcome_message = (
                        f"Kept '{name}' without character associations. "
                        "Only its pending-review entry was removed."
                    )
                    break

                valid_resolved: list[str] = []
                invalid_names: list[str] = []
                for entered_name in names:
                    resolved = _resolve_speaker(
                        entered_name,
                        char_aliases,
                        config,
                    )
                    if resolved:
                        valid_resolved.append(resolved)
                    else:
                        invalid_names.append(entered_name)

                if invalid_names:
                    warning(f"Unknown character name(s): {', '.join(invalid_names)}")
                    answer = confirm(
                        "Use the unknown name(s) as unverified character associations?",
                        default=False,
                    )
                    if answer is None:
                        info("Review cancelled. Your completed changes were kept.")
                        return
                    if not answer:
                        continue

                associated_characters = [*valid_resolved, *invalid_names]
                appended = _append_characters_to_scene_composite(
                    all_data,
                    composite_path,
                    associated_characters,
                )
                if appended is None:
                    info(
                        f"Scene '{name}' remains pending because its "
                        "manifest_scene.json entry was not found."
                    )
                    continue
                outcome_message = (
                    f"Appended {', '.join(associated_characters)} to scene "
                    f"'{name}' in manifest_scene.json."
                )
                break

            # Known character selection. The multi-select workspace may return
            # more than one character for scenes that contain multiple people.
            appended = _append_characters_to_scene_composite(
                all_data,
                composite_path,
                selections,
            )
            if appended is None:
                info(
                    f"Scene '{name}' remains pending because its "
                    "manifest_scene.json entry was not found."
                )
                continue
            outcome_message = (
                f"Appended {', '.join(selections)} to scene '{name}' "
                "in manifest_scene.json."
            )
            break

        # Publish the category update and pending-state removal together.
        pending["scenes"] = [
            e
            for e in pending.get("scenes", [])
            if e.get("composite_path") != composite_path
        ]
        _save_review_bundle(
            reports_dir,
            all_data,
            pending=pending,
            characters=char_aliases,
            manifest_index_path=manifest_index_path,
        )
        if finalizer is not None:
            finalizer()
        info(outcome_message)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def _print_next_step(config: GameConfig) -> None:
    """Print the next command appropriate to the current compose state."""
    if ManifestWriter.has_composite_manifest(config):
        info("Next step: apply the composites to the game scripts.")
        command("mise run compose apply")
    else:
        info("Next step: create the composites before reviewing them.")
        command("mise run compose")


def review(config_path: str = "src/converter/config.json") -> int:
    """Run the interactive review process under the manifest bundle lock."""
    if not is_interactive():
        error("The interactive review requires an interactive terminal.")
        info("Run the review in a terminal with 'mise run compose review'.")
        return 1

    config = load_config(config_path)
    paths = config.resolved_paths()
    index_path = _manifest_index_path(
        paths["report_dir"],
        paths["manifest_index_path"],
    )
    with manifest_bundle_lock(
        index_path,
        report_dir=paths["report_dir"],
    ):
        recover_manifest_bundle(
            index_path,
            paths["report_dir"],
        )
        return _review_unlocked(config_path)


def _review_unlocked(config_path: str = "src/converter/config.json") -> int:
    """Run the interactive review body while the bundle lock is held."""
    if not is_interactive():
        error("The interactive review requires an interactive terminal.")
        info("Run the review in a terminal with 'mise run compose review'.")
        return 1

    config = load_config(config_path)
    pending = ManifestWriter.load_pending_review(config)

    if not pending:
        info("No review items are pending.")
        _print_next_step(config)
        return 0

    paths = config.resolved_paths()
    reports_dir = paths["report_dir"]
    manifest_index_path = paths["manifest_index_path"]
    rejected_dir = paths["output_dir"] / REJECTED_SUBFOLDER

    char_aliases = _load_characters(reports_dir)
    all_data = _load_all_category_manifests(reports_dir, manifest_index_path)

    rule("Composite review")
    kv("Config", config_path)
    kv("Rejected items folder", str(rejected_dir))
    console.print()

    # Phase 1: Review unmapped speakers.
    if pending.get("unmapped_speakers"):
        rule("Phase 1:Unmapped Speakers")
    console.print("[cyan]Map Speakers to characters")
    if pending.get("unmapped_speakers"):
        heading, instructions = manual_speaker_instructions(reports_dir)
        print_panel(
            Group(*instructions),
            title=f"Phase 1 - {heading}",
            style=MAUVE,
        )
    char_aliases = _process_unmapped_speakers(
        config,
        pending,
        rejected_dir,
        char_aliases,
        all_data,
        reports_dir,
        manifest_index_path,
    )

    # Phase 2: Review scenes missing character association.
    if pending.get("scenes"):
        rule("Phase 2: Associate characters with scenes")
    console.print("[cyan]Missing scene and character association[/cyan]")
    if pending.get("scenes"):
        heading, instructions = manual_scene_instructions(reports_dir)
        print_panel(
            Group(*instructions),
            title=f"Phase 2 - {heading}",
            style=MAUVE,
        )
    _process_scenes_missing_chars(
        config,
        pending,
        rejected_dir,
        all_data,
        reports_dir,
        manifest_index_path,
    )

    console.print()
    print_summary(
        "Review complete",
        [
            ("Rejected records", str(rejected_dir)),
            ("Characters", str(reports_dir / CHARACTERS_FILE)),
        ],
    )
    console.print()
    info("Next step: apply the reviewed composites to the game scripts.")
    command("mise run compose apply")
    return 0


@app.command()
def main(
    config_path: str = typer.Argument(
        "src/converter/config.json",
        help="Path to the GameConfig JSON (default: src/converter/config.json)",
    ),
) -> int:
    """Typer wrapper around :func:`review` for CLI use."""
    return review(config_path)


if __name__ == "__main__":
    sys.exit(app())

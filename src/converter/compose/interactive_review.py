"""Interactive review CLI for scene character associations and speaker resolution.

Reviews unmapped speakers and scenes missing character associations one at a
time, with clickable composite file links and interactive prompts.

Mise task:

    mise run compose-review

Run from the repository root.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .config import GameConfig, load_config
from .manifest import (
    CHARACTER_ALIASES_FILE,
    KNOWN_FAMILIES,
    ManifestWriter,
    _load_character_aliases,
    _save_character_aliases,
)

REJECTED_SUBFOLDER = "rejected"
REJECTED_PNGS_SUBFOLDER = "pngs"


# --------------------------------------------------------------------------- #
# Clickable path helpers
# --------------------------------------------------------------------------- #


def _is_tty() -> bool:
    """Return True when stdout is attached to an interactive terminal."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _clickable(path: Path | str, base: Path | None = None) -> str:
    """Return a clickable link for ``path``.

    ``path`` may be relative to ``base`` (defaults to CWD).  Uses ANSI OSC 8
    hyperlinks when stdout is a TTY (supported by Windows Terminal, VS Code,
    iTerm2, kitty, alacritty, etc.).  Falls back to the plain file:// URI
    otherwise so the path is still readable/copyable.
    """
    p = Path(path)
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
        p = p.resolve()
    uri = p.as_uri()
    label = str(p)
    if _is_tty():
        # OSC 8 hyperlink: ESC ] 8 ; ; URI BEL  LABEL  ESC ] 8 ; ; BEL
        return f"\033]8;;{uri}\033\\{label}\033]8;;\033\\"
    return uri


# --------------------------------------------------------------------------- #
# Config / data loading
# --------------------------------------------------------------------------- #

def _load_known_char_folders(config: GameConfig, reports_dir: Path) -> set[str]:
    """Return the set of known character names from character_aliases.json.

    The file is seeded during compose (from chars/ folders and Character()
    declarations) and is the source of truth for character names thereafter.
    Includes ``narrator`` so it can be selected as a valid character name
    during interactive review even though it has no asset folder.
    """
    return set(_load_character_aliases(reports_dir).keys())


def _resolve_speaker(
    speaker: str, char_aliases: dict[str, dict], config: GameConfig,
) -> str | None:
    """Resolve a speaker name to a canonical character name.

    Checks:
      1. Direct character name match (case-insensitive).
      2. Player alias (``you``, ``y``) -> ``player_folder`` when configured.
      3. Alias match against character_aliases.json.

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

    # 3. Alias match against character_aliases.json.
    for char_name, info in char_aliases.items():
        aliases = {a.lower() for a in info.get("aliases", [])}
        if speaker_lower in aliases:
            return char_name

    return None


# --------------------------------------------------------------------------- #
# Manifest load / save helpers
# --------------------------------------------------------------------------- #

def _load_all_category_manifests(reports_dir: Path) -> dict[str, dict]:
    """Load every ``manifest_<cat>.json`` file into memory.

    Returns ``{cat_name: cat_data}`` covering all known families.  Missing or
    unreadable files yield an empty composite list for that category.
    """
    all_data: dict[str, dict] = {}
    for cat_name in KNOWN_FAMILIES:
        cat_path = reports_dir / f"manifest_{cat_name}.json"
        if cat_path.is_file():
            try:
                all_data[cat_name] = json.loads(
                    cat_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                all_data[cat_name] = {
                    "category": cat_name,
                    "composites": [],
                    "summary": {"composites": 0},
                }
        else:
            all_data[cat_name] = {
                "category": cat_name,
                "composites": [],
                "summary": {"composites": 0},
            }
    return all_data


def _save_all_category_manifests(reports_dir: Path, all_data: dict[str, dict]) -> None:
    """Write every category manifest back to disk."""
    for cat_name, cat_data in all_data.items():
        cat_path = reports_dir / f"manifest_{cat_name}.json"
        cat_path.write_text(json.dumps(cat_data, indent=2), encoding="utf-8")


def _save_pending_review(reports_dir: Path, pending: dict) -> None:
    """Write the pending review data back to ``scene_review_pending.json``.

    Resolved entries (mapped/rejected speakers, associated/rejected scenes)
    are removed from this file so they are not re-reviewed on subsequent runs.
    """
    pending_path = reports_dir / "scene_review_pending.json"
    pending_path.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def _find_composite_index(
    all_data: dict[str, dict], composite_path: str,
) -> tuple[str, int] | None:
    """Return ``(cat_name, index)`` of the composite with ``composite_path``."""
    for cat_name, cat_data in all_data.items():
        for i, comp in enumerate(cat_data.get("composites", [])):
            if comp.get("composite_path") == composite_path:
                return cat_name, i
    return None


# --------------------------------------------------------------------------- #
# Speaker dialog manipulation
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
    composite: dict, old_speaker: str, new_name: str,
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


# --------------------------------------------------------------------------- #
# Speaker rejection
# --------------------------------------------------------------------------- #

def _reject_speaker(
    config: GameConfig,
    speaker: str,
    composite_paths: list[str],
    reports_dir: Path,
    rejected_dir: Path,
    all_data: dict[str, dict],
) -> None:
    """Reject an unmapped speaker: remove its dialogs from manifests and write a
    rejected record under ``rejected_dir``."""
    # Remove dialogs for this speaker from every affected composite in memory.
    target_set = set(composite_paths)
    for cat_data in all_data.values():
        new_list: list[dict] = []
        for comp in cat_data.get("composites", []):
            if comp.get("composite_path") not in target_set:
                new_list.append(comp)
                continue
            new_list.append(_remove_speaker_from_dialogs(comp, speaker))
        cat_data["composites"] = new_list
        cat_data["summary"] = {"composites": len(new_list)}

    _save_all_category_manifests(reports_dir, all_data)

    # Write rejected record.
    rejected_dir.mkdir(parents=True, exist_ok=True)
    rejected_record = {
        "speaker": speaker,
        "reason": "speaker_not_matched",
        "composites": sorted(composite_paths),
    }
    rejected_path = rejected_dir / f"speaker_{speaker}.json"
    rejected_path.write_text(json.dumps(rejected_record, indent=2), encoding="utf-8")
    print(f"  -> Rejected speaker '{speaker}': dialogs removed, "
          f"record written to {rejected_path}")


# --------------------------------------------------------------------------- #
# Scene rejection
# --------------------------------------------------------------------------- #

def _reject_scene(
    config: GameConfig,
    composite_path: str,
    composite_name: str,
    reports_dir: Path,
    rejected_dir: Path,
    all_data: dict[str, dict],
) -> None:
    """Reject a scene composite: move the PNG to rejected/pngs/, remove its
    manifest entry, and write a rejected record."""
    game_root_parent = config.game_root.parent

    # Move the PNG file.
    source_png = game_root_parent / composite_path
    rejected_png_dir = rejected_dir / REJECTED_PNGS_SUBFOLDER
    rejected_png_dir.mkdir(parents=True, exist_ok=True)
    rejected_png = rejected_png_dir / Path(composite_path).name

    if source_png.is_file():
        shutil.move(str(source_png), str(rejected_png))
        print(f"  -> Moved PNG to {rejected_png}")
    else:
        print(f"  -> PNG not found at {source_png} (may already be moved)")

    # Remove the composite from all category manifests in memory.
    target_path = composite_path
    for cat_data in all_data.values():
        cat_data["composites"] = [
            comp for comp in cat_data.get("composites", [])
            if comp.get("composite_path") != target_path
        ]
        cat_data["summary"] = {
            "composites": len(cat_data["composites"]),
        }

    _save_all_category_manifests(reports_dir, all_data)

    # Write rejected record.
    rejected_dir.mkdir(parents=True, exist_ok=True)
    rejected_record = {
        "composite_path": composite_path,
        "name": composite_name,
        "reason": "scene_not_associated",
    }
    rejected_path = rejected_dir / f"scene_{composite_name}.json"
    rejected_path.write_text(json.dumps(rejected_record, indent=2), encoding="utf-8")
    print(f"  -> Rejected scene '{composite_name}': record written to {rejected_path}")


# --------------------------------------------------------------------------- #
# Character assignment to a scene composite
# --------------------------------------------------------------------------- #

def _assign_character_to_scene_composite(
    all_data: dict[str, dict], composite_path: str, character_folder: str,
) -> None:
    """Update the manifest entry for ``composite_path`` with the given character.

    Appends to any existing character associations rather than replacing them,
    so multiple characters can be associated with one scene.
    """
    found = _find_composite_index(all_data, composite_path)
    if found is None:
        print(f"  Warning: composite '{composite_path}' not found in manifests.")
        return
    cat_name, index = found
    comp = dict(all_data[cat_name]["composites"][index])
    existing = [c["character"] for c in comp.get("characters", [])]
    if character_folder not in existing:
        existing.append(character_folder)
    comp["characters"] = [
        {"character": c, "evidence": ["manual_review"]} for c in existing
    ]
    comp["character_association_status"] = "associated"
    comp["unmapped_speakers"] = None
    all_data[cat_name]["composites"][index] = comp


# --------------------------------------------------------------------------- #
# Phase 1: unmapped speakers
# --------------------------------------------------------------------------- #

def _process_unmapped_speakers(
    config: GameConfig,
    pending: dict,
    rejected_dir: Path,
    char_aliases: dict[str, dict],
    all_data: dict[str, dict],
    reports_dir: Path,
) -> dict[str, dict]:
    """Interactively review each unmapped speaker one at a time."""
    unmapped = pending.get("unmapped_speakers", [])
    if not unmapped:
        print("No unmapped speakers to review.")
        return char_aliases

    known_chars = set(char_aliases.keys())
    char_list = ", ".join(sorted(known_chars))

    for entry in list(unmapped):
        speaker = entry["speaker"]
        composite_paths = entry["composites"]

        print(f"\n{'=' * 60}")
        print(f"Unmapped speaker: {speaker}")
        print(f"Found in {len(composite_paths)} composite(s):")
        for comp_path in composite_paths:
            print(f"  {_clickable(comp_path, config.game_root.parent)}")

        char_name = input(
            f"\nEnter character name for '{speaker}' "
            f"(valid: {char_list}, or blank to skip): "
        ).strip()

        if not char_name:
            # Blank input -> ask to remove dialogs
            answer = input(
                f"Remove dialog associations for '{speaker}' from these composites? (y/N): "
            ).strip().lower()
            if answer in ("y", "yes"):
                _reject_speaker(
                    config, speaker, composite_paths, reports_dir, rejected_dir, all_data,
                )
            else:
                print(f"  Kept '{speaker}' as unmapped (no changes).")
        else:
            # Validate entered character name.
            resolved = _resolve_speaker(char_name, char_aliases, config)
            if resolved:
                # Valid character: add speaker as alias (append, do not overwrite).
                aliases_list = char_aliases[resolved].setdefault("aliases", [])
                speaker_key = speaker.strip().lower()
                if speaker_key not in {a.lower() for a in aliases_list}:
                    aliases_list.append(speaker_key)
                    char_aliases[resolved]["aliases"] = sorted(aliases_list)
                _save_character_aliases(reports_dir, char_aliases)
                print(f"  Mapped '{speaker}' -> '{resolved}' (alias added).")
            else:
                # Invalid character name -> ask to remove dialogs.
                print(f"  '{char_name}' is not a known character folder.")
                answer = input(
                    f"Remove dialog associations for '{speaker}'? (y/N): "
                ).strip().lower()
                if answer in ("y", "yes"):
                    _reject_speaker(
                        config, speaker, composite_paths, reports_dir, rejected_dir, all_data,
                    )
                else:
                    # Keep as-is with entered name (update dialogs only, no alias mapping).
                    target_set = set(composite_paths)
                    for cat_data in all_data.values():
                        new_list: list[dict] = []
                        for comp in cat_data.get("composites", []):
                            if comp.get("composite_path") not in target_set:
                                new_list.append(comp)
                                continue
                            new_list.append(
                                _replace_speaker_in_dialogs(comp, speaker, char_name)
                            )
                        cat_data["composites"] = new_list
                        cat_data["summary"] = {"composites": len(new_list)}
                    _save_all_category_manifests(reports_dir, all_data)
                    print(f"  Kept '{speaker}' -> '{char_name}' (unverified alias, dialogs updated).")

        # Remove this speaker from the pending review so it is not re-reviewed.
        pending["unmapped_speakers"] = [
            e for e in pending.get("unmapped_speakers", [])
            if e.get("speaker") != speaker
        ]
        _save_pending_review(reports_dir, pending)

    return char_aliases


# --------------------------------------------------------------------------- #
# Phase 2: scenes missing character association
# --------------------------------------------------------------------------- #

def _process_scenes_missing_chars(
    config: GameConfig,
    pending: dict,
    rejected_dir: Path,
    all_data: dict[str, dict],
    reports_dir: Path,
) -> None:
    """Interactively review each scene missing character association."""
    scenes = pending.get("scenes", [])
    if not scenes:
        print("No scenes missing character association.")
        return

    char_aliases = _load_character_aliases(reports_dir)
    known_chars = set(char_aliases.keys())
    char_list = ", ".join(sorted(known_chars))

    for entry in list(scenes):
        composite_path = entry["composite_path"]
        name = entry["name"]

        print(f"\n{'=' * 60}")
        print(f"Scene composite missing characters: {name}")
        print(f"  Path: {_clickable(composite_path, config.game_root.parent)}")
        print(f"  Defined: {entry['definition']['script']}:{entry['definition']['line']}")
        print("  Usage locations:")
        for loc in entry.get("usage_locations", []):
            print(f"    - {loc['script']}:{loc['line']}  {loc['statement']}")

        char_name = input(
            f"\nEnter character name for '{name}' "
            f"(valid: {char_list}, or blank to skip): "
        ).strip()

        if not char_name:
            # Blank input -> ask to keep without characters.
            answer = input(
                f"Keep scene '{name}' in manifest without character associations? (y/N): "
            ).strip().lower()
            if answer in ("y", "yes"):
                print(f"  Kept '{name}' without characters (no changes).")
            else:
                _reject_scene(
                    config, composite_path, name, reports_dir, rejected_dir, all_data,
                )
        else:
            # Validate comma-separated character names.
            names = [n.strip() for n in char_name.split(",") if n.strip()]
            valid_resolved: list[str] = []
            invalid_names: list[str] = []
            for n in names:
                resolved = _resolve_speaker(n, char_aliases, config)
                if resolved:
                    valid_resolved.append(resolved)
                else:
                    invalid_names.append(n)

            if invalid_names:
                print(f"  Not recognized: {', '.join(invalid_names)}")
                answer = input(
                    f"Keep scene '{name}' without these character associations? (y/N): "
                ).strip().lower()
                if answer in ("y", "yes"):
                    # Assign valid ones; for invalid ones, use the entered name as-is.
                    for r in valid_resolved:
                        _assign_character_to_scene_composite(
                            all_data, composite_path, r
                        )
                    for n in invalid_names:
                        _assign_character_to_scene_composite(
                            all_data, composite_path, n
                        )
                    _save_all_category_manifests(reports_dir, all_data)
                    print(f"  Assigned {', '.join(names)} to scene '{name}'.")
                else:
                    _reject_scene(
                        config, composite_path, name, reports_dir, rejected_dir, all_data,
                    )
            else:
                # All names are valid characters.
                for r in valid_resolved:
                    _assign_character_to_scene_composite(
                        all_data, composite_path, r
                    )
                _save_all_category_manifests(reports_dir, all_data)
                print(f"  Assigned {', '.join(valid_resolved)} to scene '{name}'.")

        # Remove this scene from the pending review so it is not re-reviewed.
        pending["scenes"] = [
            e for e in pending.get("scenes", [])
            if e.get("composite_path") != composite_path
        ]
        _save_pending_review(reports_dir, pending)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def review(config_path: str = "src/converter/config.json") -> int:
    """Run the interactive review process.

    Phase 1 reviews unmapped speakers one at a time.
    Phase 2 reviews scenes missing character association one at a time.
    """
    config = load_config(config_path)
    pending = ManifestWriter.load_pending_review(config)

    if not pending:
        print("No pending scene review data found. Run compose first.")
        return 0

    reports_dir = config.resolved_paths()["manifest_path"].parent
    rejected_dir = config.game_root / "composites" / REJECTED_SUBFOLDER

    char_aliases = _load_character_aliases(reports_dir)
    all_data = _load_all_category_manifests(reports_dir)

    print("=== Interactive Scene Review ===")
    print(f"Config: {config_path}")
    print(f"Rejected folder: {rejected_dir}")
    print()

    # Phase 1: Review unmapped speakers.
    char_aliases = _process_unmapped_speakers(
        config, pending, rejected_dir, char_aliases, all_data, reports_dir,
    )

    # Phase 2: Review scenes missing character association.
    _process_scenes_missing_chars(config, pending, rejected_dir, all_data, reports_dir)

    print("\n=== Review Complete ===")
    print(f"Rejected records written to: {rejected_dir}")
    print(f"Character aliases saved to: {reports_dir / CHARACTER_ALIASES_FILE}")
    print()
    print("Next steps:")
    print("  Run:    mise run apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    config_path = argv[0] if argv else "src/converter/config.json"
    return review(config_path)


if __name__ == "__main__":
    sys.exit(main())
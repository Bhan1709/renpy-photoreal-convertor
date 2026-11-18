"""Reusable manual-review guidance for the two compose review phases."""

from __future__ import annotations

from pathlib import Path

from rich.console import RenderableType
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text


def manual_scene_instructions(
    reports_dir: Path,
) -> tuple[str, list[RenderableType]]:
    """Return the manual fallback instructions for scene associations."""
    pending_path = reports_dir / "review_pending.json"
    scene_manifest = reports_dir / "manifest_scene.json"
    scene_json = (
        "{\n"
        '  "characters": [\n'
        '    {"character": "<folder>", '
        '"evidence": ["script_identifier:<script>:<line>"]}\n'
        "  ],\n"
        '  "character_association_status": "associated"\n'
        "}"
    )

    return (
        "Manual scene resolution",
        [
            Padding(Text("Interactive review outcomes:", style="bold")),
            Padding(
                Text(
                    "- Keep the scene unassociated: remove only its "
                    "review_pending.json entry."
                ),
                (0, 2),
            ),
            Padding(
                Text(
                    "- Remove the scene from downstream processing: remove its "
                    "manifest_scene.json entry."
                ),
                (0, 2),
            ),
            Padding(
                Text(
                    "- Select one or more characters: append them to the "
                    "matching scene's characters list in manifest_scene.json."
                ),
                (0, 2),
            ),
            Padding(
                Text(
                    "- Custom names resolve known aliases; unknown names are "
                    "appended as unverified associations."
                ),
                (0, 2),
            ),
            Padding(
                Text(
                    "Manual alternative (the interactive review writes these files "
                    "automatically):"
                )
            ),
            Padding(Text("- Associate each scene with one or more characters:")),
            Padding(Text(f"- Open {scene_manifest}."), (0, 2)),
            Padding(
                Text('- Find the scene in the "composites" list and add:'),
                (0, 2),
            ),
            Padding(
                Syntax(
                    scene_json,
                    "json",
                    theme="ansi_dark",
                    background_color="default",
                    word_wrap=True,
                    padding=(0, 1),
                ),
                (0, 3),
            ),
            Padding(
                Text(
                    "- Remove the resolved entry from the 'scenes' array in "
                    f"{pending_path}."
                ),
                (0, 2),
            ),
        ],
    )


def manual_speaker_instructions(
    reports_dir: Path,
) -> tuple[str, list[RenderableType]]:
    """Return the manual fallback instructions for speaker mappings."""
    pending_path = reports_dir / "review_pending.json"
    aliases_path = reports_dir / "character_aliases.json"
    speaker_json = (
        "{\n"
        '  "clothes": {\n'
        '    "folder": "archive/chars/clothes",\n'
        '    "aliases": ["clothes"]\n'
        "  }\n"
        "}"
    )

    return (
        "Manual speaker resolution",
        [
            Padding(
                Text(
                    "Manual alternative (the interactive review writes these files "
                    "automatically):"
                )
            ),
            Padding(Text("- Map each speaker to the appropriate character:")),
            Padding(Text(f"- Open {aliases_path}."), (0, 2)),
            Padding(
                Text("- Add the speaker under its character folder, for example:"),
                (0, 2),
            ),
            Padding(
                Syntax(
                    speaker_json,
                    "json",
                    theme="ansi_dark",
                    background_color="default",
                    word_wrap=True,
                    padding=(0, 1),
                ),
                (0, 3),
            ),
            Padding(
                Text(
                    "- Remove the resolved speaker from the 'unmapped_speakers' "
                    f"array in {pending_path}."
                ),
                (0, 2),
            ),
        ],
    )

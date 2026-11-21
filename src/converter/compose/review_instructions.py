"""Reusable manual-review guidance for the two compose review phases."""

from __future__ import annotations

from pathlib import Path

from rich.console import RenderableType
from rich.padding import Padding
from rich.syntax import Syntax
from rich.text import Text

from .manifest import CHARACTERS_FILE


def manual_scene_instructions(
    reports_dir: Path,
) -> tuple[str, list[RenderableType]]:
    """Return the manual fallback instructions for scene associations."""
    pending_path = reports_dir / "review_pending.json"
    scene_manifest = reports_dir / "manifest_scene.json"

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
                    "Integrity note: versioned bundles protect category files "
                    "with SHA-256 digests in the manifest index. Direct edits to "
                    f"{scene_manifest} will be rejected; use the interactive "
                    "review so the category, pending state, and refreshed "
                    "index are published together."
                )
            ),
            Padding(
                Text(
                    "Pending scene entries are stored in "
                    f"{pending_path}."
                )
            ),
        ],
    )


def manual_speaker_instructions(
    reports_dir: Path,
) -> tuple[str, list[RenderableType]]:
    """Return the manual fallback instructions for speaker mappings."""
    pending_path = reports_dir / "review_pending.json"
    characters_path = reports_dir / CHARACTERS_FILE
    speaker_json = (
        "{\n"
        '  "amber": {\n'
        '    "folder": "composites/static/char/amber",\n'
        '    "aliases": ["a", "am"]\n'
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
            Padding(Text(f"- Open {characters_path}."), (0, 2)),
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
            Padding(
                Text(
                    "Integrity note: prefer the interactive review because it "
                    "publishes category, character, pending-review, and index "
                    "changes together. Versioned bundles reject direct edits "
                    "to protected category files."
                )
            ),
        ],
    )

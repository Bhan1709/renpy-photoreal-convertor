"""Dialog/scene-boundary scanning for the compose pass.

Responsibility: find say/narrator statements near a given line in a script,
used to build per-location dialog context for scene composites.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..regexp import (
    NARRATOR_RE,
    NOT_SPEAKER,
    SAY_RE,
    SHOW_SCENE_RE,
    SHOW_SCREEN_RE,
)


@dataclass(frozen=True)
class DialogueLine:
    """A nearby say/narrator statement."""

    speaker: str
    text: str
    line: int


def find_nearby_dialog(
    path: Path,
    line_number: int,
    max_dialogs_back: int = 2,
    max_dialogs_forward: int = 5,
    lines: list[str] | None = None,
) -> list[DialogueLine]:
    """Find dialog/narration statements near a given line in a script.

    Collects say/narrator lines going backward and forward from
    ``line_number`` (exclusive), stopping in each direction when:

    * the per-direction dialog limit is reached, or
    * a ``show``/``scene``/``show screen`` statement is encountered, or
    * the start / end of the file is reached.

    Forward dialogs (spoken while the scene is showing) are weighted more
    heavily than backward dialogs (spoken before the scene appears), so the
    default forward limit is larger.

    Returns them ordered by line number (oldest first).

    If ``lines`` is supplied (e.g. from a single-pass script index) the file is
    not re-read from disk (Optimization #1).
    """
    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return []

    def _is_dialog_line(raw: str) -> bool:
        text = raw.strip()
        if not text or text.startswith("#"):
            return False
        if SHOW_SCENE_RE.match(raw) or SHOW_SCREEN_RE.match(raw):
            return False  # boundary: another scene/show statement
        m = SAY_RE.match(raw)
        if m:
            if m.group(2) in NOT_SPEAKER:
                return False
            return not _looks_like_asset_path(m.group(3))
        m = NARRATOR_RE.match(raw)
        if m:
            return not _looks_like_asset_path(m.group(1))
        return False

    def _extract_dialog(raw: str) -> DialogueLine | None:
        m = SAY_RE.match(raw)
        if m:
            speaker = m.group(2)
            if speaker in NOT_SPEAKER:
                return None
            dialog_text = m.group(3)
            if _looks_like_asset_path(dialog_text):
                return None
            return DialogueLine(
                speaker=speaker, text=dialog_text, line=0
            )  # line set by caller
        m = NARRATOR_RE.match(raw)
        if m:
            dialog_text = m.group(1)
            if _looks_like_asset_path(dialog_text):
                return None
            return DialogueLine(speaker="narrator", text=dialog_text, line=0)
        return None

    # --- scan backward ---
    before: list[DialogueLine] = []
    i = line_number - 2  # 0-based index of line just before anchor
    while i >= 0 and len(before) < max_dialogs_back:
        raw = lines[i]
        if _is_dialog_line(raw):
            d = _extract_dialog(raw)
            if d is not None:
                d = DialogueLine(speaker=d.speaker, text=d.text, line=i + 1)
                before.append(d)
        # Stop if this line is a show/scene statement even if not a dialog.
        stripped = raw.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and (SHOW_SCENE_RE.match(raw) or SHOW_SCREEN_RE.match(raw))
        ):
            break
        i -= 1

    # --- scan forward ---
    after: list[DialogueLine] = []
    i = line_number  # 0-based index of line just after anchor
    while i < len(lines) and len(after) < max_dialogs_forward:
        raw = lines[i]
        if _is_dialog_line(raw):
            d = _extract_dialog(raw)
            if d is not None:
                d = DialogueLine(speaker=d.speaker, text=d.text, line=i + 1)
                after.append(d)
        stripped = raw.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and (SHOW_SCENE_RE.match(raw) or SHOW_SCREEN_RE.match(raw))
        ):
            break
        i += 1

    # Oldest first: before (reversed) then after.
    return list(reversed(before)) + after


def _looks_like_asset_path(text: str) -> bool:
    t = text.strip().strip('"').strip("'")
    return (
        "/" in t
        or "\\" in t
        or t.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
    )

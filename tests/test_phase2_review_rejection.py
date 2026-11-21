"""Phase 2 regressions for transactional interactive rejection handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from converter.compose.config import GameConfig
from converter.compose.errors import ManifestError
from converter.compose.review import interactive


def _config(tmp_path: Path) -> GameConfig:
    game_root = tmp_path / "game"
    game_root.mkdir()
    return GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )


def _scene_data(composite_path: str) -> dict[str, dict]:
    return {
        "scene": {
            "category": "scene",
            "composites": [
                {
                    "composite_path": composite_path,
                    "name": "room",
                    "usage_locations": [],
                }
            ],
            "summary": {"composites": 1},
        }
    }


def test_scene_rejection_keeps_the_source_until_the_bundle_commits(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/static/scene/room.png"
    source = config.game_root / "composites" / "static" / "scene" / "room.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"

    finalizer = interactive._reject_scene(
        config,
        composite_path,
        "room",
        config.resolved_paths()["report_dir"],
        rejected_dir,
        _scene_data(composite_path),
    )

    assert source.read_bytes() == b"png"
    assert not (rejected_dir / "pngs" / "room.png").exists()
    assert not list(rejected_dir.glob("*.json")) if rejected_dir.exists() else True

    finalizer()

    assert not source.exists()
    assert (rejected_dir / "pngs" / "room.png").read_bytes() == b"png"
    records = list(rejected_dir.glob("scene_*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["composite_path"] == composite_path


def test_scene_rejection_refuses_to_delete_a_manifest_bundle_file(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/reports/manifest_index.json"
    source = config.game_root / "composites" / "reports" / "manifest_index.json"
    source.parent.mkdir(parents=True)
    source.write_text("index", encoding="utf-8")
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"

    with pytest.raises(ManifestError, match="not a generated image"):
        interactive._reject_scene(
            config,
            composite_path,
            "manifest_index",
            config.resolved_paths()["report_dir"],
            rejected_dir,
            _scene_data(composite_path),
        )

    assert source.read_text(encoding="utf-8") == "index"


def test_scene_rejection_uses_the_explicit_report_scope_for_exclusions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/static/scene/room.png"
    source = config.game_root / "composites" / "static" / "scene" / "room.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"
    explicit_report_scope = source.parent

    with pytest.raises(ManifestError, match="excluded root"):
        interactive._reject_scene(
            config,
            composite_path,
            "room",
            explicit_report_scope,
            rejected_dir,
            _scene_data(composite_path),
        )

    assert source.read_bytes() == b"png"


def test_scene_rejection_refuses_a_symlinked_rejected_png_directory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/static/scene/room.png"
    source = config.game_root / "composites" / "static" / "scene" / "room.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"
    rejected_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (rejected_dir / "pngs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")

    with pytest.raises(ManifestError, match="escapes"):
        interactive._reject_scene(
            config,
            composite_path,
            "room",
            config.resolved_paths()["report_dir"],
            rejected_dir,
            _scene_data(composite_path),
        )

    assert source.read_bytes() == b"png"
    assert not list(outside.iterdir())


def test_failed_scene_bundle_commit_leaves_the_png_and_manifest_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/static/scene/room.png"
    source = config.game_root / "composites" / "static" / "scene" / "room.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"
    pending = {
        "scenes": [
            {
                "composite_path": composite_path,
                "name": "room",
                "definition": {"script": "game/script.rpy", "line": 1},
                "usage_locations": [],
            }
        ]
    }

    monkeypatch.setattr(
        interactive,
        "select_multiple_with_context",
        lambda *_args, **_kwargs: [interactive._REMOVE_SCENE_FROM_PROCESSING],
    )

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated bundle commit failure")

    monkeypatch.setattr(interactive, "_save_review_bundle", fail_save)

    with pytest.raises(OSError, match="bundle commit failure"):
        interactive._process_scenes_missing_chars(
            config,
            pending,
            rejected_dir,
            _scene_data(composite_path),
            config.resolved_paths()["report_dir"],
            config.resolved_paths()["manifest_index_path"],
        )

    assert source.read_bytes() == b"png"
    assert not rejected_dir.exists() or not list(rejected_dir.rglob("*"))


def test_failed_speaker_bundle_commit_does_not_write_a_false_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    composite_path = "game/composites/static/scene/room.png"
    rejected_dir = config.resolved_paths()["output_dir"] / "rejected"
    pending = {
        "unmapped_speakers": [
            {
                "speaker": "../../outside",
                "composites": [composite_path],
            }
        ]
    }
    all_data = {
        "scene": {
            "category": "scene",
            "composites": [
                {
                    "composite_path": composite_path,
                    "name": "room",
                    "usage_locations": [],
                    "unmapped_speakers": ["../../outside"],
                }
            ],
            "summary": {"composites": 1},
        }
    }

    monkeypatch.setattr(
        interactive,
        "select_with_context",
        lambda *_args, **_kwargs: interactive._REMOVE_SPEAKER,
    )

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated bundle commit failure")

    monkeypatch.setattr(interactive, "_save_review_bundle", fail_save)

    with pytest.raises(OSError, match="bundle commit failure"):
        interactive._process_unmapped_speakers(
            config,
            pending,
            rejected_dir,
            {},
            all_data,
            config.resolved_paths()["report_dir"],
            config.resolved_paths()["manifest_index_path"],
        )

    assert not rejected_dir.exists() or not list(rejected_dir.rglob("*"))

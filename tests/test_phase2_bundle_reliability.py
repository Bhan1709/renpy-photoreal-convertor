"""Phase 2 regressions for native isolation and bundle integrity."""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from converter.compose.apply_pass import Applier
from converter.compose.config import GameConfig
from converter.compose.errors import ApplyError, ManifestError, NativeProtocolError
from converter.compose.manifest import ManifestWriter
from converter.compose.models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
)
from converter.compose.native.process import run_native_batch
from converter.compose.native.protocol import validate_result
from converter.compose.native.stage import _copy_render, _native_unresolved_errors
from converter.compose.native.workspace import create_workspace
from converter.compose.review.interactive import (
    _load_all_category_manifests,
    _save_all_category_manifests,
    _save_review_bundle,
)
from converter.restore import restore_from_manifest
from converter.restore.errors import RestoreError
from converter.utility.atomic import (
    AtomicWriteError,
    atomic_write_json_files,
    recover_transaction_journal,
)
from converter.utility.manifest_bundle import (
    ManifestBundlePathError,
    bundle_journal_metadata,
    bundle_journal_path,
    bundle_owner_registry_path,
    manifest_bundle_lock,
    recover_manifest_bundle,
    register_bundle_owner,
)


def _config(tmp_path: Path) -> GameConfig:
    game_root = tmp_path / "game"
    game_root.mkdir(exist_ok=True)
    report_dir = tmp_path / "reports"
    return GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=tmp_path / "output",
        report_dir=report_dir,
        manifest_index_path=report_dir / "manifest_index.json",
    )


def _directory_alias(target: Path, alias: Path) -> None:
    """Create a directory alias, skipping when the platform forbids it."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory alias creation is not available: {exc}")


def _record() -> CompositeRecord:
    return CompositeRecord(
        composite_path="game/composites/static/bg/home__abc123.png",
        name="home",
        kind="image",
        status="static",
        definition=DefinitionLocation(script="game/script.rpy", line=1),
        usage_locations=(),
        ordered_layers=(),
        dimensions=(64, 32),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="bg",
    )


def _manifest() -> CompositeManifest:
    return CompositeManifest(
        composites=(_record(),),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 1,
            "uses_processed": 1,
        },
    )


def _native_result() -> dict[str, Any]:
    return {
        "protocol": "renpy-compose-native-v2",
        "schema_version": 2,
        "status": "ok",
        "batch": {
            "index": 0,
            "state_offset": 0,
            "rendered": 0,
            "total_states": 0,
            "next_offset": None,
            "walk": {"processed": 0, "truncated": False},
        },
        "candidates": [],
        "layered_images": [],
        "image_definitions": {},
        "characters": {},
    }


def _hold_bundle_lock_in_process(
    index_path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    """Hold a bundle lock in a child process for the cross-process test."""
    with manifest_bundle_lock(index_path, timeout=5.0):
        entered.set()
        release.wait(timeout=10.0)


def test_workspace_keeps_native_result_inside_disposable_root(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

    workspace = create_workspace(
        game_root=game_root,
        script_files=["script.rpy"],
        engine_config={},
        result_path=tmp_path / "shared" / "result.json",
    )
    try:
        assert workspace.result_path.parent == workspace.root
        assert workspace.result_path != tmp_path / "shared" / "result.json"
    finally:
        workspace.cleanup()


def test_workspace_cleanup_is_idempotent(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

    workspace = create_workspace(
        game_root=game_root,
        script_files=["script.rpy"],
        engine_config={},
    )
    workspace.cleanup()
    workspace.cleanup()


def test_workspace_cleanup_failure_does_not_mask_the_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")
    workspace = create_workspace(
        game_root=game_root,
        script_files=["script.rpy"],
        engine_config={},
    )

    def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(
        "converter.compose.native.workspace.shutil.rmtree",
        fail_cleanup,
    )

    with (
        pytest.raises(ValueError, match="original failure") as exc_info,
        workspace,
    ):
        raise ValueError("original failure")

    assert "cleanup also failed" in " ".join(exc_info.value.__notes__)


def test_workspace_never_escapes_for_a_dot_dot_result_name(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

    workspace = create_workspace(
        game_root=game_root,
        script_files=["script.rpy"],
        engine_config={},
        result_path=Path(".."),
    )
    try:
        assert workspace.result_path.parent == workspace.root
        assert workspace.result_path.is_relative_to(workspace.root)
    finally:
        workspace.cleanup()


def test_native_render_copy_cannot_escape_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    render_dir = workspace / "renders"
    render_dir.mkdir(parents=True)
    outside = workspace / "outside.png"
    outside.write_bytes(b"outside")
    target = tmp_path / "output" / "render.png"

    copied = _copy_render(
        {"render_file": "../outside.png"},
        target,
        render_dir,
    )

    assert not copied
    assert not target.exists()


def test_native_process_clears_a_stale_result_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0
        pid = 1234

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "converter.compose.native.process.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    layout = SimpleNamespace(command=lambda **kwargs: ["fake-native"])
    result_path = tmp_path / "native-result.json"
    result_path.write_text("stale", encoding="utf-8")

    run_native_batch(
        layout=layout,  # type: ignore[arg-type]
        basedir=tmp_path,
        savedir=tmp_path / "savedir",
        config_path=tmp_path / "native_config.json",
        result_path=result_path,
        command="composite_export",
        timeout=1,
    )

    assert not result_path.exists()


def test_native_protocol_rejects_a_result_for_another_batch() -> None:
    result = _native_result()

    with pytest.raises(NativeProtocolError, match="batch index"):
        validate_result(result, expected_batch_index=1)

    with pytest.raises(NativeProtocolError, match="state_offset"):
        validate_result(result, expected_state_offset=4)


def test_native_diagnostics_keep_candidate_and_usage_context() -> None:
    candidates = [
        {
            "candidate_id": "candidate-1",
            "name": "home",
            "image_name": ["home", "pose"],
            "usages": [{"script": "game/script.rpy", "line": 12}],
            "unresolved_errors": ["condition failed"],
            "error": None,
        },
        {
            "candidate_id": "candidate-2",
            "name": "home",
            "image_name": ["home", "pose"],
            "usages": [{"script": "game/script.rpy", "line": 12}],
            "unresolved_errors": ["condition failed"],
            "error": None,
        },
    ]

    diagnostics = _native_unresolved_errors(candidates)

    assert len(diagnostics) == 2
    assert any("candidate-1" in diagnostic for diagnostic in diagnostics)
    assert any("candidate-2" in diagnostic for diagnostic in diagnostics)
    assert all("game/script.rpy:12" in diagnostic for diagnostic in diagnostics)


def test_native_diagnostics_include_layer_level_condition_errors() -> None:
    candidates = [
        {
            "candidate_id": "candidate-layer-1",
            "name": "home",
            "image_name": ["home", "pose"],
            "usages": [{"script": "game/script.rpy", "line": 12}],
            "unresolved_errors": [],
            "layers": [
                {
                    "sources": ["bgs/home.png"],
                    "attribute": "pose",
                    "group": "master",
                    "unresolved_errors": ["condition failed in layer"],
                }
            ],
            "error": None,
        }
    ]

    diagnostics = _native_unresolved_errors(candidates)

    assert len(diagnostics) == 1
    assert "condition failed in layer" in diagnostics[0]
    assert "candidate-layer-1" in diagnostics[0]
    assert "bgs/home.png" in diagnostics[0]


def test_manifest_index_records_category_digests_and_detects_tampering(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    writer = ManifestWriter(config)
    writer.write(_manifest())

    index = json.loads(
        config.resolved_paths()["manifest_index_path"].read_text(encoding="utf-8")
    )
    assert set(index["category_digests"]) == {
        "bg",
        "char",
        "scene",
        "gui",
        "other",
    }

    category_path = config.resolved_paths()["report_dir"] / "manifest_bg.json"
    category_path.write_text(
        json.dumps({"category": "bg", "composites": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="integrity"):
        ManifestWriter.load_all(config)


def test_versioned_bundle_rejects_removed_digest_metadata(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["category_digests"]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ManifestError, match="category_digests"):
        ManifestWriter.load_all(config)
    with pytest.raises(ManifestError, match="category_digests"):
        Applier(config)._category_manifest_files()
    with pytest.raises(ManifestError, match="category_digests"):
        _load_all_category_manifests(
            paths["report_dir"],
            paths["manifest_index_path"],
        )
    with pytest.raises(RestoreError, match="category_digests"):
        restore_from_manifest(
            config.game_root,
            manifest_index_path=paths["manifest_index_path"],
            report_dir=paths["report_dir"],
        )


def test_bundle_digest_metadata_requires_every_category(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["category_digests"]["bg"]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ManifestError, match="digest"):
        ManifestWriter.load_all(config)


def test_bundle_digest_metadata_rejects_a_missing_category_file(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["categories"].pop("char")
    index_path.write_text(json.dumps(index), encoding="utf-8")
    (paths["report_dir"] / "manifest_char.json").unlink()

    with pytest.raises(ManifestError, match="missing"):
        ManifestWriter.load_all(config)


def test_review_does_not_partially_upgrade_legacy_digest_metadata(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text(
        json.dumps({"category": "bg", "composites": []}) + "\n",
        encoding="utf-8",
    )
    index_path.write_text(
        json.dumps({"categories": {"bg": "manifest_bg.json"}}),
        encoding="utf-8",
    )

    _save_all_category_manifests(
        report_dir,
        {"bg": {"category": "bg", "composites": []}},
        index_path,
    )

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert "category_digests" not in index


def test_every_bundle_consumer_rejects_a_tampered_category(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    category_path = paths["report_dir"] / "manifest_bg.json"
    category_path.write_text(
        json.dumps({"category": "bg", "composites": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="integrity"):
        Applier(config)._category_manifest_files()
    with pytest.raises(ManifestError, match="integrity"):
        _load_all_category_manifests(
            paths["report_dir"], paths["manifest_index_path"]
        )
    with pytest.raises(RestoreError, match="integrity"):
        restore_from_manifest(
            config.game_root,
            manifest_index_path=paths["manifest_index_path"],
            report_dir=paths["report_dir"],
        )


def test_review_refreshes_digests_after_mutating_a_category(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    category_path = paths["report_dir"] / "manifest_bg.json"
    category = json.loads(category_path.read_text(encoding="utf-8"))
    category["summary"] = {"composites": 1, "reviewed": True}

    _save_all_category_manifests(
        paths["report_dir"],
        {"bg": category},
        paths["manifest_index_path"],
    )

    index = json.loads(paths["manifest_index_path"].read_text(encoding="utf-8"))
    assert index["category_digests"]["bg"]
    ManifestWriter.load_all(config)


def test_review_rejects_a_category_that_references_the_manifest_index(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    report_dir = paths["report_dir"]
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["categories"]["bg"] = index_path.name
    index_path.write_text(json.dumps(index), encoding="utf-8")
    original_category = (report_dir / "manifest_bg.json").read_bytes()

    with pytest.raises(ManifestError, match="references the manifest index"):
        _save_review_bundle(
            report_dir,
            {"bg": {"category": "bg", "composites": []}},
            pending={"scenes": [], "unmapped_speakers": []},
            characters={},
            manifest_index_path=index_path,
        )

    assert (report_dir / "manifest_bg.json").read_bytes() == original_category


def test_review_rejects_a_category_that_references_the_owner_registry(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    report_dir = paths["report_dir"]
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["categories"]["bg"] = ".manifest-bundle.owners.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ManifestError, match="collides with a bundle file"):
        _save_review_bundle(
            report_dir,
            {"bg": {"category": "bg", "composites": []}},
            pending={"scenes": [], "unmapped_speakers": []},
            characters={},
            manifest_index_path=index_path,
        )

    assert (report_dir / ".manifest-bundle.owners.json").is_file()


def test_review_rejects_duplicate_category_reference_aliases(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    report_dir = paths["report_dir"]
    index_path = paths["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["categories"]["char"] = index["categories"]["bg"]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    original_category = (report_dir / "manifest_bg.json").read_bytes()

    with pytest.raises(ManifestError, match="reference the same file"):
        _save_review_bundle(
            report_dir,
            {
                "bg": {"category": "bg", "composites": []},
                "char": {"category": "char", "composites": []},
            },
            pending={"scenes": [], "unmapped_speakers": []},
            characters={},
            manifest_index_path=index_path,
        )

    assert (report_dir / "manifest_bg.json").read_bytes() == original_category


def test_review_action_rolls_back_categories_characters_and_pending_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    ManifestWriter(config).write(_manifest())
    paths = config.resolved_paths()
    report_dir = paths["report_dir"]
    index_path = paths["manifest_index_path"]
    category_path = report_dir / "manifest_bg.json"
    characters_path = report_dir / "characters.json"
    pending_path = report_dir / "review_pending.json"

    all_data = _load_all_category_manifests(report_dir, index_path)
    all_data["bg"]["summary"] = {"composites": 1, "reviewed": True}
    characters = {"Alice": {"folder": "alice", "aliases": ["a"]}}
    pending = {"pending_reviews": 0, "scenes": [], "unmapped_speakers": []}

    original_bytes = {
        path: path.read_bytes()
        for path in (category_path, characters_path, pending_path, index_path)
    }
    original_replace = os.replace

    def fail_index_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == index_path:
            raise OSError("simulated review commit failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_index_replace)

    with pytest.raises(OSError, match="review commit failure"):
        _save_review_bundle(
            report_dir,
            all_data,
            pending=pending,
            characters=characters,
            manifest_index_path=index_path,
        )

    for path, expected in original_bytes.items():
        assert path.read_bytes() == expected
    assert not list(report_dir.glob("*.tmp"))


def test_bundle_recovery_accepts_an_index_referenced_custom_category(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    category_path = report_dir / "nested" / "custom_bg.json"
    category_path.parent.mkdir(parents=True)
    category_path.write_text("new", encoding="utf-8")
    index_path.write_text(
        json.dumps({"categories": {"bg": "nested/custom_bg.json"}}),
        encoding="utf-8",
    )
    journal_path = report_dir / ".manifest_index.json.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(index_path, report_dir)
    assert category_path.read_text(encoding="utf-8") == "old"
    assert not journal_path.exists()


def test_bundle_recovery_accepts_any_index_referenced_category(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    category_path = report_dir / "nested" / "custom.json"
    category_path.parent.mkdir(parents=True)
    category_path.write_text("new", encoding="utf-8")
    index_path.write_text(
        json.dumps({"categories": {"custom": "nested/custom.json"}}),
        encoding="utf-8",
    )
    journal_path = report_dir / ".manifest_index.json.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(index_path, report_dir)
    assert category_path.read_text(encoding="utf-8") == "old"
    assert not journal_path.exists()


def test_bundle_recovery_includes_sibling_journals_in_the_report_directory(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_index = report_dir / "current-index.json"
    foreign_index = report_dir / "foreign-index.json"
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    foreign_index.write_text("new", encoding="utf-8")
    journal_path = report_dir / ".foreign-index.json.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(foreign_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(current_index, report_dir)
    assert category_path.read_text(encoding="utf-8") == "old"
    assert foreign_index.read_text(encoding="utf-8") == "old"
    assert not journal_path.exists()


def test_bundle_recovery_accepts_a_sibling_index_referenced_category(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_index = report_dir / "current-index.json"
    foreign_index = report_dir / "foreign-index.json"
    category_path = report_dir / "nested" / "custom.json"
    category_path.parent.mkdir(parents=True)
    category_path.write_text("new", encoding="utf-8")
    foreign_index.write_text(
        json.dumps({"categories": {"custom": "nested/custom.json"}}),
        encoding="utf-8",
    )
    journal_path = report_dir / ".foreign-index.json.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(foreign_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(current_index, report_dir)
    assert category_path.read_text(encoding="utf-8") == "old"
    assert foreign_index.read_text(encoding="utf-8") == "old"
    assert not journal_path.exists()


def test_bundle_recovery_rejects_a_sibling_journal_with_an_unrelated_target(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_index = report_dir / "current-index.json"
    foreign_index = report_dir / "foreign-index.json"
    unrelated_target = report_dir / "unrelated.txt"
    unrelated_target.write_text("new", encoding="utf-8")
    foreign_index.write_text("{}", encoding="utf-8")
    journal_path = report_dir / ".foreign-index.json.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(unrelated_target.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(current_index, report_dir)

    assert unrelated_target.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


@pytest.mark.parametrize(
    ("support_name", "legacy_journal_name"),
    [
        (
            ".manifest-bundle.owners.json",
            "..manifest-bundle.owners.json.journal",
        ),
        (
            ".manifest-bundle.journal",
            "..manifest-bundle.journal.journal",
        ),
    ],
)
def test_legacy_journal_cannot_treat_a_support_file_as_an_index(
    tmp_path: Path,
    support_name: str,
    legacy_journal_name: str,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_index = report_dir / "current-index.json"
    support_path = report_dir / support_name
    original = b"original"
    if support_name == ".manifest-bundle.owners.json":
        support_path.write_bytes(original)
    legacy_path = report_dir / legacy_journal_name
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(support_path.resolve()),
                        "previous": base64.b64encode(b"tampered").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert not recover_manifest_bundle(current_index, report_dir)
    if support_path.exists():
        assert support_path.read_bytes() == original
    else:
        assert not support_path.exists()
    assert legacy_path.is_file()


@pytest.mark.parametrize(
    "legacy_journal_name",
    [
        "...journal",
        "....journal",
    ],
)
def test_legacy_journal_with_an_invalid_index_name_is_ignored(
    tmp_path: Path,
    legacy_journal_name: str,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_index = report_dir / "current-index.json"
    unrelated_target = report_dir / "unrelated.txt"
    unrelated_target.write_text("new", encoding="utf-8")
    legacy_path = report_dir / legacy_journal_name
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(unrelated_target.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert not recover_manifest_bundle(current_index, report_dir)
    assert unrelated_target.read_text(encoding="utf-8") == "new"
    assert legacy_path.is_file()


def test_legacy_sibling_journal_cannot_restore_a_peer_index_through_an_alias(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_dir = report_dir / "current"
    current_dir.mkdir()
    current_index = current_dir / "current-index.json"
    current_index.write_text("current", encoding="utf-8")
    alias_dir = report_dir / "current-alias"
    _directory_alias(current_dir, alias_dir)
    current_alias = alias_dir / "current-index.json"
    foreign_index = report_dir / "foreign-index.json"
    foreign_index.write_text(
        json.dumps({"categories": {"peer": "current/current-index.json"}}),
        encoding="utf-8",
    )
    legacy_path = report_dir / ".foreign-index.json.journal"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(current_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(current_alias, report_dir)

    assert current_index.read_text(encoding="utf-8") == "current"
    assert current_alias.read_text(encoding="utf-8") == "current"
    assert legacy_path.is_file()


def test_shared_journal_cannot_restore_a_peer_index_through_an_alias(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    current_dir = report_dir / "current"
    current_dir.mkdir()
    current_index = current_dir / "current-index.json"
    current_index.write_text("current", encoding="utf-8")
    alias_dir = report_dir / "current-alias"
    _directory_alias(current_dir, alias_dir)
    current_alias = alias_dir / "current-index.json"
    owner_index = report_dir / "owner-index.json"
    owner_index.write_text(
        json.dumps({"categories": {"peer": "current/current-index.json"}}),
        encoding="utf-8",
    )
    owner_bytes = owner_index.read_bytes()
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(current_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(current_alias, report_dir)

    assert current_index.read_text(encoding="utf-8") == "current"
    assert current_alias.read_text(encoding="utf-8") == "current"
    assert owner_index.read_bytes() == owner_bytes
    assert journal_path.is_file()


def test_legacy_recovery_accepts_a_canonical_target_through_a_junction_alias(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    canonical_dir = report_dir / "canonical"
    canonical_dir.mkdir()
    canonical_index = canonical_dir / "index.json"
    canonical_index.write_text("new", encoding="utf-8")
    alias_dir = report_dir / "alias"
    _directory_alias(canonical_dir, alias_dir)
    alias_index = alias_dir / "index.json"
    legacy_path = alias_dir / ".index.json.journal"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(canonical_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(alias_index, report_dir)
    assert canonical_index.read_text(encoding="utf-8") == "old"
    assert alias_index.read_text(encoding="utf-8") == "old"
    assert not legacy_path.exists()


@pytest.mark.parametrize(
    "support_name",
    [
        ".manifest-bundle.owners.json",
        ".manifest-bundle.journal",
    ],
)
def test_owner_registration_rejects_a_support_file_as_an_owner(
    tmp_path: Path,
    support_name: str,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    support_path = report_dir / support_name

    with pytest.raises(ManifestBundlePathError, match="collides"):
        register_bundle_owner(support_path, report_dir)

    assert not support_path.exists()


def test_apply_rejects_a_support_index_before_live_script_mutation(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    script_path = game_root / "script.rpy"
    script_path.write_text("original\n", encoding="utf-8")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    registry_path = report_dir / ".manifest-bundle.owners.json"
    registry_bytes = b'{"version": 1, "index_paths": []}\n'
    registry_path.write_bytes(registry_bytes)
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=tmp_path / "output",
        report_dir=report_dir,
        manifest_index_path=registry_path,
    )

    with pytest.raises(ApplyError, match="collides"):
        Applier(config).apply()

    assert script_path.read_text(encoding="utf-8") == "original\n"
    assert registry_path.read_bytes() == registry_bytes


def test_review_rejects_a_support_index_before_bundle_mutation(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    registry_path = report_dir / ".manifest-bundle.owners.json"
    registry_bytes = b'{"version": 1, "index_paths": []}\n'
    registry_path.write_bytes(registry_bytes)

    with pytest.raises(ManifestError, match="collides"):
        _save_review_bundle(
            report_dir,
            {},
            pending={"scenes": [], "unmapped_speakers": []},
            characters={},
            manifest_index_path=registry_path,
        )

    assert registry_path.read_bytes() == registry_bytes


def test_restore_rejects_a_support_index_before_live_script_mutation(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    script_path = game_root / "script.rpy"
    script_path.write_text("original\n", encoding="utf-8")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    registry_path = report_dir / ".manifest-bundle.owners.json"
    registry_bytes = b'{"version": 1, "index_paths": []}\n'
    registry_path.write_bytes(registry_bytes)

    with pytest.raises(RestoreError, match="collides"):
        restore_from_manifest(
            game_root,
            manifest_index_path=registry_path,
            report_dir=report_dir,
        )

    assert script_path.read_text(encoding="utf-8") == "original\n"
    assert registry_path.read_bytes() == registry_bytes


def test_bundle_journal_is_shared_by_indexes_in_one_report_directory(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    inside_index = report_dir / "inside-index.json"
    outside_index = tmp_path / "indexes" / "outside-index.json"

    assert bundle_journal_path(inside_index, report_dir) == bundle_journal_path(
        outside_index,
        report_dir,
    )
    assert bundle_journal_path(inside_index, report_dir) == (
        report_dir / ".manifest-bundle.journal"
    )


def test_report_peer_recovers_a_journal_owned_by_an_outside_index(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    owner_index = tmp_path / "indexes" / "owner-index.json"
    peer_index = report_dir / "peer-index.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(peer_index, report_dir)
    assert category_path.read_text(encoding="utf-8") == "old"
    assert owner_index.read_text(encoding="utf-8") == "old"
    assert not journal_path.exists()


def test_report_peer_rejects_an_unregistered_journal_owner(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    peer_index = report_dir / "peer-index.json"
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="not registered"):
        recover_manifest_bundle(peer_index, report_dir)

    assert category_path.read_text(encoding="utf-8") == "new"
    assert owner_index.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_report_peer_does_not_authorize_another_index_as_a_recovery_target(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    peer_index = report_dir / "peer-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text(
        json.dumps({"categories": {"peer": "../reports/peer-index.json"}}),
        encoding="utf-8",
    )
    peer_index.write_text("peer", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(peer_index.resolve()),
                        "previous": base64.b64encode(b"old-peer").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(peer_index, report_dir)

    assert category_path.read_text(encoding="utf-8") == "new"
    assert peer_index.read_text(encoding="utf-8") == "peer"
    assert journal_path.is_file()


def test_report_peer_recovers_a_real_interrupted_owner_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text('{"old": true}\n', encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text(
        json.dumps({"categories": {"bg": "../reports/manifest_bg.json"}}),
        encoding="utf-8",
    )
    peer_index = report_dir / "peer-index.json"
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    original_replace = os.replace
    atomic_module = __import__(
        "converter.utility.atomic", fromlist=["_restore_previous"]
    )
    original_restore = atomic_module._restore_previous

    def fail_owner_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == owner_index:
            raise OSError("simulated interrupted owner commit")
        original_replace(source, destination)

    def fail_rollback(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated process termination during rollback")

    monkeypatch.setattr(os, "replace", fail_owner_replace)
    monkeypatch.setattr(atomic_module, "_restore_previous", fail_rollback)

    with pytest.raises(AtomicWriteError, match="rollback"):
        atomic_write_json_files(
            {
                category_path: {"new": True},
                owner_index: {"committed": True},
            },
            journal_path=journal_path,
            allowed_paths=[category_path, owner_index],
            journal_metadata=bundle_journal_metadata(owner_index, report_dir),
        )

    monkeypatch.setattr(os, "replace", original_replace)
    monkeypatch.setattr(atomic_module, "_restore_previous", original_restore)
    assert journal_path.is_file()

    assert recover_manifest_bundle(peer_index, report_dir)
    assert json.loads(category_path.read_text(encoding="utf-8")) == {"old": True}
    assert json.loads(owner_index.read_text(encoding="utf-8")) == {
        "categories": {"bg": "../reports/manifest_bg.json"}
    }
    assert not journal_path.exists()


def test_shared_journal_cannot_restore_the_owner_registry(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text(
        json.dumps(
            {"categories": {"registry": "../reports/.manifest-bundle.owners.json"}}
        ),
        encoding="utf-8",
    )
    register_bundle_owner(owner_index, report_dir)
    registry_path = bundle_owner_registry_path(report_dir)
    registry_bytes = registry_path.read_bytes()
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(registry_path.resolve()),
                        "previous": base64.b64encode(b"tampered").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(owner_index, report_dir)

    assert registry_path.read_bytes() == registry_bytes
    assert category_path.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_malformed_owner_registry_fails_closed_without_touching_targets(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    registry_path = bundle_owner_registry_path(report_dir)
    registry_path.mkdir()
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="owner registry"):
        recover_manifest_bundle(owner_index, report_dir)

    assert category_path.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_owner_registry_cannot_register_a_support_file_as_an_owner(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = report_dir / "owner-index.json"
    owner_index.write_text("new", encoding="utf-8")
    registry_path = bundle_owner_registry_path(report_dir)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "index_paths": [str(registry_path.resolve())],
            }
        ),
        encoding="utf-8",
    )
    registry_bytes = registry_path.read_bytes()
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="owner registry"):
        recover_manifest_bundle(owner_index, report_dir)

    assert registry_path.read_bytes() == registry_bytes
    assert category_path.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_shared_journal_with_a_boolean_version_fails_closed(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    journal_path.write_text(
        json.dumps(
            {
                "version": True,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="metadata"):
        recover_manifest_bundle(owner_index, report_dir)

    assert category_path.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_shared_journal_without_owner_metadata_fails_closed(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="metadata"):
        recover_manifest_bundle(owner_index, report_dir)

    assert category_path.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_shared_journal_supersedes_an_older_legacy_journal(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    category_path = report_dir / "manifest_bg.json"
    owner_index = tmp_path / "indexes" / "owner-index.json"
    legacy_index = report_dir / "legacy-index.json"
    category_path.write_text("new", encoding="utf-8")
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    legacy_index.write_text("new", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    shared_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    shared_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"pre-shared").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )
    legacy_path = report_dir / ".legacy-index.json.journal"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(category_path.resolve()),
                        "previous": base64.b64encode(b"pre-legacy").decode("ascii"),
                    },
                    {
                        "path": str(legacy_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recover_manifest_bundle(owner_index, report_dir)
    assert category_path.read_text(encoding="utf-8") == "pre-shared"
    assert not shared_path.exists()
    assert not legacy_path.exists()


def test_report_journal_rejects_an_unrelated_target(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    owner_index = tmp_path / "indexes" / "owner-index.json"
    owner_index.parent.mkdir(parents=True)
    owner_index.write_text("new", encoding="utf-8")
    unrelated = report_dir / "unrelated.txt"
    unrelated.write_text("new", encoding="utf-8")
    register_bundle_owner(owner_index, report_dir)
    journal_path = bundle_journal_path(owner_index, report_dir)
    owner_new_digest = hashlib.sha256(b"new").hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "metadata": bundle_journal_metadata(owner_index, report_dir),
                "files": [
                    {
                        "path": str(unrelated.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    },
                    {
                        "path": str(owner_index.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                        "new_digest": owner_new_digest,
                    },
                ],
                "commit_path": str(owner_index.resolve()),
                "commit_digest": owner_new_digest,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_manifest_bundle(owner_index, report_dir)

    assert unrelated.read_text(encoding="utf-8") == "new"
    assert journal_path.is_file()


def test_atomic_rollback_restores_a_target_replaced_before_an_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text('{"old": true}\n', encoding="utf-8")
    second_path.write_text('{"old": true}\n', encoding="utf-8")
    journal_path = tmp_path / "transaction.journal"
    original_replace = os.replace
    interrupted = False

    def interrupt_after_replace(
        source: str | Path,
        destination: str | Path,
    ) -> None:
        nonlocal interrupted
        original_replace(source, destination)
        if Path(destination) == second_path and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt_after_replace)

    with pytest.raises(KeyboardInterrupt):
        atomic_write_json_files(
            {
                first_path: {"new": True},
                second_path: {"new": True},
            },
            journal_path=journal_path,
        )

    assert first_path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert second_path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert not journal_path.exists()


def test_atomic_journal_scope_rejects_an_unrelated_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("new", encoding="utf-8")
    journal_path = tmp_path / "transaction.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(target.resolve()),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        atomic_write_json_files(
            {target: {"new": True}},
            journal_path=journal_path,
            allowed_paths=[tmp_path / "other.json"],
        )
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_transaction_rejects_a_target_outside_its_allowlist(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside.json"
    journal_path = tmp_path / ".bundle.journal"

    with pytest.raises(ValueError, match="outside the allowed paths"):
        atomic_write_json_files(
            {target: {"new": True}},
            journal_path=journal_path,
            allowed_paths=[tmp_path / "inside.json"],
        )

    assert not target.exists()
    assert not journal_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_transaction_rejects_duplicate_target_aliases(
    tmp_path: Path,
) -> None:
    alias = tmp_path / "nested" / ".." / "target.json"
    target = tmp_path / "target.json"

    with pytest.raises(ValueError, match="duplicate target"):
        atomic_write_json_files(
            {
                alias: {"first": True},
                target: {"second": True},
            }
        )

    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_bundle_lock_serializes_independent_threads(tmp_path: Path) -> None:
    index_path = tmp_path / "manifest_index.json"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with manifest_bundle_lock(index_path, timeout=1.0):
            entered.set()
            release.wait(timeout=2.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=2.0)

    started = time.monotonic()
    with pytest.raises(TimeoutError), manifest_bundle_lock(
        index_path, timeout=0.05
    ):
        pass
    assert time.monotonic() - started >= 0.04

    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_bundle_lock_is_shared_by_indexes_in_one_report_directory(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    first_index = report_dir / "first-index.json"
    second_index = report_dir / "second-index.json"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with manifest_bundle_lock(
            first_index,
            report_dir=report_dir,
            timeout=1.0,
        ):
            entered.set()
            release.wait(timeout=2.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=2.0)

    with pytest.raises(TimeoutError), manifest_bundle_lock(
        second_index,
        report_dir=report_dir,
        timeout=0.05,
    ):
        pass

    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_bundle_lock_is_reentrant_within_one_thread(tmp_path: Path) -> None:
    index_path = tmp_path / "manifest_index.json"
    with manifest_bundle_lock(index_path, timeout=0.1), manifest_bundle_lock(
        index_path, timeout=0.1
    ):
        pass


def test_bundle_lock_serializes_independent_processes(tmp_path: Path) -> None:
    index_path = tmp_path / "manifest_index.json"
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_bundle_lock_in_process,
        args=(str(index_path), entered, release),
    )
    process.start()
    try:
        assert entered.wait(timeout=5.0)
        with pytest.raises(TimeoutError), manifest_bundle_lock(
            index_path, timeout=0.05
        ):
            pass
    finally:
        release.set()
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
    assert process.exitcode == 0


def test_bundle_recovery_cleans_stale_staged_files_without_a_journal(
    tmp_path: Path,
) -> None:
    target = tmp_path / "manifest_bg.json"
    stale_temporary = tmp_path / ".manifest_bg.json.crashed.tmp"
    stale_temporary.write_text("partial", encoding="utf-8")

    assert not recover_transaction_journal(
        tmp_path / ".manifest_index.json.journal",
        allowed_paths=[target],
    )
    assert not stale_temporary.exists()


def test_recovery_keeps_a_bundle_that_reached_its_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "manifest_index.json"
    journal_path = tmp_path / ".manifest_index.json.journal"
    original_unlink = Path.unlink

    def fail_journal_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == journal_path:
            raise OSError("simulated journal cleanup failure")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    with pytest.raises(AtomicWriteError, match="journal"):
        atomic_write_json_files(
            {target: {"committed": True}},
            journal_path=journal_path,
        )

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert recover_transaction_journal(journal_path)
    assert json.loads(target.read_text(encoding="utf-8")) == {"committed": True}
    assert not journal_path.exists()


def test_journal_recovers_an_interrupted_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    journal_path = tmp_path / "transaction.journal"
    first_path.write_text('{"old": true}\n', encoding="utf-8")

    original_replace = os.replace
    original_restore = __import__(
        "converter.utility.atomic", fromlist=["_restore_previous"]
    )._restore_previous

    def fail_second_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == second_path:
            raise OSError("simulated process failure")
        original_replace(source, destination)

    def fail_restore(target: Path, previous: bytes | None) -> None:
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(os, "replace", fail_second_replace)
    monkeypatch.setattr(
        "converter.utility.atomic._restore_previous",
        fail_restore,
    )

    with pytest.raises(AtomicWriteError, match="rollback"):
        atomic_write_json_files(
            {
                first_path: {"new": True},
                second_path: {"new": True},
            },
            journal_path=journal_path,
        )

    assert journal_path.is_file()
    monkeypatch.setattr(
        "converter.utility.atomic._restore_previous",
        original_restore,
    )
    assert recover_transaction_journal(journal_path)
    assert first_path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert not second_path.exists()
    assert not journal_path.exists()


def test_journal_recovery_rejects_unexpected_targets(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("new", encoding="utf-8")
    journal_path = tmp_path / "transaction.journal"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": [
                    {
                        "path": str(target),
                        "previous": base64.b64encode(b"old").decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AtomicWriteError, match="unexpected target"):
        recover_transaction_journal(journal_path, allowed_paths=[tmp_path / "other"])
    assert target.read_text(encoding="utf-8") == "new"

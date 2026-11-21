"""Phase 2 tests for atomic, path-explicit compose manifest transactions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from converter.compose import cli as compose_cli
from converter.compose.apply_pass import Applier
from converter.compose.config import GameConfig
from converter.compose.errors import ManifestError
from converter.compose.manifest import KNOWN_FAMILIES, ManifestWriter
from converter.compose.models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
)
from converter.compose.native.stage import _previous_dynamic_states
from converter.compose.review.interactive import _load_all_category_manifests
from converter.restore import restore as restore_module
from converter.utility import atomic_write_json_files


def _config(
    tmp_path: Path,
    *,
    report_dir: Path | None = None,
    manifest_index_path: Path | None = None,
) -> GameConfig:
    game_root = tmp_path / "game"
    game_root.mkdir(exist_ok=True)
    return GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=tmp_path / "output",
        report_dir=report_dir,
        manifest_index_path=manifest_index_path,
    )


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
        unresolved=("condition evaluation failed",),
        summary={
            "scripts_scanned": 1,
            "composites": 1,
            "uses_processed": 1,
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_config_resolves_explicit_manifest_paths(tmp_path: Path) -> None:
    report_dir = tmp_path / "custom-reports"
    index_path = report_dir / "index.json"

    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )

    paths = config.resolved_paths()
    assert paths["report_dir"] == report_dir
    assert paths["manifest_index_path"] == index_path


def test_previous_dynamic_states_are_read_from_the_explicit_index(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "custom-index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    _write_json(index_path, {"dynamic_states": [{"name": "home"}]})

    assert _previous_dynamic_states(config) == [{"name": "home"}]


def test_manifest_writer_derives_index_from_explicit_report_dir(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "custom-reports"
    config = _config(tmp_path)

    writer = ManifestWriter(config, report_dir=report_dir)
    writer.write(_manifest())

    assert writer.manifest_index_path == report_dir / "manifest_index.json"
    assert (report_dir / "manifest_index.json").is_file()
    assert (report_dir / "manifest_bg.json").is_file()


def test_manifest_writer_rejects_index_path_that_collides_with_bundle_file(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_bg.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )

    with pytest.raises(ManifestError, match="collides"):
        ManifestWriter(config).write(_manifest())

    assert not index_path.exists()


def test_manifest_write_is_one_atomic_transaction_with_relative_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )

    replaced: list[Path] = []
    original_replace = os.replace

    def tracking_replace(source: str | Path, destination: str | Path) -> None:
        replaced.append(Path(destination))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", tracking_replace)

    ManifestWriter(config).write(_manifest())

    expected = {
        report_dir / f"manifest_{category}.json" for category in KNOWN_FAMILIES
    }
    expected.update(
        {
            index_path,
            report_dir / "characters.json",
            report_dir / "review_pending.json",
        }
    )
    assert set(replaced) == expected
    assert replaced[-1] == index_path
    assert not list(report_dir.glob("*.tmp"))

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["categories"] == {
        category: f"manifest_{category}.json" for category in KNOWN_FAMILIES
    }
    assert all(not Path(value).is_absolute() for value in index["categories"].values())
    assert index["unresolved"] == ["condition evaluation failed"]


def test_manifest_bundle_preserves_native_identity_and_cfg_state(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    native_record = _record().replace(
        content_hash="a" * 64,
        condition_state={"seen_home": True},
    )
    manifest = CompositeManifest(
        composites=(native_record,),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=("condition evaluation failed",),
        summary={
            "scripts_scanned": 1,
            "composites": 1,
            "uses_processed": 1,
            "cfg_truncated": True,
        },
        cfg_processed=42,
        cfg_truncated=True,
    )

    ManifestWriter(config).write(manifest)

    category = json.loads(
        (report_dir / "manifest_bg.json").read_text(encoding="utf-8")
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert category["composites"][0]["content_hash"] == "a" * 64
    assert category["composites"][0]["condition_state"] == {"seen_home": True}
    assert index["cfg_processed"] == 42
    assert index["cfg_truncated"] is True


def test_atomic_bundle_stages_all_payloads_before_replacing_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    replaced: list[Path] = []
    original_replace = os.replace

    def tracking_replace(source: str | Path, destination: str | Path) -> None:
        replaced.append(Path(destination))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", tracking_replace)

    with pytest.raises(TypeError):
        atomic_write_json_files(
            {
                first_path: {"valid": True},
                second_path: {"invalid": {1, 2}},
            }
        )

    assert replaced == []
    assert not first_path.exists()
    assert not second_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_bundle_reports_an_incomplete_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from converter.utility import AtomicWriteError

    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text('{"old": true}\n', encoding="utf-8")
    original_replace = os.replace
    replace_calls = 0

    def failing_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated commit failure")
        if replace_calls > 2:
            raise OSError("simulated rollback failure")
        original_replace(source, destination)

    def failing_write_bytes(self: Path, data: bytes) -> int:
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)

    with pytest.raises(AtomicWriteError, match="rollback"):
        atomic_write_json_files(
            {
                first_path: {"first": True},
                second_path: {"second": True},
            }
        )


def test_failed_atomic_replace_leaves_existing_manifest_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    report_dir.mkdir()
    old_index = '{"categories": {"bg": "old.json"}}\n'
    old_category = '{"category": "bg", "composites": []}\n'
    index_path.write_text(old_index, encoding="utf-8")
    (report_dir / "manifest_bg.json").write_text(old_category, encoding="utf-8")

    original_replace = os.replace

    def failing_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == index_path:
            raise OSError("simulated replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        ManifestWriter(config).write(_manifest())

    assert index_path.read_text(encoding="utf-8") == old_index
    assert (report_dir / "manifest_bg.json").read_text(encoding="utf-8") == old_category
    assert not list(report_dir.glob("*.tmp"))


def test_category_references_are_relative_to_the_index_directory(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = tmp_path / "indexes" / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )

    ManifestWriter(config).write(_manifest())

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["categories"]["bg"] == "../reports/manifest_bg.json"
    loaded = ManifestWriter.load_all(config)
    assert [record.name for record in loaded.composites] == ["home"]


def test_manifest_loader_follows_relative_category_references(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    category_path = report_dir / "nested" / "custom_bg.json"
    _write_json(
        category_path,
        {"category": "bg", "composites": [_record().to_dict()]},
    )
    _write_json(
        index_path,
        {
            "categories": {"bg": "nested/custom_bg.json"},
            "summary": {},
            "dynamic_states": [],
            "image_declarations": [],
            "dynamic_declarations": [],
            "unresolved": [],
        },
    )

    loaded = ManifestWriter.load_all(config)

    assert [record.name for record in loaded.composites] == ["home"]


def test_review_consumer_uses_category_references_from_the_index(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    category_path = report_dir / "nested" / "custom_bg.json"
    _write_json(
        category_path,
        {"category": "bg", "composites": [_record().to_dict()]},
    )
    _write_json(index_path, {"categories": {"bg": "nested/custom_bg.json"}})

    categories = _load_all_category_manifests(report_dir, index_path)

    assert categories["bg"]["composites"][0]["name"] == "home"


def test_apply_consumer_uses_category_references_from_the_index(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    category_path = report_dir / "nested" / "custom_bg.json"
    _write_json(category_path, {"category": "bg", "composites": []})
    _write_json(index_path, {"categories": {"bg": "nested/custom_bg.json"}})

    assert Applier(config)._category_manifest_files() == [category_path]


def test_compose_cli_does_not_rewrite_the_native_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_dir = tmp_path / "reports"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=report_dir / "manifest_index.json",
    )
    manifest = _manifest()
    calls: list[CompositeManifest] = []
    original_write = ManifestWriter.write

    def tracking_write(
        writer: ManifestWriter,
        value: CompositeManifest,
        *args: Any,
        **kwargs: Any,
    ) -> CompositeManifest:
        calls.append(value)
        return original_write(writer, value, *args, **kwargs)

    def fake_compose(
        composer: Any,
        dry_run: bool = False,
        clean_output: bool = False,
    ) -> CompositeManifest:
        ManifestWriter(config).write(manifest)
        return manifest

    monkeypatch.setattr(ManifestWriter, "write", tracking_write)
    monkeypatch.setattr(compose_cli.Composer, "compose", fake_compose)

    compose_cli.compose_game(config)

    assert calls == [manifest]


def test_consumers_use_report_dir_for_unreferenced_category_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    report_dir = tmp_path / "reports"
    index_path = tmp_path / "indexes" / "manifest_index.json"
    category_path = report_dir / "manifest_bg.json"
    category_record = _record().to_dict()
    category_record["usage_locations"] = [
        {"script": "game/script.rpy", "line": 12, "statement": "show home"}
    ]
    _write_json(
        category_path,
        {
            "category": "bg",
            "composites": [category_record],
        },
    )
    _write_json(index_path, {"categories": {}})
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=tmp_path / "output",
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    captured: dict[str, Any] = {}

    def fake_restore_rpy_files(
        root: Path,
        *,
        archive_path: Path | None = None,
        only_files: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, str]:
        captured["only_files"] = only_files
        return {}

    monkeypatch.setattr(restore_module, "restore_rpy_files", fake_restore_rpy_files)

    loaded = ManifestWriter.load_all(config)
    assert [record.name for record in loaded.composites] == ["home"]
    assert Applier(config)._category_manifest_files() == [category_path]
    assert _load_all_category_manifests(report_dir, index_path)["bg"]["composites"]

    restore_module.restore_from_manifest(
        game_root,
        manifest_index_path=index_path,
        report_dir=report_dir,
    )
    assert captured == {"only_files": ["script.rpy"]}


def test_manifest_index_rejects_non_object_categories(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    config = _config(
        tmp_path,
        report_dir=report_dir,
        manifest_index_path=index_path,
    )
    _write_json(index_path, {"categories": []})

    with pytest.raises(ManifestError, match="'categories' must be an object"):
        ManifestWriter.load_all(config)


def test_restore_keeps_legacy_monolithic_manifest_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    legacy_manifest = tmp_path / "composites" / "reports" / "manifest.json"
    _write_json(
        legacy_manifest,
        {
            "composites": [
                {
                    "usage_locations": [
                        {"script": "game/legacy.rpy", "line": 3},
                    ]
                }
            ]
        },
    )
    captured: dict[str, Any] = {}

    def fake_restore_rpy_files(
        root: Path,
        *,
        archive_path: Path | None = None,
        only_files: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, str]:
        captured["only_files"] = only_files
        return {}

    monkeypatch.setattr(restore_module, "restore_rpy_files", fake_restore_rpy_files)

    restore_module.restore_from_manifest(game_root)

    assert captured == {"only_files": ["legacy.rpy"]}


def test_restore_reads_split_manifest_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    report_dir = tmp_path / "reports"
    index_path = report_dir / "manifest_index.json"
    category_path = report_dir / "nested" / "custom_bg.json"
    _write_json(
        category_path,
        {
            "category": "bg",
            "composites": [
                {
                    "usage_locations": [
                        {"script": "game/script.rpy", "line": 12},
                    ]
                }
            ],
        },
    )
    _write_json(index_path, {"categories": {"bg": "nested/custom_bg.json"}})
    captured: dict[str, Any] = {}

    def fake_restore_rpy_files(
        root: Path,
        *,
        archive_path: Path | None = None,
        only_files: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, str]:
        captured["root"] = root
        captured["only_files"] = only_files
        return {}

    monkeypatch.setattr(restore_module, "restore_rpy_files", fake_restore_rpy_files)

    result = restore_module.restore_from_manifest(
        game_root,
        manifest_index_path=index_path,
        report_dir=report_dir,
    )

    assert result == {}
    assert captured == {
        "root": game_root,
        "only_files": ["script.rpy"],
    }

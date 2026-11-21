"""Phase 2 regressions for untrusted manifest, archive, and live paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest

from converter.compose.apply_pass import Applier
from converter.compose.config import GameConfig
from converter.compose.errors import ApplyError, ManifestError
from converter.compose.manifest import ManifestWriter
from converter.compose.models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
    UsageLocation,
)
from converter.compose.parsing.files import files as compose_script_files
from converter.restore import (
    restore_decompile,
    restore_from_manifest,
    restore_rpy_files,
)
from converter.restore.errors import RestoreError
from converter.utility.safe_paths import (
    UnsafePathError,
    game_script_relative_path,
    resolve_game_output_path,
    resolve_under_root,
    safe_relative_path,
)


@pytest.mark.parametrize(
    "value",
    [
        "/absolute/path",
        "C:/drive/path",
        "C:drive-relative",
        r"\\server\share\file",
        r"\rooted\file",
        "../outside",
        r"..\outside",
        "safe/../../outside",
    ],
)
def test_safe_relative_path_rejects_escaping_syntax(value: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_relative_path(value)


def test_safe_relative_path_normalizes_mixed_separators() -> None:
    assert safe_relative_path(r"scripts\main.rpy").as_posix() == "scripts/main.rpy"


def test_game_script_paths_are_normalized_to_the_game_root(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()

    assert game_script_relative_path(game_root, "game/scripts/main.rpy").as_posix() == (
        "scripts/main.rpy"
    )
    assert game_script_relative_path(game_root, r"scripts\main.rpy").as_posix() == (
        "scripts/main.rpy"
    )
    with pytest.raises(UnsafePathError):
        game_script_relative_path(game_root, "game/../outside.rpy")


def test_resolve_under_root_rejects_a_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "target.txt").write_text("outside", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")

    with pytest.raises(UnsafePathError):
        resolve_under_root(root, "link/target.txt")


def test_shared_script_discovery_rejects_a_symlink_escape(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    outside = tmp_path / "outside.rpy"
    outside.write_text("label outside:\n    pass\n", encoding="utf-8")
    link = game_root / "linked.rpy"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )

    with pytest.raises(UnsafePathError, match="escapes"):
        compose_script_files(config)


def test_resolve_game_output_path_rejects_a_path_outside_the_output(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    output_root = game_root / "composites"
    game_root.mkdir()

    accepted = resolve_game_output_path(
        game_root,
        output_root,
        "game/composites/static/bg/home.png",
    )
    assert accepted == (output_root / "static/bg/home.png").resolve()

    with pytest.raises(UnsafePathError):
        resolve_game_output_path(
            game_root,
            output_root,
            "game/other/home.png",
        )
    with pytest.raises(UnsafePathError, match="not a generated image"):
        resolve_game_output_path(
            game_root,
            output_root,
            "game/composites/reports/manifest_index.json",
        )
    with pytest.raises(UnsafePathError, match="not a generated image"):
        resolve_game_output_path(
            game_root,
            output_root,
            "game/composites/static/rejected/home.png",
        )
    assert resolve_game_output_path(
        game_root,
        output_root,
        "game/composites/dynamic_review/bg/home.jpg",
    ) == (output_root / "dynamic_review/bg/home.jpg").resolve()


def test_apply_uses_the_explicit_report_scope_for_generated_image_exclusions(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    output_root = game_root / "composites"
    source = output_root / "static" / "scene" / "room.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=output_root,
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )
    record = CompositeRecord(
        composite_path="game/composites/static/scene/room.png",
        name="room",
        kind="image",
        status="static",
        definition=DefinitionLocation(script="game/script.rpy", line=1),
        usage_locations=(),
        ordered_layers=(),
        dimensions=(64, 32),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="scene",
    )
    manifest = CompositeManifest(
        composites=(record,),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={},
    )
    applier = Applier(config, report_dir=source.parent)

    with pytest.raises(ApplyError, match="Unsafe composite path"):
        applier._generated_composites(manifest)

    assert source.read_bytes() == b"png"


def test_apply_rejects_a_traversal_script_before_touching_live_files(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    report_dir = tmp_path / "reports"
    outside = tmp_path / "outside.rpy"
    outside.write_text("label outside:\n    pass\n", encoding="utf-8")
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        report_dir=report_dir,
        manifest_index_path=report_dir / "manifest_index.json",
    )
    record = CompositeRecord(
        composite_path="game/composites/static/bg/home.png",
        name="home",
        kind="image",
        status="static",
        definition=DefinitionLocation(script="game/script.rpy", line=1),
        usage_locations=(
            UsageLocation(
                script="../../outside.rpy",
                line=1,
                statement="show home",
            ),
        ),
        ordered_layers=(),
        dimensions=(64, 32),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="bg",
    )
    ManifestWriter(config).write(
        CompositeManifest(
            composites=(record,),
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
    )

    with pytest.raises(ManifestError, match="Unsafe script path"):
        Applier(config).apply()

    assert outside.read_text(encoding="utf-8") == "label outside:\n    pass\n"


def test_restore_rejects_a_traversal_archive_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rpatool.archive

    game_root = tmp_path / "game"
    game_root.mkdir()
    (game_root / "archive.rpa").write_bytes(b"placeholder")
    outside = tmp_path / "outside.rpy"
    outside.write_text("outside", encoding="utf-8")

    class UnsafeArchive:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def list(self) -> list[str]:
            return ["safe.rpy", "../outside.rpy"]

        def read(self, _filename: str) -> bytes:
            return b"overwritten"

    monkeypatch.setattr(rpatool.archive, "RenPyArchive", UnsafeArchive)

    with pytest.raises(RestoreError, match="Unsafe archive script path"):
        restore_rpy_files(game_root)

    assert outside.read_text(encoding="utf-8") == "outside"
    assert not (game_root / "safe.rpy").exists()


def test_apply_rejects_an_unsafe_definition_script(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )
    record = CompositeRecord(
        composite_path="game/composites/static/bg/home.png",
        name="home",
        kind="image",
        status="static",
        definition=DefinitionLocation(script="../../outside.rpy", line=1),
        usage_locations=(),
        ordered_layers=(),
        dimensions=(64, 32),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="bg",
    )

    with pytest.raises(ManifestError, match="Unsafe definition script path"):
        Applier(config)._source_condition_universe(record)


def test_apply_rejects_a_declaration_path_outside_the_game_root(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        declaration_path=tmp_path / "outside.rpy",
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )

    with pytest.raises(ApplyError, match="declaration path"):
        Applier(config)._declaration_path()


def test_global_screen_rewrite_preflights_every_target_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    first = game_root / "first.rpy"
    second = game_root / "second.rpy"
    original_text = 'screen test:\n    add "x" "../composites/static/bg/home.png"\n'
    first.write_text(original_text, encoding="utf-8")
    second.write_text(original_text, encoding="utf-8")
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=game_root / "composites",
        report_dir=tmp_path / "reports",
        manifest_index_path=tmp_path / "reports" / "manifest_index.json",
    )
    original_read_text = Path.read_text

    def fail_second_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "second.rpy":
            raise OSError("simulated later script read failure")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fail_second_read)

    with pytest.raises(OSError, match="later script read failure"):
        Applier(config)._replace_generated_screen_paths(
            {"home": "composites/static/bg/home.png"}
        )

    assert first.read_text(encoding="utf-8") == original_text


def test_restore_decompile_rejects_a_game_folder_outside_the_allowed_root(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"game_folder": str(outside)}),
        encoding="utf-8",
    )

    with pytest.raises(RestoreError, match="outside"):
        restore_decompile(config_path, allowed_root=allowed_root)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert config_path.is_file()


def test_restore_rejects_a_traversal_script_in_a_manifest(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    index_path = report_dir / "manifest_index.json"
    category_path = report_dir / "manifest_bg.json"
    category_path.write_text(
        json.dumps(
            {
                "category": "bg",
                "composites": [
                    {
                        "usage_locations": [
                            {
                                "script": "../../outside.rpy",
                                "line": 1,
                            }
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    index_path.write_text(
        json.dumps({"categories": {"bg": "manifest_bg.json"}}),
        encoding="utf-8",
    )

    with pytest.raises(RestoreError, match="Unsafe game script path"):
        restore_from_manifest(
            game_root,
            manifest_index_path=index_path,
            report_dir=report_dir,
        )

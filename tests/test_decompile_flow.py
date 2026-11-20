"""Tests for the canonical decompile preparation service."""

from __future__ import annotations

from pathlib import Path

from converter.decompile import flow
from converter.decompile.decompile_files import DecompileFilesResult
from converter.decompile.extract import ArchiveExtraction


def _successful_decompile_result() -> DecompileFilesResult:
    return DecompileFilesResult(files=())


def test_prepare_game_stages_source_and_writes_config(tmp_path: Path, monkeypatch) -> None:
    renpy_games = tmp_path / "renpy_game"
    source_game = renpy_games / "Example"
    source_game_folder = source_game / "game"
    source_game_folder.mkdir(parents=True)
    source_script = source_game_folder / "script.rpy"
    source_script.write_text("label start:\n    pass\n", encoding="utf-8")

    destination = renpy_games / "game"
    config_path = tmp_path / "config.json"
    calls: list[str] = []

    monkeypatch.setattr(
        flow,
        "find_game_folder",
        lambda _root, **_kwargs: source_game_folder,
    )

    def fake_copy(source: Path, dest: Path) -> list[Path]:
        calls.append("copy")
        dest.mkdir(parents=True)
        copied = dest / "script.rpy"
        copied.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
        return [copied]

    monkeypatch.setattr(flow, "copy_game_files", fake_copy)
    monkeypatch.setattr(
        flow,
        "extract_all_rpa_detailed",
        lambda _folder: calls.append("extract") or [],
    )
    monkeypatch.setattr(
        flow,
        "decompile_rpyc_files_detailed",
        lambda _folder: calls.append("decompile") or _successful_decompile_result(),
    )
    monkeypatch.setattr(
        flow,
        "scan_image_categories",
        lambda _folder: {
            "classification_method": "folder_based",
            "image_categories": {
                "bg": "archive/bgs",
                "char": "archive/chars",
                "gui": "archive/gui",
                "scene": "archive/xxx",
            },
        },
    )

    def fake_write_config(*_args, **kwargs) -> Path:
        calls.append("config")
        output = Path(kwargs["config_path"])
        output.write_text("{}\n", encoding="utf-8")
        return output

    monkeypatch.setattr(flow, "write_config", fake_write_config)

    result = flow.prepare_game(
        renpy_games=renpy_games,
        dest=destination,
        config_path=config_path,
    )

    assert result.success
    assert result.game_folder == destination
    assert result.config == config_path
    assert calls == ["copy", "extract", "decompile", "config"]
    assert source_script.read_text(encoding="utf-8") == "label start:\n    pass\n"
    assert (destination / "script.rpy").is_file()


def test_prepare_game_dry_run_does_not_modify_files(
    tmp_path: Path, monkeypatch
) -> None:
    renpy_games = tmp_path / "renpy_game"
    source_game_folder = renpy_games / "Example" / "game"
    source_game_folder.mkdir(parents=True)
    (source_game_folder / "script.rpy").write_text("pass\n", encoding="utf-8")

    monkeypatch.setattr(
        flow,
        "find_game_folder",
        lambda _root, **_kwargs: source_game_folder,
    )
    monkeypatch.setattr(
        flow,
        "copy_game_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not stage files")
        ),
    )
    monkeypatch.setattr(
        flow,
        "extract_all_rpa_detailed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not extract archives")
        ),
    )
    monkeypatch.setattr(
        flow,
        "write_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not write config")
        ),
    )

    result = flow.prepare_game(
        renpy_games=renpy_games,
        dest=renpy_games / "game",
        config_path=tmp_path / "config.json",
        dry_run=True,
    )

    assert result.success
    assert result.dry_run
    assert result.config is None
    assert not (renpy_games / "game").exists()


def test_prepare_game_does_not_write_config_after_archive_failure(
    tmp_path: Path, monkeypatch
) -> None:
    renpy_games = tmp_path / "renpy_game"
    source_game_folder = renpy_games / "Example" / "game"
    source_game_folder.mkdir(parents=True)
    (source_game_folder / "script.rpy").write_text("pass\n", encoding="utf-8")

    monkeypatch.setattr(
        flow,
        "find_game_folder",
        lambda _root, **_kwargs: source_game_folder,
    )
    monkeypatch.setattr(flow, "copy_game_files", lambda _source, dest: [])
    monkeypatch.setattr(
        flow,
        "extract_all_rpa_detailed",
        lambda _folder: [
            ArchiveExtraction(
                archive=_folder / "archive.rpa",
                extracted_folder=_folder / "archive",
                error="corrupt archive",
            )
        ],
    )
    monkeypatch.setattr(
        flow,
        "write_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed extraction must not write config")
        ),
    )

    result = flow.prepare_game(
        renpy_games=renpy_games,
        dest=renpy_games / "game",
        config_path=tmp_path / "config.json",
    )

    assert not result.success
    assert result.config is None
    assert any("corrupt archive" in error for error in result.errors)


def test_prepare_game_rejects_destination_inside_source_game(
    tmp_path: Path, monkeypatch
) -> None:
    renpy_games = tmp_path / "renpy_game"
    source_game_folder = renpy_games / "Example" / "game"
    source_game_folder.mkdir(parents=True)
    source_script = source_game_folder / "script.rpy"
    source_script.write_text("pass\n", encoding="utf-8")

    monkeypatch.setattr(
        flow,
        "find_game_folder",
        lambda _root, **_kwargs: source_game_folder,
    )
    result = flow.prepare_game(
        renpy_games=renpy_games,
        dest=source_game_folder / "staged",
        config_path=tmp_path / "config.json",
    )

    assert not result.success
    assert any("overlap" in error for error in result.errors)
    assert source_script.read_text(encoding="utf-8") == "pass\n"


def test_decompile_run_summary_counts_only_successful_archives() -> None:
    game_folder = Path("game")
    result = flow.DecompileRun(
        game_folder=game_folder,
        extracted_archives=(
            ArchiveExtraction(
                archive=game_folder / "good.rpa",
                extracted_folder=game_folder / "good",
            ),
            ArchiveExtraction(
                archive=game_folder / "bad.rpa",
                extracted_folder=game_folder / "bad",
                error="corrupt archive",
            ),
        ),
        config=Path("config.json"),
    )

    summary = result.to_dict()

    assert summary["extracted_archives"] == [
        {"archive": "good.rpa", "extracted_folder": "good"}
    ]


def test_prepare_game_reports_missing_source(monkeypatch) -> None:
    monkeypatch.setattr(flow, "find_game_folder", lambda _root, **_kwargs: None)

    result = flow.prepare_game()

    assert not result.success
    assert result.game_folder is None
    assert result.config is None

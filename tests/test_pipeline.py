"""Tests for the unified decompile-to-compose pipeline."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from converter import pipeline
from converter.decompile.flow import DecompileRun


def _successful_run(*, dry_run: bool = False) -> DecompileRun:
    return DecompileRun(
        game_folder=Path("staged/game"),
        source_game="Example",
        source_path=Path("runtime"),
        copied=1,
        extracted_archives=(),
        decompiled=0,
        config=None if dry_run else Path("config.json"),
        dry_run=dry_run,
        image_categories={},
        errors=(),
    )


def test_default_pipeline_uses_canonical_preparation_and_refreshed_config(
    tmp_path: Path, monkeypatch
) -> None:
    renpy_games = tmp_path / "renpy_game"
    destination = renpy_games / "game"
    config_path = tmp_path / "config.json"
    prepared: list[tuple[Path, Path, Path]] = []
    composed: list[Any] = []
    expected_config = SimpleNamespace(name="Example")

    def fake_prepare_game(**kwargs) -> DecompileRun:
        prepared.append(
            (kwargs["renpy_games"], kwargs["dest"], kwargs["config_path"])
        )
        return _successful_run()

    monkeypatch.setattr(pipeline, "prepare_game", fake_prepare_game)
    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pipeline,
        "compose_game",
        lambda config, **_kwargs: composed.append(config),
    )

    code = pipeline.run_pipeline(
        renpy_games=renpy_games,
        dest=destination,
        config_path=config_path,
    )

    assert code == 0
    assert prepared == [(renpy_games, destination, config_path)]
    assert composed == [expected_config]


def test_default_pipeline_dry_run_does_not_compose(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        pipeline,
        "prepare_game",
        lambda **_kwargs: _successful_run(dry_run=True),
    )
    monkeypatch.setattr(
        pipeline,
        "compose_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default dry-run must not compose")
        ),
    )

    code = pipeline.run_pipeline(dry_run=True)

    assert code == 0


def test_default_pipeline_composes_the_config_returned_by_preparation(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "requested-config.json"
    prepared_config = tmp_path / "prepared-config.json"
    loaded_paths: list[Path] = []
    expected_config = SimpleNamespace(name="Example")

    monkeypatch.setattr(
        pipeline,
        "prepare_game",
        lambda **_kwargs: DecompileRun(
            game_folder=prepared_config.parent / "game",
            source_game="Example",
            source_path=prepared_config.parent,
            copied=1,
            config=prepared_config,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "load_config",
        lambda path: loaded_paths.append(path) or expected_config,
    )
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(pipeline, "compose_game", lambda *_args, **_kwargs: None)

    code = pipeline.run_pipeline(config_path=config_path)

    assert code == 0
    assert loaded_paths == [prepared_config]


def test_pipeline_reports_compose_failure(monkeypatch) -> None:
    expected_config = SimpleNamespace(name="Example")

    monkeypatch.setattr(
        pipeline,
        "prepare_game",
        lambda **_kwargs: _successful_run(),
    )
    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(
        pipeline,
        "compose_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("native renderer failed")
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed compose must not print review instructions")
        ),
    )

    code = pipeline.run_pipeline()

    assert code == 1


def test_compose_only_dry_run_does_not_print_review_instructions(monkeypatch) -> None:
    expected_config = SimpleNamespace(name="Example")

    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(pipeline, "compose_game", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not print stale review instructions")
        ),
    )

    code = pipeline.run_pipeline(compose_only=True, dry_run=True)

    assert code == 0


def test_compose_only_does_not_run_decompile(monkeypatch) -> None:
    expected_config = SimpleNamespace(name="Example")
    composed: list[Any] = []

    monkeypatch.setattr(
        pipeline,
        "prepare_game",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("compose-only must not decompile")
        ),
    )
    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pipeline,
        "compose_game",
        lambda config, **_kwargs: composed.append(config),
    )

    code = pipeline.run_pipeline(compose_only=True)

    assert code == 0
    assert composed == [expected_config]


def test_apply_only_dry_run_does_not_apply(monkeypatch) -> None:
    expected_config = SimpleNamespace(name="Example")
    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(
        pipeline,
        "apply_game",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("apply-only dry-run must not apply")
        ),
    )

    code = pipeline.run_pipeline(apply_only=True, dry_run=True)

    assert code == 0


def test_pipeline_modes_are_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.setattr(
        pipeline,
        "prepare_game",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid mode combination must not start")
        ),
    )

    code = pipeline.run_pipeline(compose_only=True, apply_only=True)

    assert code == 2

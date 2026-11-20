"""CLI-level regression tests for reliable exit codes."""

from __future__ import annotations

from typer.testing import CliRunner

from converter import pipeline
from converter.decompile import decompile

runner = CliRunner()


def test_decompile_command_returns_nonzero_for_failed_run(monkeypatch) -> None:
    monkeypatch.setattr(
        decompile,
        "process_game",
        lambda **_kwargs: {
            "success": False,
            "game_folder": None,
            "config": None,
        },
    )

    result = runner.invoke(decompile.app, ["decompile"])

    assert result.exit_code == 1


def test_root_dry_run_option_is_forwarded_to_decompile_subcommand(
    monkeypatch,
) -> None:
    received: dict[str, bool] = {}

    def fake_process_game(**kwargs) -> dict[str, object]:
        received.update(kwargs)
        return {"success": True, "game_folder": "game", "config": "config.json"}

    monkeypatch.setattr(decompile, "process_game", fake_process_game)

    result = runner.invoke(decompile.app, ["--dry-run", "decompile"])

    assert result.exit_code == 0
    assert received["dry_run"] is True


def test_pipeline_command_rejects_mutually_exclusive_modes() -> None:
    result = runner.invoke(
        pipeline.app,
        ["--compose-only", "--apply-only"],
    )

    assert result.exit_code == 2


def test_pipeline_command_propagates_run_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **_kwargs: 7)

    result = runner.invoke(pipeline.app, [])

    assert result.exit_code == 7

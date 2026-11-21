"""Phase 3 regressions for native compose performance and publication safety."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from converter.compose.apply_pass import Applier
from converter.compose.config import GameConfig, load_config
from converter.compose.errors import ManifestError, NativeExecutionError
from converter.compose.manifest import ManifestWriter
from converter.compose.models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
)
from converter.compose.native.constants import (
    PROTOCOL_SCHEMA,
    PROTOCOL_VERSION,
)
from converter.compose.native.process import (
    PersistentNativeWorker,
    _native_environment,
)
from converter.compose.native.prune import prune_stale_generated_images
from converter.compose.native.stage import (
    _copy_render,
    _discovery_fingerprint,
    _run_batches,
    _validate_modes,
)

_RUNTIME_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "converter"
    / "compose"
    / "native"
    / "runtime"
    / "composite_export.rpy"
)


def _runtime_python_tree() -> ast.Module:
    source = _RUNTIME_PATH.read_text(encoding="utf-8")
    body = source.split("init -999 python in _composite_export:\n", 1)[1]
    body = body.split("\ninit -998 python:", 1)[0]
    lines = [line.removeprefix("    ") for line in body.splitlines()]
    return ast.parse("\n".join(lines) + "\n", filename=str(_RUNTIME_PATH))


def _load_runtime_functions(
    names: set[str],
    namespace: dict[str, Any],
) -> dict[str, Any]:
    tree = _runtime_python_tree()
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(  # noqa: S102 - execute selected runtime functions in an isolated namespace
        compile(module, str(_RUNTIME_PATH), "exec"), namespace
    )
    return namespace


@dataclass
class _Attribute:
    attribute: str
    group: str | None = None
    default: bool = False


class _ConditionLayer:
    def __init__(self, condition: str) -> None:
        self.condition = condition


class _LayeredImage:
    def __init__(
        self,
        *,
        attributes: list[_Attribute] | None = None,
        conditions: list[str] | None = None,
    ) -> None:
        self.attributes = attributes or []
        self.layers = [_ConditionLayer(value) for value in conditions or []]


def _budget_namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "_STRING_TYPES": (str,),
        "_IDENT_RE": re.compile(r"\b[A-Za-z_]\w*\b"),
        "_EQ_RE": re.compile(
            r"\b([A-Za-z_]\w*)\s*==\s*"
            r"(-?\d+(?:\.\d+)?|True|False|None|'[^']*'|\"[^\"]*\")"
        ),
        "_KEYWORDS": {
            "True",
            "False",
            "None",
            "and",
            "or",
            "not",
            "in",
            "is",
            "if",
            "else",
            "elif",
            "for",
            "while",
            "lambda",
        },
    }
    return _load_runtime_functions(
        {
            "_NativeLimitError",
            "_build_states",
            "_condition_assignments",
            "_condition_texts",
            "_condition_variables",
            "_parse_literal",
            "_synthetic_states",
        },
        namespace,
    )


def _usage(tag: str, attributes: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "tag": tag,
        "attributes": attributes,
        "image_name": (tag, *attributes),
    }


def test_runtime_mandatory_cfg_overflow_fails_explicitly() -> None:
    namespace = _budget_namespace()
    config = {
        "modes": ["cfg"],
        "max_total_states": 1,
        "max_total_variants": 10,
    }

    with pytest.raises(namespace["_NativeLimitError"], match="mandatory states"):
        namespace["_build_states"](
            config,
            [_usage("home"), _usage("away")],
            {},
            {},
        )


def test_runtime_optional_variants_truncate_deterministically() -> None:
    namespace = _budget_namespace()
    image = _LayeredImage(
        attributes=[_Attribute("pose", group="master")],
        conditions=["flag == False"],
    )
    config = {
        "modes": ["cfg", "conditions"],
        "max_total_states": 1,
        "max_total_variants": 10,
        "max_condition_assignments": 1,
    }

    states, budget = namespace["_build_states"](
        config,
        [_usage("hero", ("pose",))],
        {"hero": (("hero",), image)},
        {"flag": True},
    )

    assert len(states) == 1
    assert states[0]["condition_assignments"] == {}
    assert budget["truncated"] is True
    assert budget["condition_variants"] == 0
    assert budget["skipped_variants"] >= 1


def test_runtime_prioritizes_condition_variants_before_synthetic_inventory() -> None:
    namespace = _budget_namespace()
    image = _LayeredImage(
        attributes=[_Attribute("pose", group="master")],
        conditions=["flag == False"],
    )
    config = {
        "modes": ["cfg", "conditions", "layered_all"],
        "max_total_states": 2,
        "max_total_variants": 1,
        "max_condition_assignments": 1,
        "max_synthetic_states": 8,
    }

    states, budget = namespace["_build_states"](
        config,
        [_usage("hero", ("pose",))],
        {"hero": (("hero",), image)},
        {"flag": True},
    )

    assert len(states) == 2
    assert states[1]["condition_assignments"] == {"flag": False}
    assert budget["condition_variants"] == 1
    assert budget["synthetic_states"] == 0
    assert budget["truncated"] is True


def test_discovery_cache_is_reusable_across_disposable_workspace_paths(
    tmp_path: Path,
) -> None:
    namespace: dict[str, Any] = {"hashlib": hashlib, "json": json, "os": os}
    _load_runtime_functions(
        {"_dedupe_usages", "_load_states", "_save_states", "_states_signature"},
        namespace,
    )
    first_config = {
        "game_root": str(tmp_path / "workspace-a" / "game"),
        "discovery_fingerprint": "fingerprint-a",
        "modes": ["cfg", "conditions"],
        "defaults": {"flag": True},
        "max_total_states": 100,
        "max_total_variants": 200,
    }
    second_config = dict(first_config)
    second_config["game_root"] = str(tmp_path / "workspace-b" / "game")
    first_signature = namespace["_states_signature"](first_config)
    second_signature = namespace["_states_signature"](second_config)
    cache_path = tmp_path / "discovery.json"
    state = {
        "kind": "image",
        "tag": "home",
        "attributes": ("home",),
        "condition_assignments": {},
        "synthetic": False,
        "usages": [],
    }
    namespace["_save_states"](
        str(cache_path),
        first_signature,
        [state],
        {"processed": 1, "truncated": False},
        metadata={"layered_images": []},
        budget={"total_states": 1},
        state_counts={"image": 1},
        discovery_fingerprint="fingerprint-a",
    )

    reused = namespace["_load_states"](
        str(cache_path),
        second_signature,
        "fingerprint-a",
    )

    assert first_signature == second_signature
    assert reused is not None
    assert reused[0][0]["tag"] == "home"


def test_discovery_cache_rejects_a_different_fingerprint(tmp_path: Path) -> None:
    namespace: dict[str, Any] = {"json": json, "os": os}
    _load_runtime_functions(
        {"_dedupe_usages", "_load_states", "_save_states"},
        namespace,
    )
    cache_path = tmp_path / "discovery.json"
    state = {
        "kind": "image",
        "tag": "home",
        "attributes": ("home",),
        "condition_assignments": {},
        "synthetic": False,
        "usages": [
            {
                "script": "game/script.rpy",
                "line": 1,
                "statement": "show home",
                "image_name": ("home",),
            }
        ],
    }
    namespace["_save_states"](
        str(cache_path),
        "signature",
        [state],
        {"processed": 1, "truncated": False},
        metadata={"layered_images": []},
        budget={"total_states": 1},
        state_counts={"image": 1},
        discovery_fingerprint="fingerprint-a",
    )

    loaded = namespace["_load_states"](
        str(cache_path),
        "signature",
        "fingerprint-a",
    )
    rejected = namespace["_load_states"](
        str(cache_path),
        "signature",
        "fingerprint-b",
    )

    assert loaded is not None
    assert loaded[0][0]["tag"] == "home"
    assert rejected is None


def test_discovery_fingerprint_covers_scripts_modes_defaults_and_budgets(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    script = game_root / "script.rpy"
    script.write_text("label start:\n    pass\n", encoding="utf-8")
    layout = SimpleNamespace(version="8.3.2")

    def fingerprint() -> str:
        return _discovery_fingerprint(
            game_root=game_root,
            script_files=["script.rpy"],
            layout=layout,  # type: ignore[arg-type]
            modes=["cfg", "conditions"],
            defaults={"flag": True},
            max_total_states=100,
            max_total_variants=200,
        )

    original = fingerprint()
    script.write_text("label start:\n    show home\n", encoding="utf-8")
    changed_script = fingerprint()
    script.write_text("label start:\n    pass\n", encoding="utf-8")

    assert fingerprint() == original
    assert changed_script != original
    assert (
        _discovery_fingerprint(
            game_root=game_root,
            script_files=["script.rpy"],
            layout=layout,  # type: ignore[arg-type]
            modes=["cfg"],
            defaults={"flag": True},
            max_total_states=100,
            max_total_variants=200,
        )
        != original
    )
    assert (
        _discovery_fingerprint(
            game_root=game_root,
            script_files=["script.rpy"],
            layout=layout,  # type: ignore[arg-type]
            modes=["cfg", "conditions"],
            defaults={"flag": False},
            max_total_states=100,
            max_total_variants=200,
        )
        != original
    )
    assert (
        _discovery_fingerprint(
            game_root=game_root,
            script_files=["script.rpy"],
            layout=layout,  # type: ignore[arg-type]
            modes=["cfg", "conditions"],
            defaults={"flag": True},
            max_total_states=99,
            max_total_variants=200,
        )
        != original
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, ["cfg", "layered_all", "conditions"]),
        ([], ["cfg", "layered_all", "conditions"]),
        (["all"], ["cfg", "layered_all", "conditions"]),
        (["condition"], ["cfg", "conditions"]),
        (["layered"], ["cfg", "layered_all"]),
        (["declaration"], ["cfg"]),
        (["screen"], ["cfg"]),
        (["conditions"], ["cfg", "conditions"]),
    ],
)
def test_native_mode_aliases_normalize_to_canonical_modes(
    requested: list[str] | None,
    expected: list[str],
) -> None:
    assert _validate_modes(requested) == expected


def test_config_loader_normalizes_legacy_mode_names(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "game_folder": str(game_root),
                "source_path": str(tmp_path / "runtime"),
                "compose_modes": ["ALL", "condition"],
                "extracted_archives": [],
                "image_categories": {
                    "classification_method": "folder_based",
                    "image_categories": {},
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.compose_modes == ("cfg", "layered_all", "conditions")


def test_native_child_environment_does_not_inherit_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "secret-api-key")
    monkeypatch.setenv("GH_TOKEN", "secret-github-token")
    monkeypatch.setenv("PYTHONPATH", "unsafe-import-path")
    monkeypatch.setenv("RENPY_SEARCHPATH", "unsafe-renpy-path")

    environment = _native_environment(
        basedir=tmp_path,
        savedir=tmp_path / "savedir",
        config_path=tmp_path / "native_config.json",
        result_path=tmp_path / "native-result.json",
    )

    assert "NVIDIA_API_KEY" not in environment
    assert "GH_TOKEN" not in environment
    assert "PYTHONPATH" not in environment
    assert "RENPY_SEARCHPATH" not in environment
    assert environment["RENPY_COMPOSITE_CONFIG"] == str(
        tmp_path / "native_config.json"
    )
    assert environment["RENPY_COMPOSITE_OUTPUT"] == str(
        tmp_path / "native-result.json"
    )
    assert environment["TEMP"] == str(tmp_path / "tmp")
    assert environment["TMP"] == str(tmp_path / "tmp")
    assert environment["TMPDIR"] == str(tmp_path / "tmp")


def test_persistent_worker_preserves_zero_logical_batch_identity(
    tmp_path: Path,
) -> None:
    worker = PersistentNativeWorker(
        layout=SimpleNamespace(command=lambda **kwargs: ["unused"]),  # type: ignore[arg-type]
        basedir=tmp_path,
        savedir=tmp_path / "savedir",
    )
    worker.jobs_dir.mkdir(parents=True)
    worker.results_dir.mkdir(parents=True)
    worker.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]
    stale_done = worker.jobs_dir / "job-000007.done"
    stale_done.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    stale_result = worker.results_dir / "result-000007.jsonl"
    stale_result.write_text("stale", encoding="utf-8")
    captured: dict[str, Any] = {}

    def complete_job() -> None:
        deadline = time.monotonic() + 5
        job_path: Path | None = None
        while time.monotonic() < deadline:
            candidates = list(worker.jobs_dir.glob("job-*.json"))
            if candidates:
                job_path = candidates[0]
                break
            time.sleep(0.01)
        if job_path is None:
            return
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        captured.update(payload)
        result_path = Path(payload["result_path"])
        result_path.write_text("{}", encoding="utf-8")
        done_path = job_path.with_suffix(".done")
        done_path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")

    thread = threading.Thread(target=complete_job)
    thread.start()
    try:
        result_path = worker.run_job(
            7,
            {"job_id": 0, "attempt": 1},
            progress_path=tmp_path / "progress.log",
            timeout=5,
        )
    finally:
        thread.join(timeout=5)

    assert result_path.name.startswith("result-000007-")
    assert result_path.name.endswith(".jsonl")
    assert result_path.read_text(encoding="utf-8") == "{}"
    assert captured["job_id"] == 0
    assert captured["submission_id"] in result_path.name
    assert stale_done.is_file()


def test_runtime_uses_the_matching_worker_completion_marker() -> None:
    source = _RUNTIME_PATH.read_text(encoding="utf-8")

    assert 'os.path.splitext(job_path)[0] + ".done"' in source
    assert ".json.done" not in source


def test_runtime_atomic_render_temporary_keeps_a_png_suffix() -> None:
    source = _RUNTIME_PATH.read_text(encoding="utf-8")

    assert 'temporary = path + ".tmp-" + str(os.getpid()) + ".png"' in source


def test_persistent_worker_rejects_a_progress_path_outside_its_workspace(
    tmp_path: Path,
) -> None:
    basedir = tmp_path / "workspace"
    basedir.mkdir()
    worker = PersistentNativeWorker(
        layout=SimpleNamespace(command=lambda **kwargs: ["unused"]),  # type: ignore[arg-type]
        basedir=basedir,
        savedir=basedir / "savedir",
    )
    worker.jobs_dir.mkdir(parents=True)
    worker.results_dir.mkdir(parents=True)
    worker.process = SimpleNamespace(poll=lambda: None)  # type: ignore[assignment]

    with pytest.raises(NativeExecutionError, match="inside the disposable workspace"):
        worker.run_job(
            1,
            {"job_id": 1, "attempt": 1},
            progress_path=tmp_path / "outside.log",
            timeout=1,
        )


def test_persistent_worker_start_clears_stale_queue_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PersistentNativeWorker(
        layout=SimpleNamespace(command=lambda **kwargs: ["unused"]),  # type: ignore[arg-type]
        basedir=tmp_path,
        savedir=tmp_path / "savedir",
    )
    worker.jobs_dir.mkdir(parents=True)
    worker.results_dir.mkdir(parents=True)
    stale_job = worker.jobs_dir / "job-stale.json"
    stale_done = worker.jobs_dir / "job-stale.done"
    stale_result = worker.results_dir / "result-stale.jsonl"
    for path in (stale_job, stale_done, stale_result):
        path.write_text("stale", encoding="utf-8")

    class FakeProcess:
        returncode = 0
        pid = 1234

        def poll(self) -> int:
            return 0

    def fake_launch(*_args: Any, **_kwargs: Any) -> FakeProcess:
        worker.ready_path.write_text("ready", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(
        "converter.compose.native.process._launch_process",
        fake_launch,
    )

    worker.start()
    try:
        assert not stale_job.exists()
        assert not stale_done.exists()
        assert not stale_result.exists()
    finally:
        worker.close()


def test_persistent_worker_start_failure_falls_back_to_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SimpleNamespace(
        root=tmp_path,
        game_dir=tmp_path / "game",
        savedir=tmp_path / "savedir",
        config_path=tmp_path / "native_config.json",
        result_path=tmp_path / "native-result.json",
    )
    workspace.game_dir.mkdir()
    workspace.savedir.mkdir()
    calls: list[dict[str, Any]] = []

    class FailingWorker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise NativeExecutionError("worker unavailable")

    def fake_run_native_batch(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        config = json.loads(kwargs["config_path"].read_text(encoding="utf-8"))
        batch = {
            "index": config["batch_index"],
            "state_offset": config["state_offset"],
            "rendered": 0,
            "total_states": 0,
            "next_offset": None,
            "walk": {"processed": 0, "truncated": False},
            "identity": {
                "session_id": config["session_id"],
                "job_id": config["job_id"],
                "attempt": config["attempt"],
            },
        }
        result = {
            "protocol": PROTOCOL_VERSION,
            "schema_version": PROTOCOL_SCHEMA,
            "status": "ok",
            "batch": batch,
            "candidates": [],
            "layered_images": [],
            "image_definitions": {},
            "characters": {},
        }
        kwargs["result_path"].write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="", command=())

    monkeypatch.setattr(
        "converter.compose.native.stage.PersistentNativeWorker",
        FailingWorker,
    )
    monkeypatch.setattr(
        "converter.compose.native.stage.run_native_batch",
        fake_run_native_batch,
    )

    result = _run_batches(
        workspace,  # type: ignore[arg-type]
        ["cfg"],
        {},
        SimpleNamespace(),  # type: ignore[arg-type]
        5,
        render_dir=tmp_path / "renders",
        states_path=tmp_path / "discovery.json",
        max_total_states=100,
        max_total_variants=200,
        discovery_fingerprint="fingerprint",
        session_id="session",
        persistent_worker=True,
        header_cb=lambda result: None,
        candidate_cb=lambda candidate: None,
    )

    assert len(calls) == 1
    assert result["batch"]["total_states"] == 0


def test_one_shot_retry_halves_the_batch_and_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SimpleNamespace(
        root=tmp_path,
        game_dir=tmp_path / "game",
        savedir=tmp_path / "savedir",
        config_path=tmp_path / "native_config.json",
        result_path=tmp_path / "native-result.json",
    )
    workspace.game_dir.mkdir()
    workspace.savedir.mkdir()
    calls: list[dict[str, Any]] = []

    def fake_run_native_batch(**kwargs: Any) -> SimpleNamespace:
        config = json.loads(kwargs["config_path"].read_text(encoding="utf-8"))
        calls.append(config)
        if len(calls) == 1:
            raise NativeExecutionError("simulated renderer failure")
        batch = {
            "index": 0,
            "state_offset": 0,
            "rendered": 0,
            "total_states": 0,
            "next_offset": None,
            "walk": {"processed": 0, "truncated": False},
            "identity": {
                "session_id": config["session_id"],
                "job_id": config["job_id"],
                "attempt": config["attempt"],
            },
        }
        result = {
            "protocol": PROTOCOL_VERSION,
            "schema_version": PROTOCOL_SCHEMA,
            "status": "ok",
            "batch": batch,
            "candidates": [],
            "layered_images": [],
            "image_definitions": {},
            "characters": {},
        }
        kwargs["result_path"].write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="", command=())

    monkeypatch.setattr(
        "converter.compose.native.stage.run_native_batch",
        fake_run_native_batch,
    )

    result = _run_batches(
        workspace,  # type: ignore[arg-type]
        ["cfg"],
        {},
        SimpleNamespace(),  # type: ignore[arg-type]
        5,
        render_dir=tmp_path / "renders",
        states_path=tmp_path / "discovery.json",
        max_total_states=100,
        max_total_variants=200,
        discovery_fingerprint="fingerprint",
        session_id="session",
        persistent_worker=False,
        header_cb=lambda result: None,
        candidate_cb=lambda candidate: None,
    )

    assert len(calls) == 2
    assert calls[0]["max_render_states"] == 512
    assert calls[1]["max_render_states"] == 256
    assert calls[0]["attempt"] == 1
    assert calls[1]["attempt"] == 2
    assert result["batch"]["identity"] == {
        "session_id": "session",
        "job_id": 0,
        "attempt": 2,
    }


def _png_bytes(width: int = 2, height: int = 2) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_copy_render_uses_a_verified_content_addressed_store(
    tmp_path: Path,
) -> None:
    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir()
    content_hash = "a" * 64
    source = render_dir / f"{content_hash}.png"
    source.write_bytes(_png_bytes())
    target = tmp_path / "output" / "static" / "bg" / "home.png"

    published = _copy_render(
        {
            "render_file": source.name,
            "width": 2,
            "height": 2,
        },
        target,
        render_dir,
        cas_dir,
    )

    assert published is True
    assert source.is_file()
    assert target.read_bytes() == source.read_bytes()
    cas_files = list(cas_dir.rglob("*.png"))
    assert len(cas_files) == 1
    assert cas_files[0].read_bytes() == source.read_bytes()


def test_copy_render_copy_failure_preserves_the_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    source = render_dir / "source.png"
    source.write_bytes(_png_bytes())
    target = tmp_path / "output" / "static" / "bg" / "home.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous")

    def no_links(source: Path, target: Path) -> None:
        raise OSError("no links")

    def fail_copy(source: Path, target: Path) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(
        "converter.compose.native.stage.os.link",
        no_links,
    )
    monkeypatch.setattr(
        "converter.compose.native.stage.shutil.copy2",
        fail_copy,
    )

    assert not _copy_render(
        {"render_file": source.name, "width": 2, "height": 2},
        target,
        render_dir,
    )
    assert target.read_bytes() == b"previous"
    assert source.is_file()


def test_copy_render_rejects_invalid_png_and_corrupt_cas(
    tmp_path: Path,
) -> None:
    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    cas_dir = tmp_path / "cas"
    cas_dir.mkdir()
    source = render_dir / "bad.png"
    source.write_bytes(b"not a png")
    target = tmp_path / "output" / "static" / "bg" / "bad.png"

    assert not _copy_render(
        {"render_file": source.name, "width": 2, "height": 2},
        target,
        render_dir,
        cas_dir,
    )
    assert not target.exists()

    valid_source = render_dir / "valid.png"
    valid_source.write_bytes(_png_bytes())
    blob_hash = hashlib.sha256(valid_source.read_bytes()).hexdigest()
    corrupt = cas_dir / blob_hash[:2] / f"{blob_hash}.png"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"corrupt")
    corrupt_target = tmp_path / "output" / "static" / "bg" / "valid.png"

    assert not _copy_render(
        {"render_file": valid_source.name, "width": 2, "height": 2},
        corrupt_target,
        render_dir,
        cas_dir,
    )
    assert not corrupt_target.exists()


def _prune_config(tmp_path: Path) -> GameConfig:
    game_root = tmp_path / "game"
    game_root.mkdir()
    output_dir = tmp_path / "output"
    report_dir = tmp_path / "reports"
    return GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        output_dir=output_dir,
        report_dir=report_dir,
        manifest_index_path=report_dir / "manifest_index.json",
    )


def _published_record(relative: str) -> CompositeRecord:
    return CompositeRecord(
        composite_path=relative,
        name="home",
        kind="image",
        status="static",
        definition=DefinitionLocation(script="game/script.rpy", line=1),
        usage_locations=(),
        ordered_layers=(),
        dimensions=(2, 2),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="bg",
    )


def test_apply_rejects_a_downgraded_bundle_before_live_mutation(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    script = config.game_root / "script.rpy"
    script.write_text("label start:\n    pass\n", encoding="utf-8")
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 0,
            "uses_processed": 0,
        },
    )
    ManifestWriter(config).write(manifest)
    index_path = config.resolved_paths()["manifest_index_path"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index.pop("bundle_version", None)
    index.pop("category_digests", None)
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ManifestError, match="bundle_version"):
        Applier(config).apply()

    assert script.read_text(encoding="utf-8") == "label start:\n    pass\n"
    assert not (config.game_root / "composite_images.rpy").exists()


def test_apply_treats_pruning_failure_as_post_commit_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prune_config(tmp_path)
    script = config.game_root / "script.rpy"
    script.write_text("label start:\n    pass\n", encoding="utf-8")
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 0,
            "uses_processed": 0,
        },
    )
    ManifestWriter(config).write(manifest)

    def fail_prune(*_args: Any, **_kwargs: Any) -> None:
        raise ManifestError("unsafe published reference")

    monkeypatch.setattr(
        "converter.compose.apply_pass.applier.prune_stale_generated_images",
        fail_prune,
    )

    _manifest, _report = Applier(config).apply()

    assert script.read_text(encoding="utf-8") == "label start:\n    pass\n"
    assert (config.game_root / "composite_images.rpy").is_file()


def test_pruner_uses_the_published_bundle_and_retains_live_references(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    generated = config.resolved_paths()["output_dir"] / "static" / "bg"
    generated.mkdir(parents=True)
    keep = generated / "keep.png"
    stale = generated / "stale.png"
    live = generated / "live.png"
    keep.write_bytes(_png_bytes())
    stale.write_bytes(_png_bytes())
    live.write_bytes(_png_bytes())
    shallow = config.resolved_paths()["output_dir"] / "static" / "shallow.png"
    shallow.write_bytes(_png_bytes())
    reserved = config.resolved_paths()["output_dir"] / "static" / "reports" / "x.png"
    reserved.parent.mkdir(parents=True)
    reserved.write_bytes(_png_bytes())
    script = config.game_root / "composite_images.rpy"
    script.write_text(
        'image live = "../output/static/bg/live.png"\n',
        encoding="utf-8",
    )
    manifest = CompositeManifest(
        composites=(_published_record("output/static/bg/keep.png"),),
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
    ManifestWriter(config).write(manifest)

    result = prune_stale_generated_images(config)

    assert result.removed == (stale,)
    assert keep.is_file()
    assert live.is_file()
    assert shallow.is_file()
    assert reserved.is_file()


def test_pruner_retains_references_from_renpy_translation_scripts(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    generated = config.resolved_paths()["output_dir"] / "static" / "bg"
    generated.mkdir(parents=True)
    live = generated / "translation.png"
    stale = generated / "stale.png"
    live.write_bytes(_png_bytes())
    stale.write_bytes(_png_bytes())
    script = config.game_root / "labels_ren.py"
    script.write_text(
        'image translated = "../output/static/bg/translation.png"\n',
        encoding="utf-8",
    )
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 0,
            "uses_processed": 1,
        },
    )
    ManifestWriter(config).write(manifest)

    result = prune_stale_generated_images(config)

    assert result.removed == (stale,)
    assert live.is_file()


def test_apply_replaces_generated_paths_in_renpy_translation_scripts(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    translation = config.game_root / "labels_ren.py"
    translation.write_text(
        'image translated = "../output/static/bg/home.png"\n',
        encoding="utf-8",
    )

    changed = Applier(config)._replace_generated_screen_paths(
        {"home": "output/static/bg/home.png"}
    )

    assert changed == 1
    assert translation.read_text(encoding="utf-8-sig") == (
        'image translated = "home"\n'
    )


def test_pruner_retains_dynamic_state_generated_references(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    generated = config.resolved_paths()["output_dir"] / "static" / "bg"
    generated.mkdir(parents=True)
    dynamic = generated / "dynamic.png"
    stale = generated / "stale.png"
    dynamic.write_bytes(_png_bytes())
    stale.write_bytes(_png_bytes())
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[
            {
                "condition": "flag",
                "generated_states": [
                    {
                        "composite_path": "output/static/bg/dynamic.png",
                        "name": "dynamic",
                    }
                ],
            }
        ],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 0,
            "uses_processed": 1,
        },
    )
    ManifestWriter(config).write(manifest)

    result = prune_stale_generated_images(config)

    assert result.removed == (stale,)
    assert dynamic.is_file()


def test_pruner_fails_closed_for_a_bundle_with_a_missing_reference(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    stale = config.resolved_paths()["output_dir"] / "static" / "bg" / "stale.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(_png_bytes())
    manifest = CompositeManifest(
        composites=(_published_record("output/static/bg/missing.png"),),
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
    ManifestWriter(config).write(manifest)

    result = prune_stale_generated_images(config)

    assert result.removed == ()
    assert result.skipped
    assert stale.is_file()


def test_pruner_refuses_an_unsafe_manifest_reference_before_deletion(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    stale = config.resolved_paths()["output_dir"] / "static" / "bg" / "stale.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(_png_bytes())
    manifest = CompositeManifest(
        composites=(_published_record("output/static/../outside.png"),),
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
    ManifestWriter(config).write(manifest)

    with pytest.raises(ManifestError, match="unsafe composite path"):
        prune_stale_generated_images(config)

    assert stale.is_file()


def test_pruner_refuses_a_tampered_category_before_deletion(
    tmp_path: Path,
) -> None:
    config = _prune_config(tmp_path)
    stale = config.resolved_paths()["output_dir"] / "static" / "bg" / "stale.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(_png_bytes())
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=(),
        summary={
            "scripts_scanned": 1,
            "composites": 0,
            "uses_processed": 0,
        },
    )
    ManifestWriter(config).write(manifest)
    category = config.resolved_paths()["report_dir"] / "manifest_bg.json"
    category.write_text(
        json.dumps({"category": "bg", "composites": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="integrity"):
        prune_stale_generated_images(config)

    assert stale.is_file()

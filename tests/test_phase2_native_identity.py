"""Phase 2 tests for native composite identity and diagnostic-state preservation."""

from __future__ import annotations

from pathlib import Path

from converter.compose.models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
    Layer,
)
from converter.compose.native.stage import (
    _native_record_key,
    _native_unresolved_errors,
    _native_walk_state,
)
from converter.compose.records import _record_key


def _layers() -> list[Layer]:
    return [
        Layer(
            source="bgs/home.png",
            line=10,
            conditions=("seen_home",),
        )
    ]


def _record() -> CompositeRecord:
    return CompositeRecord(
        composite_path="game/composites/static/bg/home__abc123.png",
        name="home",
        kind="layeredimage",
        status="static",
        definition=DefinitionLocation(script="game/script.rpy", line=1),
        usage_locations=(),
        ordered_layers=(),
        dimensions=(64, 32),
        dynamic_conditions=(),
        resolved_conditions=(),
        family="bg",
        content_hash="a" * 64,
        condition_state={"seen_home": True},
    )


def test_record_key_includes_native_content_hash() -> None:
    first = _record_key(
        "layeredimage",
        "home",
        _layers(),
        content_hash="a" * 64,
        condition_state={"seen_home": True},
    )
    second = _record_key(
        "layeredimage",
        "home",
        _layers(),
        content_hash="b" * 64,
        condition_state={"seen_home": True},
    )

    assert first != second


def test_record_key_includes_native_condition_state() -> None:
    first = _record_key(
        "layeredimage",
        "home",
        _layers(),
        content_hash="a" * 64,
        condition_state={"seen_home": True},
    )
    second = _record_key(
        "layeredimage",
        "home",
        _layers(),
        content_hash="a" * 64,
        condition_state={"seen_home": False},
    )

    assert first != second


def test_native_candidate_key_uses_hash_and_condition_state() -> None:
    candidate = {
        "kind": "layeredimage",
        "name": "home",
        "content_hash": "a" * 64,
        "condition_assignments": {"seen_home": True},
    }

    key = _native_record_key(candidate, _layers())

    assert key == _record_key(
        "layeredimage",
        "home",
        _layers(),
        content_hash="a" * 64,
        condition_state={"seen_home": True},
    )


def test_composite_record_round_trip_preserves_native_identity_state() -> None:
    record = _record()

    payload = record.to_dict()
    restored = CompositeRecord.from_dict(payload)

    assert payload["content_hash"] == "a" * 64
    assert payload["condition_state"] == {"seen_home": True}
    assert restored.content_hash == record.content_hash
    assert restored.condition_state == record.condition_state


def test_composite_manifest_round_trip_preserves_cfg_truncation() -> None:
    manifest = CompositeManifest(
        composites=(),
        dynamic_states=[],
        image_declarations=[],
        dynamic_declarations=[],
        unresolved=("condition evaluation failed",),
        summary={"cfg_truncated": True},
        cfg_processed=42,
        cfg_truncated=True,
    )

    payload = manifest.to_dict()
    restored = CompositeManifest.from_dict(payload)

    assert payload["cfg_processed"] == 42
    assert payload["cfg_truncated"] is True
    assert restored.cfg_processed == 42
    assert restored.cfg_truncated is True


def test_native_unresolved_condition_errors_are_preserved() -> None:
    candidates = [
        {"unresolved_errors": ["condition 'seen_home' failed: NameError"]},
        {"unresolved_errors": ["condition 'seen_home' failed: NameError"]},
        {"unresolved_errors": ["condition 'chapter' failed: TypeError"]},
    ]

    assert _native_unresolved_errors(candidates) == (
        "condition 'chapter' failed: TypeError",
        "condition 'seen_home' failed: NameError",
    )


def test_native_render_failures_are_preserved_as_unresolved() -> None:
    candidates = [
        {
            "error": "render failed: renderer crashed",
            "unresolved_errors": [],
        }
    ]

    assert _native_unresolved_errors(candidates) == (
        "render failed: renderer crashed",
    )


def test_native_walk_state_preserves_cfg_truncation() -> None:
    result = {
        "batch": {
            "walk": {
                "processed": 42,
                "truncated": True,
                "roots": 3,
            }
        }
    }

    assert _native_walk_state(result) == {
        "processed": 42,
        "truncated": True,
        "roots": 3,
    }


def test_injected_runtime_reports_condition_evaluation_errors() -> None:
    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "converter"
        / "compose"
        / "native"
        / "runtime"
        / "composite_export.rpy"
    )
    source = runtime_path.read_text(encoding="utf-8")

    assert "except Exception as exc:" in source
    assert '"unresolved_errors"' in source
    assert "return False, error" in source
    assert 'getattr(exc, "composite_unresolved_errors"' in source


def test_injected_runtime_empty_render_preserves_diagnostic_tuple_shape() -> None:
    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "converter"
        / "compose"
        / "native"
        / "runtime"
        / "composite_export.rpy"
    )
    source = runtime_path.read_text(encoding="utf-8")

    assert (
        "return None, 0, 0, None, layers, sources, unresolved_errors"
        in source
    )


def test_injected_runtime_collects_diagnostics_before_empty_render_return() -> None:
    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "converter"
        / "compose"
        / "native"
        / "runtime"
        / "composite_export.rpy"
    )
    source = runtime_path.read_text(encoding="utf-8")

    diagnostics_index = source.index(
        "layers, unresolved_errors = _selected_layers"
    )
    empty_return_index = source.index("if width <= 0 or height <= 0")
    assert diagnostics_index < empty_return_index

"""Record emission helpers for the compose pass.

Public API
----------

    from converter.compose.records import _CompositeEmitter, _emit_record
"""

from __future__ import annotations

from .emitter import _CompositeEmitter
from .record import (
    _build_base_record,
    _determine_family_and_path,
    _emit_record,
    _enrich_char_record,
    _enrich_scene_record,
    _loc_to_usage,
    _normalize_layers,
    _record_key,
    _render_or_reuse_png,
    _resolve_speaker_name,
    _restore_layer_order,
)

__all__ = [
    "_CompositeEmitter",
    "_build_base_record",
    "_determine_family_and_path",
    "_emit_record",
    "_enrich_char_record",
    "_enrich_scene_record",
    "_loc_to_usage",
    "_normalize_layers",
    "_record_key",
    "_render_or_reuse_png",
    "_resolve_speaker_name",
    "_restore_layer_order",
]

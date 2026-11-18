"""Composite emission and deduplication during the compose pass.

Encapsulates the state previously held in a closure inside
:meth:`converter.compose.compose_pass.orchestrate.Composer.compose` so it can be
instantiated and unit-tested independently.
"""

from __future__ import annotations

from tqdm.rich import tqdm  # type: ignore[import-untyped]

from ..association import CharacterAssociator
from ..compositing import Compositor
from ..config import GameConfig
from ..models import CompositeRecord, Layer
from ..parsing import ScriptBlock, ScriptIndex
from ..resolution import Resolver
from .record import _emit_record, _loc_to_usage, _normalize_layers, _record_key


class _CompositeEmitter:
    """Callable that handles composite dedup and emission during the compose pass.

    Encapsulates the state previously held in a closure inside
    :meth:`Composer.compose` so it can be instantiated and unit-tested
    independently.

    Attributes:
        records: All emitted :class:`CompositeRecord`s (in insertion order).
        records_by_key: Dedup key -> record, for O(1) duplicate detection.
        key_to_index: Dedup key -> position in ``records``, for O(1) replacement.
        use_count: Number of unique composites emitted.
        usages_processed: Total number of usage statements seen (incl. dups).
    """

    def __init__(
        self,
        config: GameConfig,
        resolver: Resolver,
        compositor: Compositor,
        associator: CharacterAssociator,
        index: ScriptIndex,
        bar: tqdm,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.compositor = compositor
        self.associator = associator
        self.index = index
        self.bar = bar
        self.dry_run = dry_run

        self.records: list[CompositeRecord] = []
        self.records_by_key: dict[tuple[str, str, str], CompositeRecord] = {}
        self.key_to_index: dict[tuple[str, str, str], int] = {}
        self.use_count = 0
        self.usages_processed = 0

    def __call__(
        self,
        name: str,
        kind: str,
        layers: list[Layer],
        locations: list[dict],
        block: ScriptBlock,
    ) -> None:
        """Process one usage statement: dedup, emit, and update the progress bar."""
        self.usages_processed += 1
        normalized = _normalize_layers(layers, self.config)
        if not normalized:
            self.bar.update(1)
            return
        key = _record_key(kind, name, normalized)
        digest = key[2]

        if key in self.records_by_key:
            self._merge_duplicate(key, locations)
            self.bar.update(1)
            return

        record = _emit_record(
            name,
            kind,
            normalized,
            locations,
            block,
            self.index,
            self.config,
            self.resolver,
            self.compositor,
            self.associator,
            dry_run=self.dry_run,
            digest=digest,
        )
        if record is None:
            self.bar.update(1)
            return
        self.records_by_key[key] = record
        self.key_to_index[key] = len(self.records)
        self.records.append(record)
        self.use_count += 1
        self.bar.update(1)

    def _merge_duplicate(
        self, key: tuple[str, str, str], locations: list[dict]
    ) -> None:
        """Merge usage locations for a duplicate composite key."""
        existing = self.records_by_key[key]
        new_locs = [
            loc
            for loc in locations
            if (loc["script"], loc["line"])
            not in {(l.script, l.line) for l in existing.usage_locations}
        ]
        if not new_locs:
            return
        existing_keys = {(l.script, l.line) for l in existing.usage_locations}
        merged_list = list(existing.usage_locations)
        for loc in new_locs:
            k = (loc["script"], loc["line"])
            if k not in existing_keys:
                merged_list.append(_loc_to_usage(loc))
                existing_keys.add(k)
        merged = tuple(sorted(merged_list, key=lambda l: (l.script, l.line)))
        existing = existing.replace(usage_locations=merged)
        self.records_by_key[key] = existing
        # O(1) replacement via key->index map instead of O(n) scan
        idx = self.key_to_index.get(key)
        if idx is not None:
            self.records[idx] = existing

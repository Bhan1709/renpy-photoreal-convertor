"""Compose pass orchestration.

Responsibility: tie together parsing, resolution, condition evaluation, and
compositing to produce a :class:`~converter.compose.models.CompositeManifest`.

This is the main entry point for the compose pass: :class:`Composer` and its
:meth:`Composer.compose` method.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from ..utility import ProgressBar
from .character_associator import CharacterAssociator
from .compositor import Compositor
from .conditions import eval_cond, select_layers_for_chain, state_at_call
from .config import GameConfig
from .errors import ComposeError
from .models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
    Layer,
    UsageLocation,
)
from .parser import blocks_from, files, find_nearby_dialog, parse_image_declarations, parse_layered, parse_screen_stack
from .resolver import Resolver
from .parser import ScriptBlock  # type: ignore[F401]  (used in annotations)


_SHOW_RE = re.compile(r"^\s*(?:show|scene)\s+(?!screen\b)([\w.]+)(?:\s+([^\n#]+))?")
_DECLARATION_USE_RE = re.compile(r"^\s*(?:show|scene)\s+([\w.]+)")


class Composer:
    """Runs the full compose pass for a :class:`GameConfig`."""

    def __init__(self, config: GameConfig, device: str = "auto") -> None:
        self.config = config
        self.device = device
        self.resolver = Resolver(config)
        self.compositor = Compositor(config, device=device)
        self.associator = CharacterAssociator(config)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def compose(self, dry_run: bool = False, clean_output: bool = False) -> CompositeManifest:
        """Run the compose pass and return a :class:`CompositeManifest`.

        Args:
            dry_run: If True, do not write PNGs or the manifest.
            clean_output: Accepted for API parity; actual cleanup is handled
                by the caller/CLI before this method is invoked.
        """
        script_files = files(self.config)
        all_blocks = blocks_from(script_files, self.config)
        defaults = _collect_defaults(script_files, self.config)

        # Index layeredimage and screen definitions.
        definitions: dict[str, tuple[ScriptBlock, list[Layer]]] = {}
        screens: dict[str, ScriptBlock] = {}
        for b in all_blocks:
            if b.kind == "layeredimage" and b.name not in definitions:
                definitions[b.name] = (b, parse_layered(b, self.config))
            elif b.kind == "screen" and b.name not in screens:
                screens[b.name] = b

        # Index image declarations.
        image_declarations = parse_image_declarations(script_files, self.config)

        records: list[CompositeRecord] = []
        records_by_key: dict[tuple[str, str, str], CompositeRecord] = {}
        use_count = 0

        def emit_wrapper(
            name: str, kind: str, layers: list[Layer], locations: list[dict], block: ScriptBlock,
        ) -> None:
            nonlocal use_count
            record, key = _emit_record(
                name, kind, layers, locations, block, self.config, self.resolver,
                self.compositor, self.associator, dry_run=dry_run, device=self.device,
            )
            if record is None:
                return
            assert key is not None
            if key in records_by_key:
                existing = records_by_key[key]
                new_locs = [
                    loc for loc in locations
                    if (loc["script"], loc["line"]) not in
                    {(l.script, l.line) for l in existing.usage_locations}
                ]
                if new_locs:
                    existing_keys = {(l.script, l.line) for l in existing.usage_locations}
                    merged_list = list(existing.usage_locations)
                    for loc in new_locs:
                        k = (loc["script"], loc["line"])
                        if k not in existing_keys:
                            merged_list.append(_loc_to_usage(loc))
                            existing_keys.add(k)
                    merged = tuple(sorted(merged_list, key=lambda l: (l.script, l.line)))
                    existing = existing.replace(usage_locations=merged)
                    existing = existing.replace(usage_locations=merged)
                    records_by_key[key] = existing
                    # Replace in records list by finding position of old record with same key
                    for i, r in enumerate(records):
                        if _record_key(r) == key:
                            records[i] = existing
                            break
                if existing.family == "xxx":
                    chars = self.associator.associate(
                        list(existing.ordered_layers), name, block,
                        [{"script": l.script, "line": l.line, "statement": l.statement} for l in existing.usage_locations],
                    )
                    existing = existing.replace(
                        characters=tuple(chars),
                        character_association_status="associated" if chars else "missing",
                    )
                    records_by_key[key] = existing
                    for i, r in enumerate(records):
                        if _record_key(r) == key:
                            records[i] = existing
                            break
                return
            records_by_key[key] = record
            records.append(record)
            use_count += 1

        # --- Pass 1: layeredimage show/scene statements ---
        _process_layeredimage_uses(
            script_files, definitions, defaults, emit_wrapper, self.config
        )

        # --- Pass 2: ordinary image declarations ---
        dynamic_declarations: list[dict] = []
        _process_image_declarations(
            script_files, image_declarations, emit_wrapper, dynamic_declarations, self.config
        )

        # --- Pass 3: screen stacks ---
        _process_screen_stacks(
            script_files, screens, definitions, defaults, emit_wrapper, self.config
        )

        # --- Build manifest ---
        previous_dynamic_states = _load_previous_dynamic_states(self.config)
        declaration_records = [
            {
                "name": declaration.name,
                "script": declaration.script.relative_to(self.config.game_root.parent).as_posix(),
                "line": declaration.line,
                "expression": declaration.expression,
                "source_candidates": list(declaration.sources),
                "status": (
                    "dynamic_review"
                    if "ConditionSwitch" in declaration.expression
                    or "LiveComposite" in declaration.expression
                    else "declaration_candidate"
                ),
            }
            for declaration in sorted(image_declarations.values(), key=lambda d: d.name)
        ]

        return CompositeManifest(
            composites=tuple(records),
            dynamic_states=previous_dynamic_states,
            image_declarations=declaration_records,
            dynamic_declarations=dynamic_declarations,
            unresolved=tuple(),
            summary={
                "scripts_scanned": len(script_files),
                "composites": len(records),
                "uses_processed": use_count,
                "image_declarations_indexed": len(declaration_records),
                "dynamic_declarations": len(dynamic_declarations),
            },
        )


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

def _loc_to_usage(loc: dict) -> UsageLocation:
    return UsageLocation(
        script=loc["script"], line=loc["line"], statement=loc["statement"],
        state_snapshot=loc.get("state_snapshot"),
    )


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")


def _collect_defaults(script_files: list[Path], config: GameConfig) -> dict[str, object]:
    defaults: dict[str, object] = dict(config.game_defaults)
    for path in script_files:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            dm = re.match(
                r"^\s*default\s+([A-Za-z_]\w*)\s*=\s*(True|False|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")",
                raw.split("#", 1)[0].strip(),
            )
            if dm and dm.group(1) not in defaults:
                key, value = dm.groups()
                parsed = _parse_literal(value)
                if parsed is not None:
                    defaults[key] = parsed
    return defaults


def _parse_literal(value: str) -> bool | int | float | str | None:
    value = value.strip()
    if value == "True":
        return True
    if value == "False":
        return False
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        return value[1:-1]
    return None


def _load_previous_dynamic_states(config: GameConfig) -> list[dict]:
    previous_manifest_path = config.resolved_paths()["manifest_path"]
    if previous_manifest_path.is_file():
        try:
            return json.loads(previous_manifest_path.read_text(encoding="utf-8")).get("dynamic_states", [])
        except (OSError, json.JSONDecodeError):
            return []
    return []


# ---------------------------------------------------------------------- #
# Pass 1: layeredimage show/scene statements
# ---------------------------------------------------------------------- #

def _process_layeredimage_uses(
    script_files: list[Path],
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
) -> None:
    total_lines = sum(
        len(f.read_text(encoding="utf-8-sig", errors="replace").splitlines())
        for f in script_files
    )
    scanned_lines = 0
    last_percent = -1
    scan_bar = ProgressBar(total=total_lines, width=28, label="indexing script uses")
    for f in script_files:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        for no, line in enumerate(text.splitlines(), 1):
            m = _SHOW_RE.search(line.split("#", 1)[0].strip())
            if not m or m.group(1) not in definitions:
                continue
            tag, tail = m.group(1), (m.group(2) or "")
            block, candidates = definitions[tag]
            _emit_layeredimage_uses(
                tag, tail, block, candidates, f, no, defaults, emit_wrapper, config
            )
        scanned_lines += len(text.splitlines())
        pct = int(scanned_lines * 100 / max(total_lines, 1))
        if pct != last_percent:
            scan_bar.update(scanned_lines)
            last_percent = pct
    scan_bar.finish()


def _emit_layeredimage_uses(
    tag: str, tail: str, block: ScriptBlock, candidates: list[Layer],
    f: Path, no: int, defaults: dict[str, object], emit_wrapper, config: GameConfig,
) -> None:
    attr_tokens = set(re.findall(r"\b[a-zA-Z][\w]*\b", tail))
    valid_attrs = {x.attribute for x in candidates if x.attribute}
    attrs = attr_tokens & valid_attrs
    groups = {x.group for x in candidates if x.group}
    chosen_attrs = set(attrs)
    for group in groups:
        group_candidates = [x for x in candidates if x.group == group]
        explicit = {x.attribute for x in group_candidates if x.attribute in attrs}
        defaults_for_group = {x.attribute for x in group_candidates if x.default_attribute}
        chosen_attrs.update(explicit or defaults_for_group)
    selected = [
        x for x in candidates if not x.attribute or x.attribute in chosen_attrs
    ]
    if not selected:
        return

    call_state = state_at_call(f, no, defaults, config)
    resolved: list[Layer] = []
    unresolved_conditions: list[str] = []

    chained: dict[str, list[Layer]] = defaultdict(list)
    unchained: list[Layer] = []
    for layer in selected:
        cid = layer.chain_id
        if cid is not None:
            chained[cid].append(layer)
        else:
            unchained.append(layer)

    for layer in unchained:
        result = True
        layer_unresolved: list[str] = []
        for condition in layer.conditions:
            outcome = eval_cond(condition, call_state.values)
            if outcome is None:  # UNKNOWN
                layer_unresolved.append(condition)
                result = None  # type: ignore[assignment]
                break
            if outcome is False:
                result = False
                break
        if result is True:
            resolved.append(layer)
        elif result is None:
            layer = layer.with_unresolved(tuple(layer_unresolved))
            resolved.append(layer)
            unresolved_conditions.extend(layer_unresolved)

    for cid, chain_layers in chained.items():
        chain_selected, chain_unresolved = select_layers_for_chain(chain_layers, call_state)
        unresolved_conditions.extend(chain_unresolved)
        resolved.extend(chain_selected)

    locations = [
        {
            "script": f.relative_to(config.game_root.parent).as_posix(),
            "line": no,
            "statement": _line_statement(f, no),
        }
    ]
    if resolved:
        emit_wrapper(tag, "layeredimage", resolved, locations, block)


def _line_statement(f: Path, line_no: int) -> str:
    try:
        lines = f.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if 1 <= line_no <= len(lines):
            return lines[line_no - 1].split("#", 1)[0].strip()
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------- #
# Pass 2: image declarations
# ---------------------------------------------------------------------- #

class _DeclBlock:
    """Minimal stand-in for a ScriptBlock for image declarations."""

    def __init__(self, path: Path, line: int) -> None:
        self.path = path
        self.line = line


def _process_image_declarations(
    script_files: list[Path],
    image_declarations,
    emit_wrapper,
    dynamic_declarations: list[dict],
    config: GameConfig,
) -> None:
    path_regex = config.path_regex()

    for f in script_files:
        for no, raw in enumerate(f.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            use = _DECLARATION_USE_RE.search(raw.split("#", 1)[0].strip())
            if not use or use.group(1) not in image_declarations:
                continue
            name = use.group(1)
            decl = image_declarations[name]
            expression = decl.expression
            loc = {
                "script": f.relative_to(config.game_root.parent).as_posix(),
                "line": no,
                "statement": raw.split("#", 1)[0].strip(),
            }
            dynamic = any(
                token in expression
                for token in ("ConditionSwitch", "LiveComposite", "Function(", "DynamicDisplayable")
            )
            paths = decl.sources
            if dynamic:
                dynamic_declarations.append({
                    "name": name,
                    "declaration": {
                        "script": decl.script.relative_to(config.game_root.parent).as_posix(),
                        "line": decl.line,
                    },
                    "usage_locations": [loc],
                    "expression": expression,
                    "source_candidates": list(paths),
                    "status": "dynamic_review",
                    "reason": "runtime-dependent image declaration",
                })
                continue
            if not paths:
                continue
            layers = [
                Layer(
                    source=p, line=decl.line, role="image_declaration_layer",
                    conditions=(), declaration_expression=expression,
                )
                for p in paths
            ]
            comp = re.search(r"Composite\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
            if comp:
                canvas_size = (int(comp.group(1)), int(comp.group(2)))
                layers = [l.replace(canvas_size=canvas_size) for l in layers]
            position_pairs = list(
                re.finditer(
                    r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*,\s*['\"]("
                    + path_regex.pattern[1:-1] + r")['\"]",
                    expression,
                )
            )
            for index, match in enumerate(position_pairs):
                if index < len(layers):
                    layers[index] = layers[index].replace(
                        offset=(int(match.group(1)), int(match.group(2)))
                    )
            emit_wrapper(name, "image_declaration", layers, [loc], _DeclBlock(decl.script, decl.line))


# ---------------------------------------------------------------------- #
# Pass 3: screen stacks
# ---------------------------------------------------------------------- #

def _process_screen_stacks(
    script_files: list[Path],
    screens: dict[str, ScriptBlock],
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
) -> None:
    screen_stack_cache = {
        name: parse_screen_stack(block, definitions, config)
        for name, block in screens.items()
    }
    screen_bar = ProgressBar(
        total=len(script_files), width=28, label="resolving screen stacks"
    )
    for f in script_files:
        lines = f.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        for no, raw in enumerate(lines, 1):
            sm = re.search(r"^\s*show\s+screen\s+([\w.]+)", raw.split("#", 1)[0].strip())
            if not sm or sm.group(1) not in screens:
                continue
            name = sm.group(1)
            block = screens[sm.group(1)]
            stack = list(screen_stack_cache[name])  # shallow copy
            if stack:
                call_state = state_at_call(f, no, defaults, config)
                selected: list[Layer] = []

                chained: dict[str, list[Layer]] = defaultdict(list)
                unchained: list[Layer] = []
                for layer in stack:
                    cid = layer.chain_id
                    if cid is not None:
                        chained[cid].append(layer)
                    else:
                        unchained.append(layer)

                for layer in unchained:
                    outcome = True
                    layer_unresolved: list[str] = []
                    for condition in layer.conditions:
                        value = eval_cond(condition, call_state.values)
                        if value is None:  # UNKNOWN
                            layer_unresolved.append(condition)
                            outcome = None  # type: ignore[assignment]
                            break
                        if value is False:
                            outcome = False
                            break
                    if outcome is True:
                        selected.append(layer)
                    elif outcome is None:
                        layer = layer.with_unresolved(tuple(layer_unresolved))
                        selected.append(layer)

                for cid, chain_layers in chained.items():
                    chain_selected, _ = select_layers_for_chain(chain_layers, call_state)
                    selected.extend(chain_selected)

                if selected:
                    relevant_names = {
                        name
                        for layer in stack
                        for condition in layer.conditions
                        for name in re.findall(r"\b[A-Za-z_]\w*\b", condition)
                        if name not in {"True", "False", "and", "or", "not"}
                    }
                    state_snapshot = {
                        key: call_state.values[key]
                        for key in sorted(relevant_names)
                        if key in call_state.values
                    }
                    emit_wrapper(
                        name,
                        "screen",
                        selected,
                        [
                            {
                                "script": f.relative_to(config.game_root.parent).as_posix(),
                                "line": no,
                                "statement": raw.split("#", 1)[0].strip(),
                                "state_snapshot": state_snapshot,
                            }
                        ],
                        block,
                    )
        screen_bar.update(1)  # type: ignore[arg-type]
    screen_bar.finish()


# ---------------------------------------------------------------------- #
# Record emission (shared by all passes)
# ---------------------------------------------------------------------- #

def _emit_record(
    name: str, kind: str, layers: list[Layer], locations: list[dict], block: ScriptBlock,
    config: GameConfig, resolver: Resolver, compositor: Compositor,
    associator: CharacterAssociator, *,
    dry_run: bool = False, device: str = "auto",
) -> tuple[CompositeRecord | None, tuple[str, str, str] | None]:
    """Register a composite record (and optionally render its PNG).

    Returns ``(record, key)`` or ``(None, None)`` if the stack could not be
    normalized to a configured asset folder.
    """
    dynamic = any(x.unresolved_conditions for x in layers)

    prefixes = tuple(f"{n}/" for n in config.categories.values())
    normalized: list[Layer] = [
        Layer(
            source=x.source, line=x.line, group=x.group, conditions=x.conditions,
            attribute=x.attribute, default_attribute=x.default_attribute,
            offset=x.offset, offset_rel=x.offset_rel, chain_id=x.chain_id,
            resolved_path=x.resolved_path, resolution_method=x.resolution_method,
            warning=x.warning, unresolved_conditions=x.unresolved_conditions,
            via_image_tag=x.via_image_tag, role=x.role, canvas_size=x.canvas_size,
            declaration_expression=x.declaration_expression,
        )
        for x in layers
        if any(x.source.lower().startswith(p) for p in prefixes)
    ]
    if not normalized:
        return None, None

    semantic = json.dumps(
        [kind, name, [(x.source, list(x.conditions), x.attribute) for x in normalized]],
        sort_keys=True,
    )
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    key = (kind, name, digest)

    folder = next(
        (x.source.split("/")[:-1] for x in normalized if "/" in x.source), [kind]
    )
    family = config.category_for_folder(folder[0]) if folder and folder[0] else kind
    if family is None:
        family = folder[0] if folder else kind
    sub = Path(*folder[1:]) if len(folder) > 1 else Path()
    status = "dynamic_review" if dynamic else "static"
    paths = config.resolved_paths()
    target = paths["output_dir"] / status / family / sub / f"{name}__{digest}.png"
    dimensions: tuple[int, int] | None = None

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        image = compositor.composite(normalized, canvas_size=None)
        if image is None:
            return None, None
        dimensions = (int(image.size[0]), int(image.size[1]))
        image.save(target)

    family2 = ""
    for x in normalized:
        if "/" in x.source:
            f2 = config.category_for_folder(x.source.split("/", 1)[0].lower())
            if f2 is None:
                f2 = x.source.split("/", 1)[0].lower()
            family2 = f2
            break

    scene_folder = config.folder_for("scene").lower()
    is_xxx = any(
        x.source.split("/", 1)[0].lower() == scene_folder
        for x in normalized if "/" in x.source
    )

    usage_locations = tuple(
        UsageLocation(
            script=loc["script"], line=loc["line"], statement=loc["statement"],
            state_snapshot=loc.get("state_snapshot"),
        )
        for loc in locations
    )

    record = CompositeRecord(
        composite_path=target.relative_to(config.game_root.parent).as_posix(),
        name=name,
        kind=kind,
        status=status,
        definition=DefinitionLocation(
            script=block.path.relative_to(config.game_root.parent).as_posix(),
            line=block.line,
        ),
        usage_locations=usage_locations,
        ordered_layers=tuple(normalized),
        dimensions=dimensions,
        dynamic_conditions=tuple(
            sorted({c for x in layers for c in x.unresolved_conditions})
        ),
        resolved_conditions=tuple(
            sorted({c for x in layers for c in x.conditions})
        ),
        family="xxx" if is_xxx else (family2 or kind),
    )

    if is_xxx:
        char_locs = [{"script": l.script, "line": l.line, "statement": l.statement} for l in usage_locations]
        chars = associator.associate(list(normalized), name, block, char_locs)
        record = record.replace(
            characters=tuple(chars),
            character_association_status="associated" if chars else "missing",
            family="xxx",
        )
    elif family2 == "char":
        record = record.replace(family="char")
        char_folder = None
        for x in normalized:
            parts = x.source.split("/", 1)
            if len(parts) > 1 and parts[0].lower() == "chars":
                char_folder = parts[1].split("/", 1)[0]
                break
        if char_folder:
            record = record.replace(character_name=associator.display_name(char_folder))
    elif family2 == "scene":
        record = record.replace(family="scene")
        dialog = find_nearby_dialog(block.path, block.line)
        if dialog:
            record = record.replace(
                dialog_context=tuple(
                    {"speaker": d.speaker, "text": d.text, "line": d.line}
                    for d in dialog
                )
            )

    return record, key


def _record_key(record: CompositeRecord) -> tuple[str, str, str]:
    """Compute the dedup key for a CompositeRecord."""
    semantic = json.dumps(
        [record.kind, record.name, [(l.source, list(l.conditions), l.attribute) for l in record.ordered_layers]],
        sort_keys=True,
    )
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    return (record.kind, record.name, digest)

"""Build reviewable composites for script-used Ren'Py component stacks.

This intentionally performs static analysis only. It resolves literal layeredimage
attributes and literal screen conditions where possible, and writes provenance for
every rendered stack. Dynamic stacks are previews, not exhaustive runtime states.

The script is parameterized by a :class:`~converter.compose.config.GameConfig`
which describes how a single Ren'Py game maps its four canonical component
categories (bg, char, scene, gui) onto on-disk asset folders.  Nothing about a
specific game is hard-coded here; all folder names, aliases, and paths come from
config.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from PIL import Image

from ..utility import ProgressBar
from .config import (
    CANONICAL_CATEGORIES,
    GameConfig,
    default_config,
    load_config,
)

# --------------------------------------------------------------------------- #
# Module-level configuration
# --------------------------------------------------------------------------- #
# Bound once at program start (by ``set_config`` / ``main``).  All path- and
# game-specific behaviour below reads from this config rather than hard-coded
# constants, so the same code works for any Ren'Py game.
_config: GameConfig | None = None


def set_config(config: GameConfig) -> None:
    """Bind the module to a :class:`GameConfig`.  Call once at startup."""
    global _config
    _config = config


def get_config() -> GameConfig:
    """Return the bound :class:`GameConfig`, raising if none is set."""
    if _config is None:
        raise RuntimeError(
            "compose config not initialized; call set_config() before using this module"
        )
    return _config


# --------------------------------------------------------------------------- #
# Regexes built from config (replaces the hard-coded PATH_RE alternation).
# --------------------------------------------------------------------------- #
def path_regex() -> re.Pattern[str]:
    """Regex matching asset paths under any configured category folder."""
    return get_config().path_regex()


BLOCK_RE = re.compile(r"^(\s*)(layeredimage|screen)\s+([\w.]+)(?:\([^\n]*\))?\s*:\s*$")
IMAGE_RE = re.compile(r"^(\s*)image\s+([\w.]+)\s*=\s*(.*)$")
UNKNOWN = object()
_LINES_CACHE = {}
_STATE_CACHE = {}

# --------------------------------------------------------------------------- #
# Character / identity inference
# --------------------------------------------------------------------------- #

_CHARACTER_ALIASES = None
_XXX_CHARACTER_TAGS = None


def character_aliases(config: GameConfig | None = None):
    """Map names, Ren'Py character variables, image tags, and asset dirs to identities.

    Scans the configured character asset folder and the live ``Character()``
    declarations to build an alias graph.  No game-specific names are hard-coded.
    """
    config = config or get_config()
    global _CHARACTER_ALIASES, _XXX_CHARACTER_TAGS
    if _CHARACTER_ALIASES is not None:
        return _CHARACTER_ALIASES

    chars_dir = config.char_dir()
    folders = {p.name.lower() for p in chars_dir.iterdir() if p.is_dir()} if chars_dir.is_dir() else set()
    aliases: defaultdict[str, set[str]] = defaultdict(set)
    for folder in folders:
        aliases[re.sub(r"[^a-z0-9]", "", folder)].add(folder)

    # Character() declarations are authoritative for display names and speaker vars.
    define_re = re.compile(
        r"""^\s*define\s+([A-Za-z_]\w*)\s*=\s*Character\(\s*(['"])(.*?)\2(.*?)\)\s*$"""
    )
    for script in files(config):
        if "archive" in script.parts or "cache" in script.parts:
            continue
        for raw in script.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = define_re.match(clean(raw))
            if not match:
                continue
            speaker, _, display_name, tail = match.groups()
            display = re.sub(r"[^a-z0-9]", "", display_name.lower())
            image_match = re.search(r"""image\s*=\s*(['"])(.*?)\1""", tail)
            image_tag = image_match.group(2).lower() if image_match else ""
            candidates = set()
            if display in folders:
                candidates.add(display)
            for word in re.findall(r"[A-Za-z]+", display_name.lower()):
                if word in folders:
                    candidates.add(word)
            if image_tag in folders:
                candidates.add(image_tag)
            if not candidates:
                # Keep named speakers even when their sprite tag is shared/absent.
                canonical = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")
                if canonical:
                    candidates.add(canonical)
            for canonical in candidates:
                aliases[display].add(canonical)
                aliases[re.sub(r"[^a-z0-9]", "", speaker.lower())].add(canonical)
                if image_tag:
                    aliases[re.sub(r"[^a-z0-9]", "", image_tag)].add(canonical)

    _XXX_CHARACTER_TAGS = folders
    _CHARACTER_ALIASES = aliases
    return aliases


def associate_xxx_characters(layers, image_name, config=None, block=None, usage_locations=None):
    """Infer involved characters from XXX asset paths and their Ren'Py definitions."""
    config = config or get_config()
    aliases = character_aliases(config)
    evidence = defaultdict(set)

    def add_alias(alias, source, *, allow_short=False):
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if len(key) < 3 and not allow_short:
            return
        for canonical in aliases.get(key, ()):
            if len(key) < 3 and canonical not in _XXX_CHARACTER_TAGS:
                continue
            if canonical in config.non_character_folders:
                continue
            evidence[canonical].add(source)

    def add_identifier_tokens(value, source):
        # Ren'Py identifiers and asset files commonly concatenate image tags:
        # dndXS4LoBrow -> ["dnd", "XS", "4", "Lo", "Brow"].
        for token in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", value):
            if len(token) >= 2:
                add_alias(token, source, allow_short=True)

    for layer in layers:
        source = (layer.get("source") or "").replace("\\", "/")
        for part in source.split("/")[1:]:
            stem = Path(part).stem
            add_alias(stem, f"asset:{source}", allow_short=True)
            add_identifier_tokens(stem, f"asset:{source}")

    add_alias(image_name, f"image:{image_name}")
    add_identifier_tokens(image_name, f"image:{image_name}")

    # Infer identities from the declaring screen/layeredimage and its nearby
    # section heading/comments, where CG assets are often named only by quest ID.
    if block:
        for _, raw in block.get("body", []):
            text = raw.split("#", 1)[0]
            identifiers = re.findall(r"[A-Za-z_]\w*", text)
            for token in identifiers:
                evidence_source = (
                    f"script_identifier:{block['path'].relative_to(get_config().game_root).as_posix()}:{block['line']}"
                )
                add_alias(token, evidence_source)
                add_identifier_tokens(token, evidence_source)
        try:
            lines = block["path"].read_text(encoding="utf-8-sig", errors="replace").splitlines()
            before = lines[max(0, block["line"] - 80):block["line"] - 1]
            base_line = max(1, block["line"] - 79)
            headings = [(i, raw) for i, raw in enumerate(before, base_line)
                        if re.match(r"^\s*#{3,}\s*\S", raw)]
            if headings:
                index, raw = headings[-1]
                comment = raw.split("#", 1)[-1].lower()
                for alias, canonicals in aliases.items():
                    if len(alias) < 3 or not re.search(
                            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", comment):
                        continue
                    for canonical in canonicals:
                        if canonical not in config.non_character_folders:
                            evidence[canonical].add(
                                f"script_comment:{block['path'].relative_to(get_config().game_root).as_posix()}:{index}"
                            )
        except OSError:
            pass

    for location in usage_locations or []:
        statement = location.get("statement", "")
        usage_source = f"script_usage:{location.get('script')}:{location.get('line')}"
        for token in re.findall(r"[A-Za-z_]\w*", statement):
            add_identifier_tokens(token, usage_source)
        for alias, canonicals in aliases.items():
            if len(alias) < 3:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", statement.lower()):
                for canonical in canonicals:
                    evidence[canonical].add(
                        f"script_usage:{location.get('script')}:{location.get('line')}"
                    )

    return [{"character": tag, "evidence": sorted(sources)}
            for tag, sources in sorted(evidence.items())]


# --------------------------------------------------------------------------- #
# Script discovery
# --------------------------------------------------------------------------- #


def files(config: GameConfig | None = None):
    # Prefer live files over duplicated decompilation/archive copies.
    config = config or get_config()
    return sorted(
        (p for p in config.game_root.rglob("*.rpy")
         if "cache" not in p.parts and p.name != "composite_images.rpy"),
        key=lambda p: ("/archive/" in p.as_posix().lower(), p.as_posix().lower()),
    )


def clean(s):
    return s.split("#", 1)[0].strip()


def blocks_from(paths, config=None):
    out = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            m = BLOCK_RE.match(lines[i])
            if not m:
                i += 1
                continue
            indent, kind, name = len(m.group(1)), m.group(2), m.group(3)
            start = i
            i += 1
            body = []
            while i < len(lines):
                raw = lines[i]
                if raw.strip() and len(raw) - len(raw.lstrip()) <= indent:
                    break
                body.append((i + 1, raw))
                i += 1
            out.append({"kind": kind, "name": name, "path": path, "line": start + 1, "body": body})
    return out


# --------------------------------------------------------------------------- #
# Progress / literal parsing
# --------------------------------------------------------------------------- #


def parse_literal(value):
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
    return UNKNOWN


def parse_layered(block, config=None):
    """Read source paths and nearby literal attribute/group/condition declarations."""
    config = config or get_config()
    records = []
    group = ""
    group_indent = None
    active_attr = None
    active_default = False
    attr_indent = None
    condition_stack = []
    for line_no, raw in block["body"]:
        text = clean(raw)
        if not text:
            continue
        indent = len(raw) - len(raw.lstrip())
        condition_stack = [(level, cond) for level, cond in condition_stack if level < indent]
        gm = re.match(r"group\s+([\w]+)\s*:", text)
        if group_indent is not None and indent <= group_indent and not gm:
            group = ""
            group_indent = None
            active_attr = None
            active_default = False
            attr_indent = None
        elif attr_indent is not None and indent <= attr_indent:
            active_attr = None
            active_default = False
            attr_indent = None
        if gm:
            group = gm.group(1)
            group_indent = indent
            active_attr = None
            active_default = False
            attr_indent = None
            continue
        am = re.match(r"attribute\s+([\w]+)(\s+default)?\s*:", text)
        if am:
            active_attr = am.group(1)
            active_default = bool(am.group(2))
            attr_indent = indent
            continue
        cm = re.match(r"(if|elif|else)\s+(.+?)\s*:", text)
        if cm:
            kind, cond = cm.groups()
            if kind == "else":
                cond = "<else>"
            condition_stack = [(level, value) for level, value in condition_stack if level < indent]
            condition_stack.append((indent, cond))
            continue
        pm = path_regex().search(text)
        if pm:
            offset = None
            offset_rel = None
            xp = re.search(r"\bxpos\s+([\d.]+)\s*", text)
            yp = re.search(r"\bypos\s+([\d.]+)\s*", text)
            if xp and yp:
                offset = [float(xp.group(1)), float(yp.group(1))]
            xa = re.search(r"\bxalign\s+([\d.]+)\s*", text)
            ya = re.search(r"\byalign\s+([\d.]+)\s*", text)
            if xa and ya:
                offset_rel = [float(xa.group(1)), float(ya.group(1))]
            records.append({"source": pm.group(0), "line": line_no, "group": group,
                            "conditions": [c for _, c in condition_stack], "attribute": active_attr,
                            "default_attribute": active_default, "offset": offset,
                            "offset_rel": offset_rel})
    return records


def eval_cond(expr, env):
    expr = expr.strip()
    if expr == "<else>":
        return UNKNOWN
    try:
        # Ren'Py conditions are standard Python comparison/boolean expressions
        return bool(eval(expr, {"__builtins__": {}}, env))
    except (NameError, TypeError, KeyError):
        return UNKNOWN
    except Exception:
        return UNKNOWN


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

# Per-game path alias overrides (e.g. a sprite sheet renamed between versions).
# Populated from config.path_aliases at config-bind time.
_ALIASES: dict[str, str] = {}
_RESET_DEFAULTS_MAP = None


def bind_aliases(config: GameConfig | None = None):
    """Copy config path aliases into the module-level resolution table."""
    config = config or get_config()
    global _ALIASES
    _ALIASES = dict(config.path_aliases)


@lru_cache(maxsize=4096)
def resolve_source(s):
    config = get_config()
    if s in _ALIASES:
        s = _ALIASES[s]
    p = config.archive_dir / Path(s)
    if p.is_file():
        return p
    # Asset folder variants occur in extracted builds; match exact relative path case-insensitively.
    wanted = s.replace("/", "\\").lower()
    suffix_matches = []
    for candidate in config.archive_dir.rglob(Path(s).name):
        try:
            relative = str(candidate.relative_to(config.archive_dir)).replace("/", "\\").lower()
            if relative == wanted:
                return candidate
            if relative.endswith("\\" + wanted):
                suffix_matches.append(candidate)
        except ValueError:
            pass
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return None


@lru_cache(maxsize=256)
def load_rgba(path_string):
    with Image.open(path_string) as source:
        return source.convert("RGBA")


# --------------------------------------------------------------------------- #
# Compositing
# --------------------------------------------------------------------------- #


def composite(layers, config=None):
    config = config or get_config()
    opened, size = [], None
    for candidate in layers:
        if candidate.get("canvas_size"):
            size = tuple(candidate["canvas_size"])
            break
    for layer in layers:
        path = resolve_source(layer["source"])
        if path is None:
            layer["resolved_path"] = None
            continue
        layer["resolved_path"] = path.relative_to(config.game_root.parent).as_posix()
        try:
            archive_rel = path.relative_to(config.archive_dir).as_posix().lower()
        except ValueError:
            archive_rel = ""
        if archive_rel != layer["source"].lower():
            layer["resolution_method"] = "unique_folder_suffix_correlation"
        try:
            im = load_rgba(str(path))
        except Exception:
            continue
        if size is None:
            size = im.size
        opened.append((im, layer))
    if not opened:
        return None
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for im, layer in opened:
        if im.size != size:
            layer["warning"] = f"size {im.size} differs from canvas {size}; top-left composited"
        offset = layer.get("offset")
        if offset is None:
            rel = layer.get("offset_rel")
            if rel is not None:
                offset = [int(rel[0] * size[0]), int(rel[1] * size[1])]
            else:
                offset = [0, 0]
        canvas.alpha_composite(im, (int(offset[0]), int(offset[1])))
    return canvas


def parse_position_from_transforms(transforms):
    """Extract xpos/ypos or xalign/yalign from a Ren'Py add statement's transform text."""
    offset = None
    offset_rel = None
    xp = re.search(r"\bxpos\s+([\d.]+)\s*", transforms)
    yp = re.search(r"\bypos\s+([\d.]+)\s*", transforms)
    if xp and yp:
        offset = [float(xp.group(1)), float(yp.group(1))]
    xa = re.search(r"\bxalign\s+([\d.]+)\s*", transforms)
    ya = re.search(r"\byalign\s+([\d.]+)\s*", transforms)
    if xa and ya:
        offset_rel = [float(xa.group(1)), float(ya.group(1))]
    return offset, offset_rel


def parse_screen_stack(block, definitions, config=None):
    config = config or get_config()
    stack, conditions = [], []
    prefixes = tuple(f"{name}/" for name in config.categories.values())
    for line_no, row in block["body"]:
        text = clean(row)
        if not text:
            continue
        indent = len(row) - len(row.lstrip())
        conditions = [(level, cond) for level, cond in conditions if level < indent]
        cm = re.match(r"(?:if|elif)\s+(.+?)\s*:", text)
        if cm:
            conditions = [(level, cond) for level, cond in conditions if level < indent]
            conditions.append((indent, cm.group(1)))
            continue
        if text == "else:":
            conditions = [(level, cond) for level, cond in conditions if level < indent]
            conditions.append((indent, "<else>"))
            continue
        quoted = re.match(r"add\s+([\"'])(.+?)\1", text)
        if quoted and path_regex().search(quoted.group(2)):
            offset, offset_rel = parse_position_from_transforms(text[quoted.end():].strip())
            stack.append({"source": quoted.group(2), "line": line_no,
                          "conditions": [c for _, c in conditions], "role": "screen_add",
                          "offset": offset, "offset_rel": offset_rel})
        elif quoted and quoted.group(2) in definitions:
            tag = quoted.group(2)
            stack.extend({**layer, "conditions": [c for _, c in conditions] + layer["conditions"], "via_image_tag": tag}
                         for layer in definitions[tag][1])
        elif quoted and quoted.group(2).lower().startswith(prefixes):
            offset, offset_rel = parse_position_from_transforms(text[quoted.end():].strip())
            stack.append({"source": quoted.group(2), "line": line_no,
                          "conditions": [c for _, c in conditions], "role": "screen_add",
                          "offset": offset, "offset_rel": offset_rel})
        else:
            named = re.match(r"add\s+([\w.]+)", text)
            if named and named.group(1) in definitions:
                tag = named.group(1)
                stack.extend({**layer, "conditions": [c for _, c in conditions] + layer["conditions"], "via_image_tag": tag}
                             for layer in definitions[tag][1])
    return stack


# --------------------------------------------------------------------------- #
# State / defaults
# --------------------------------------------------------------------------- #


def get_reset_defaults_map(config=None):
    config = config or get_config()
    global _RESET_DEFAULTS_MAP
    if _RESET_DEFAULTS_MAP is not None:
        return _RESET_DEFAULTS_MAP
    _RESET_DEFAULTS_MAP = {}
    char_file = config.game_root / "characters.rpy"
    if not char_file.exists():
        return _RESET_DEFAULTS_MAP
    current_label = None
    for line in char_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = re.match(r"^\s*label\s+([A-Za-z_]\w*ResetDefault):", clean(line))
        if m:
            current_label = m.group(1)
            _RESET_DEFAULTS_MAP[current_label] = []
            continue
        if current_label:
            if re.match(r"^\s*return\b", clean(line)):
                current_label = None
                continue
            m_assign = re.match(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", clean(line))
            if m_assign:
                _RESET_DEFAULTS_MAP[current_label].append(m_assign.groups())
    return _RESET_DEFAULTS_MAP


def state_at_call(path, line_number, defaults, config=None):
    config = config or get_config()
    key = str(path)
    if key not in _STATE_CACHE:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        labels, assignments = [0], []
        reset_map = get_reset_defaults_map(config)
        for i, raw in enumerate(lines, 1):
            text = clean(raw)
            if re.match(r"^\s*label\s+[\w.]+(?:\([^\n]*\))?\s*:", text):
                labels.append(i)
            m = re.match(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", text)
            if m:
                name, value = m.groups()
                parsed = parse_literal(value)
                if parsed is UNKNOWN and value in defaults:
                    parsed = defaults[value]
                if parsed is not UNKNOWN:
                    assignments.append((i, name, parsed))
            m_call = re.match(r"^\s*call\s+([A-Za-z_]\w*ResetDefault)\b", text)
            if m_call and m_call.group(1) in reset_map:
                for name, value in reset_map[m_call.group(1)]:
                    parsed = parse_literal(value)
                    if parsed is UNKNOWN and value in defaults:
                        parsed = defaults[value]
                    if parsed is not UNKNOWN:
                        assignments.append((i, name, parsed))
        _STATE_CACHE[key] = (labels, assignments)
    labels, assignments = _STATE_CACHE[key]
    label_start = labels[max(0, bisect.bisect_right(labels, line_number - 1) - 1)]
    state = dict(defaults)
    left = bisect.bisect_left(assignments, (label_start, "", None))
    right = bisect.bisect_left(assignments, (line_number, "", None))
    for _, name, value in assignments[left:right]:
        state[name] = value
    return state


# --------------------------------------------------------------------------- #
# Record emission
# --------------------------------------------------------------------------- #


def emit(name, kind, layers, locations, block, config, *, dry_run=False):
    """Register a composite record (and optionally render its PNG).

    Returns ``(record, key)`` or ``(None, None)`` if the stack could not be
    normalized to a configured asset folder.  ``key`` is the dedup key used to
    merge duplicate composite records.
    """
    dynamic = any(x.get("unresolved_conditions") for x in layers)

    # Normalize via config-driven folder prefixes (replaces hard-coded alternation).
    prefixes = tuple(f"{n}/" for n in config.categories.values())
    normalized = [dict(x) for x in layers if any(
        x.get("source", "").lower().startswith(p) for p in prefixes)]
    if not normalized:
        return None, None

    semantic = json.dumps([kind, name, [(x["source"], x.get("conditions", []), x.get("attribute")) for x in normalized]], sort_keys=True)
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    key = (kind, name, digest)

    folder = next((x["source"].split("/")[:-1] for x in normalized if "/" in x["source"]), [kind])
    family = config.category_for_folder(folder[0]) if folder and folder[0] else kind
    if family is None:
        family = folder[0] if folder else kind
    sub = Path(*folder[1:]) if len(folder) > 1 else Path()
    status = "dynamic_review" if dynamic else "static"
    paths = config.resolved_paths()
    target = paths["output_dir"] / status / family / sub / f"{name}__{digest}.png"
    dimensions = None
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        image = composite(normalized, config)
        if image is None:
            return None, None
        dimensions = list(image.size)
        image.save(target)

    family2 = next((config.category_for_folder(x["source"].split("/", 1)[0].lower())
                    for x in normalized if "/" in x["source"]), "")
    if family2 is None:
        family2 = next((x["source"].split("/", 1)[0].lower() for x in normalized if "/" in x["source"]), "")

    record = {
        "composite_path": target.relative_to(config.game_root.parent).as_posix(),
        "name": name,
        "kind": kind,
        "status": status,
        "definition": {"script": block["path"].relative_to(config.game_root.parent).as_posix(), "line": block["line"]},
        "usage_locations": locations,
        "ordered_layers": normalized,
        "dimensions": dimensions,
        "dynamic_conditions": sorted({c for x in layers for c in x.get("unresolved_conditions", [])}),
        "resolved_conditions": sorted({c for x in layers for c in x.get("conditions", [])}),
    }
    if family2 == "xxx":
        record["characters"] = associate_xxx_characters(normalized, name, config, block, locations)
        record["family"] = "xxx"
        record["character_association_status"] = (
            "associated" if record["characters"] else "missing"
        )
    return record, key


# --------------------------------------------------------------------------- #
# Main compose pass
# --------------------------------------------------------------------------- #


def compose(config: GameConfig | None = None, dry_run: bool = False, clean_output: bool = False):
    """Run the full compose pass for ``config`` and return the manifest dict."""
    config = config or get_config()
    bind_aliases(config)
    set_config(config)

    script_files = files(config)
    all_blocks = blocks_from(script_files, config)
    defaults = dict(config.game_defaults)
    for path in script_files:
        for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            dm = re.match(r"^\s*default\s+([A-Za-z_]\w*)\s*=\s*(True|False|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")", clean(raw))
            if dm and dm.group(1) not in defaults:
                key, value = dm.groups()
                defaults[key] = parse_literal(value)

    definitions = {}
    screens = {}
    for b in all_blocks:
        if b["kind"] == "layeredimage" and b["name"] not in definitions:
            definitions[b["name"]] = (b, parse_layered(b, config))
        elif b["kind"] == "screen" and b["name"] not in screens:
            screens[b["name"]] = b

    # Index image declarations as provenance-bearing candidate stacks.
    image_declarations = {}
    for path in script_files:
        source_lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        i = 0
        while i < len(source_lines):
            raw = source_lines[i]
            im = IMAGE_RE.match(clean(raw))
            if im:
                _, name, expression = im.groups()
                declaration_indent = len(raw) - len(raw.lstrip())
                expr_lines = [expression]
                j = i + 1
                while j < len(source_lines):
                    following = source_lines[j]
                    if following.strip() and len(following) - len(following.lstrip()) <= declaration_indent:
                        break
                    expr_lines.append(clean(following))
                    j += 1
                expression = "\n".join(expr_lines)
                sources = [m.group(0) for m in path_regex().finditer(expression)]
                if sources and name not in image_declarations:
                    image_declarations[name] = {
                        "script": path,
                        "line": i + 1,
                        "expression": expression,
                        "sources": sources,
                    }
                i = j
            else:
                i += 1

    records = []
    unresolved = []
    use_count = 0
    records_by_key = {}
    dynamic_declarations = []

    def emit_wrapper(name, kind, layers, locations, block):
        nonlocal use_count
        record, key = emit(name, kind, layers, locations, block, config, dry_run=dry_run)
        if record is None:
            return
        if key in records_by_key:
            existing = records_by_key[key]
            existing["usage_locations"].extend(
                loc for loc in locations if loc not in existing["usage_locations"]
            )
            if existing.get("family") == "xxx":
                existing["characters"] = associate_xxx_characters(
                    existing["ordered_layers"], name, config, block, existing["usage_locations"]
                )
                existing["character_association_status"] = (
                    "associated" if existing["characters"] else "missing"
                )
            return
        records_by_key[key] = record
        records.append(record)
        use_count += 1

    # Layered image show statements.
    show_re = re.compile(r"^\s*(?:show|scene)\s+(?!screen\b)([\w.]+)(?:\s+([^\n#]+))?")
    total_lines = sum(len(f.read_text(encoding="utf-8-sig", errors="replace").splitlines()) for f in script_files)
    scanned_lines = 0
    last_percent = -1
    scan_bar = ProgressBar(total=total_lines, width=28, label="indexing script uses")
    for f in script_files:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        for no, line in enumerate(text.splitlines(), 1):
            m = show_re.search(clean(line))
            if not m or m.group(1) not in definitions:
                continue
            tag, tail = m.group(1), (m.group(2) or "")
            block, candidates = definitions[tag]
            attr_tokens = set(re.findall(r"\b[a-zA-Z][\w]*\b", tail))
            valid_attrs = {x["attribute"] for x in candidates if x.get("attribute")}
            attrs = attr_tokens & valid_attrs
            groups = {x.get("group") for x in candidates if x.get("group")}
            chosen_attrs = set(attrs)
            for group in groups:
                group_candidates = [x for x in candidates if x.get("group") == group]
                explicit = {x.get("attribute") for x in group_candidates if x.get("attribute") in attrs}
                defaults_for_group = {x.get("attribute") for x in group_candidates if x.get("default_attribute")}
                chosen_attrs.update(explicit or defaults_for_group)
            selected = [x.copy() for x in candidates if not x.get("attribute") or x.get("attribute") in chosen_attrs]
            if not selected:
                continue
            call_state = state_at_call(f, no, defaults, config)
            resolved, unresolved_conditions = [], []
            for layer in selected:
                result = True
                for condition in layer.get("conditions", []):
                    outcome = eval_cond(condition, call_state)
                    if outcome is UNKNOWN:
                        unresolved_conditions.append(condition)
                        result = UNKNOWN
                        break
                    if not outcome:
                        result = False
                        break
                if result is True:
                    resolved.append(layer)
            if unresolved_conditions:
                selected_keys = {(x["source"], x["line"], x.get("attribute")) for x in resolved}
                resolved.extend(x for x in selected if not x.get("conditions") and
                                (x["source"], x["line"], x.get("attribute")) not in selected_keys)
                for layer in resolved:
                    if layer.get("conditions"):
                        layer["unresolved_conditions"] = sorted(set(unresolved_conditions))
            locations = [{"script": f.relative_to(config.game_root.parent).as_posix(), "line": no, "statement": clean(line)}]
            if resolved:
                emit_wrapper(tag, "layeredimage", resolved, locations, block)
        scanned_lines += len(text.splitlines())
        pct = int(scanned_lines * 100 / max(total_lines, 1))
        if pct != last_percent:
            scan_bar.update(scanned_lines)
            last_percent = pct
    scan_bar.finish()

    # Resolve ordinary `image` declarations when used by show/scene.
    declaration_use_re = re.compile(r"^\s*(?:show|scene)\s+([\w.]+)")
    for f in script_files:
        for no, raw in enumerate(f.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            use = declaration_use_re.search(clean(raw))
            if not use or use.group(1) not in image_declarations:
                continue
            name = use.group(1)
            decl = image_declarations[name]
            expression = decl["expression"]
            loc = {"script": f.relative_to(config.game_root.parent).as_posix(), "line": no, "statement": clean(raw)}
            dynamic = any(token in expression for token in ("ConditionSwitch", "LiveComposite", "Function(", "DynamicDisplayable"))
            paths = decl["sources"]
            if dynamic:
                dynamic_declarations.append({"name": name, "declaration": {"script": decl["script"].relative_to(config.game_root.parent).as_posix(), "line": decl["line"]},
                    "usage_locations": [loc], "expression": expression, "source_candidates": paths,
                    "status": "dynamic_review", "reason": "runtime-dependent image declaration"})
                continue
            if not paths:
                continue
            layers = [{"source": p, "line": decl["line"], "role": "image_declaration_layer", "conditions": [],
                       "declaration_expression": expression} for p in paths]
            comp = re.search(r"Composite\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
            if comp:
                canvas_size = [int(comp.group(1)), int(comp.group(2))]
                for layer in layers:
                    layer["canvas_size"] = canvas_size
            position_pairs = list(re.finditer(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*,\s*['\"](" + path_regex().pattern[1:-1] + r")['\"]", expression))
            for index, match in enumerate(position_pairs):
                if index < len(layers):
                    layers[index]["offset"] = [int(match.group(1)), int(match.group(2))]
            decl_block = {"path": decl["script"], "line": decl["line"]}
            emit_wrapper(name, "image_declaration", layers, [loc], decl_block)

    # Screen stacks: selected literal add paths and named layeredimage displayables.
    screen_files = 0
    screen_stack_cache = {name: parse_screen_stack(block, definitions, config) for name, block in screens.items()}
    screen_bar = ProgressBar(total=len(script_files), width=28, label="resolving screen stacks")
    for f in script_files:
        lines = f.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        for no, raw in enumerate(lines, 1):
            sm = re.search(r"^\s*show\s+screen\s+([\w.]+)", clean(raw))
            if not sm or sm.group(1) not in screens:
                continue
            name, block = sm.group(1), screens[sm.group(1)]
            stack = copy.deepcopy(screen_stack_cache[name])
            if stack:
                call_state = state_at_call(f, no, defaults, config)
                selected, unknown_conditions = [], []
                for layer in stack:
                    outcome = True
                    for condition in layer.get("conditions", []):
                        value = eval_cond(condition, call_state)
                        if value is UNKNOWN:
                            unknown_conditions.append(condition)
                            outcome = UNKNOWN
                            break
                        if value is False:
                            outcome = False
                            break
                    if outcome is True:
                        selected.append(layer)
                # If no literal call-site state resolves the screen, keep unconditional layers
                # as a marked partial preview; never overlay mutually-exclusive alternatives.
                if unknown_conditions:
                    selected_keys = {(x["source"], x["line"], x.get("attribute")) for x in selected}
                    selected.extend(x for x in stack if not x.get("conditions") and
                                    (x["source"], x["line"], x.get("attribute")) not in selected_keys)
                if selected:
                    relevant_names = {name for layer in stack for condition in layer.get("conditions", [])
                                      for name in re.findall(r"\b[A-Za-z_]\w*\b", condition)
                                      if name not in {"True", "False", "and", "or", "not"}}
                    state_snapshot = {key: call_state[key] for key in sorted(relevant_names) if key in call_state}
                    for layer in selected:
                        # Only conditional layers are unresolved. An unconditional layer is
                        # fully determined even when a sibling condition cannot be evaluated.
                        if unknown_conditions and layer.get("conditions"):
                            layer["unresolved_conditions"] = sorted(set(unknown_conditions))
                    emit_wrapper(name, "screen", selected, [{"script": f.relative_to(config.game_root.parent).as_posix(), "line": no,
                          "statement": clean(raw), "state_snapshot": state_snapshot}], block)
        screen_files += 1
        screen_bar.update(screen_files)
    screen_bar.finish()

    previous_dynamic_states = []
    previous_manifest_path = config.resolved_paths()["manifest_path"]
    if previous_manifest_path.is_file():
        try:
            previous_dynamic_states = json.loads(
                previous_manifest_path.read_text(encoding="utf-8")
            ).get("dynamic_states", [])
        except (OSError, json.JSONDecodeError):
            previous_dynamic_states = []

    declaration_records = []
    for name, declaration in sorted(image_declarations.items()):
        declaration_records.append({"name": name, "script": declaration["script"].relative_to(config.game_root.parent).as_posix(),
                                    "line": declaration["line"], "expression": declaration["expression"],
                                    "source_candidates": declaration["sources"],
                                    "status": "dynamic_review" if "ConditionSwitch" in declaration["expression"] or "LiveComposite" in declaration["expression"] else "declaration_candidate"})
    manifest = {
        "composites": records,
        "dynamic_states": previous_dynamic_states,
        "image_declarations": declaration_records,
        "dynamic_declarations": dynamic_declarations,
        "unresolved": sorted(set(unresolved)),
        "summary": {"scripts_scanned": len(script_files),
                    "composites": len(records), "uses_processed": use_count, "image_declarations_indexed": len(declaration_records),
                    "dynamic_declarations": len(dynamic_declarations)},
    }
    return manifest


def write_manifest(manifest, config=None, dry_run=False):
    """Write the manifest JSON to disk (unless dry_run) and print a summary."""
    config = config or get_config()
    paths = config.resolved_paths()

    if dry_run:
        xxx_records = [r for r in manifest.get("composites", []) if r.get("family") == "xxx"]
        missing = [r.get("composite_path", r.get("name")) for r in xxx_records
                   if not r.get("characters")]
        association_summary = {
            "images": len(xxx_records), "associated": len(xxx_records) - len(missing),
            "missing": len(missing), "missing_examples": missing[:25],
        }
    else:
        paths["output_dir"].mkdir(exist_ok=True)
        paths["manifest_path"].parent.mkdir(parents=True, exist_ok=True)
        paths["manifest_path"].write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        association_summary = manifest.get("summary", {}).get("xxx_character_associations", {})

    print(json.dumps({
        "scripts_scanned": manifest["summary"]["scripts_scanned"],
        "composites": manifest["summary"]["composites"],
        "uses_processed": manifest["summary"]["uses_processed"],
        "unresolved": sorted(set(manifest.get("unresolved", []))),
        "xxx_character_associations": association_summary,
    }, indent=2))
    return manifest


def main():
    ap = argparse.ArgumentParser(description="Build reviewable composites for a Ren'Py game.")
    ap.add_argument("--game", help="Game name (used with default config convention)")
    ap.add_argument("--game-root", help="Absolute path to the Ren'Py \\game folder")
    ap.add_argument("--config", help="Path to a GameConfig JSON file")
    ap.add_argument("--archive-dir", help="Absolute path to the extracted asset archive")
    ap.add_argument("--output-dir", help="Directory for generated composite PNGs")
    ap.add_argument("--manifest", help="Path for the manifest JSON report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clean-output", action="store_true", help="remove prior generated PNGs and manifest before rendering")
    args = ap.parse_args()

    # ------------------------------------------------------------------ #
    # Resolve config: explicit JSON > path overrides > default convention.
    # ------------------------------------------------------------------ #
    if args.config:
        config = load_config(args.config)
    else:
        game_root = Path(args.game_root).resolve() if args.game_root else None
        if game_root is None and args.game:
            game_root = (Path("src/renpy_game") / args.game).resolve()
            # Append common build subdirectory if present.
            candidates = [p for p in game_root.iterdir() if p.is_dir() and p.name.endswith("-pc")]
            if candidates:
                game_root = (candidates[0] / "game").resolve()
        if game_root is None:
            game_root = Path("src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game").resolve()
        archive_dir = Path(args.archive_dir).resolve() if args.archive_dir else None
        config = default_config(
            args.game or game_root.parent.name,
            game_root,
            archive_dir,
        )
        # Apply output/manifest overrides without losing type fidelity.
        if args.output_dir or args.manifest:
            updates: dict[str, object] = {}
            if args.output_dir:
                updates["output_dir"] = Path(args.output_dir).resolve()
            if args.manifest:
                updates["manifest_path"] = Path(args.manifest).resolve()
            config = replace(config, **updates)

    set_config(config)

    if args.clean_output:
        paths = config.resolved_paths()
        if paths["output_dir"].exists():
            for folder in (paths["output_dir"] / "static", paths["output_dir"] / "dynamic_review"):
                if folder.exists():
                    for old in folder.rglob("*.png"):
                        old.unlink()
        paths["manifest_path"].unlink(missing_ok=True)

    manifest = compose(config, dry_run=args.dry_run, clean_output=args.clean_output)
    write_manifest(manifest, config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

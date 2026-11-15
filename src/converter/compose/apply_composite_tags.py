"""Adopt resolved composites in live Ren'Py scripts.

Reads the manifest JSON produced by the compose pass and rewrites safe ``show``/
``scene`` statements to use generated composite tags.  It also declares every
generated composite, including dynamic-screen state images, in
``game/composite_images.rpy``.

Statements with locally determined state are rewritten to a generated composite.
When state depends on globals/defaults, a runtime ``ConditionSwitch`` selects a
generated composite only for an exact condition state represented in the manifest,
and falls back to the original live image for all other states.

Run after ``compose_script_elements.py`` produced the manifest from clean sources.
The script refuses to touch a line whose text no longer matches the recorded
statement, so a second run is a no-op.

Everything is parameterized by a :class:`~converter.compose.config.GameConfig`;
no folder names or paths for a specific game are hard-coded.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from .config import GameConfig, default_config, load_config

# --------------------------------------------------------------------------- #
# Module-level configuration
# --------------------------------------------------------------------------- #
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
# Config-derived constants (replaces hard-coded paths / alternations).
# --------------------------------------------------------------------------- #
def _manifest_path() -> Path:
    return get_config().resolved_paths()["manifest_path"]


def _game_root() -> Path:
    return get_config().game_root


def _declaration_path() -> Path:
    return get_config().resolved_paths()["declaration_path"]


def _report_path() -> Path:
    return get_config().resolved_paths()["report_path"]


KEYWORDS = {"at", "with", "zorder", "as", "parent", "to"}
EXPR_KEYWORDS = {"True", "False", "and", "or", "not", "None"}
SHOW_RE = re.compile(r"^(show|scene)\s+\S+")
_PROVENANCE = {}
_DEFINITION_CONDITIONS = {}
_SOURCE_LINES = {}


def build_replacement(statement, composite_name):
    stripped = statement.strip()
    has_colon = stripped.endswith(":")
    content = stripped[:-1].rstrip() if has_colon else stripped
    tokens = content.split()
    result = [tokens[0], composite_name]
    for index in range(2, len(tokens)):
        if tokens[index] in KEYWORDS:
            result.extend(tokens[index:])
            break
    return " ".join(result) + (":" if has_colon else "")


def rewrite_script_line(raw_line, statement, composite_name):
    """Replace the show/scene displayable while retaining an inline comment."""
    code, marker, comment = raw_line.rstrip("\r\n").partition("#")
    newline = "\r\n" if raw_line.endswith("\r\n") else "\n" if raw_line.endswith("\n") else ""
    indent = code[:len(code) - len(code.lstrip())]
    replacement = indent + build_replacement(statement, composite_name)
    if marker:
        replacement += " #" + comment
    return replacement + newline


def line_uses_tag(path, line, tag):
    key = str(path)
    if key not in _SOURCE_LINES:
        _SOURCE_LINES[key] = path.read_text(
            encoding="utf-8-sig", errors="replace"
        ).splitlines()
    lines = _SOURCE_LINES[key]
    if not 1 <= line <= len(lines):
        return False
    return bool(re.match(
        rf"^(?:show|scene)\s+{re.escape(tag)}(?:\s|:|$)",
        lines[line - 1].strip(),
    ))


def reset_defaults_map(config=None):
    """Reuse the composer's ``*ResetDefault`` handling without importing its globals."""
    config = config or get_config()
    mapping = {}
    path = config.game_root / "characters.rpy"
    if not path.exists():
        return mapping
    current = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.split("#", 1)[0].strip()
        m = re.match(r"label\s+([A-Za-z_]\w*ResetDefault):", text)
        if m:
            current = m.group(1)
            mapping[current] = []
            continue
        if current:
            if re.match(r"return\b", text):
                current = None
                continue
            m = re.match(r"\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", text)
            if m:
                mapping[current].append(m.group(1))
    return mapping


def written_before(path, line, reset_map):
    key = str(path)
    if key not in _PROVENANCE:
        labels, writes = [0], []
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        for number, raw in enumerate(lines, 1):
            text = raw.split("#", 1)[0].strip()
            if re.match(r"label\s+[\w.]+(?:\([^\n]*\))?\s*:", text):
                labels.append(number)
            m = re.match(r"\$\s*([A-Za-z_]\w*)\s*=", text)
            if m:
                writes.append((number, m.group(1)))
            m = re.match(r"call\s+([A-Za-z_]\w*ResetDefault)\b", text)
            if m and m.group(1) in reset_map:
                writes.extend((number, name) for name in reset_map[m.group(1)])
            for name in re.findall(r"SetVariable\(\s*['\"]([A-Za-z_]\w*)['\"]", text):
                writes.append((number, name))
        _PROVENANCE[key] = (labels, writes)
    labels, writes = _PROVENANCE[key]
    start = labels[max(0, bisect.bisect_right(labels, line - 1) - 1)]
    return {name for number, name in writes if start <= number < line}


def condition_variables(record):
    names = set()
    for layer in record["ordered_layers"]:
        for cond in layer.get("conditions", []):
            names.update(v for v in re.findall(r"\b[A-Za-z_]\w*\b", cond) if v not in EXPR_KEYWORDS)
    return names


def generated_composites(manifest):
    """Return every extant generated image path keyed by its unique image tag."""
    paths = {}
    missing = []
    entries = list(manifest.get("composites", []))
    for matrix in manifest.get("dynamic_states", []):
        entries.extend(matrix.get("generated_states", []))

    for entry in entries:
        rel = entry.get("composite_path")
        if not rel:
            continue
        path = get_config().game_root.parent / rel
        if not path.is_file():
            missing.append(rel)
            continue
        tag = Path(rel).stem
        previous = paths.get(tag)
        if previous and previous != rel:
            raise ValueError(f"Generated composite image tag collision: {tag}: {previous}, {rel}")
        paths[tag] = rel
    return paths, missing


def replace_generated_screen_paths(image_tags):
    """Use declared image tags in screen switches instead of raw generated PNG paths."""
    replacements = {f'"../{rel.replace(chr(92), "/")}"': f'"{tag}"'
                    for tag, rel in image_tags.items()}
    changed = 0
    config = get_config()
    for path in config.game_root.rglob("*.rpy"):
        if "archive" in path.parts or path == _declaration_path():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        updated = text
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != text:
            changed += sum(text.count(old) for old in replacements if old in text)
            path.write_text(updated, encoding="utf-8-sig")
    return changed


def source_condition_universe(record):
    """Return effective branch conditions keyed by their source line.

    Matches asset paths using the config-driven regex (replaces the hard-coded
    ``(?:chars|xxx|bgs|gui)/`` alternation).
    """
    config = get_config()
    definition = record.get("definition", {})
    cache_key = (definition.get("script"), definition.get("line"), record.get("name"))
    if cache_key in _DEFINITION_CONDITIONS:
        return _DEFINITION_CONDITIONS[cache_key]
    source = get_config().game_root.parent / (definition.get("script") or "")
    if not source.is_file():
        return None
    lines = source.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    start = definition.get("line")
    if not isinstance(start, int) or start < 1 or start > len(lines):
        return None
    header = re.match(r"^(\s*)layeredimage\s+([\w.]+)(?:\([^\n]*\))?\s*:\s*$", lines[start - 1])
    if not header or header.group(2) != record.get("name"):
        return None
    block_indent = len(header.group(1))
    branch_stack = []
    sibling_conditions = {}
    effective_by_line = {}
    asset_re = config.path_regex()
    for number in range(start, len(lines)):
        raw = lines[number]
        if raw.strip() and len(raw) - len(raw.lstrip()) <= block_indent:
            break
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        indent = len(raw) - len(raw.lstrip())
        branch_stack = [(level, condition) for level, condition in branch_stack if level < indent]
        branch = re.match(r"(if|elif|else)\s*(.*?)\s*:\s*$", text)
        if branch:
            kind, condition = branch.groups()
            if kind == "if":
                sibling_conditions[indent] = [condition]
                effective = condition
            elif kind == "elif":
                previous = sibling_conditions.get(indent, [])
                effective = " and ".join(
                    [*(f"not ({item})" for item in previous), f"({condition})"]
                )
                sibling_conditions.setdefault(indent, []).append(condition)
            else:
                previous = sibling_conditions.get(indent, [])
                effective = " and ".join(f"not ({item})" for item in previous)
            branch_stack = [(level, value) for level, value in branch_stack if level < indent]
            branch_stack.append((indent, effective))
            continue
        if asset_re.search(text):
            effective_by_line[number + 1] = tuple(condition for _, condition in branch_stack)
    _DEFINITION_CONDITIONS[cache_key] = effective_by_line
    return effective_by_line


def attribute_signature(record):
    return tuple(sorted({
        layer.get("attribute")
        for layer in record.get("ordered_layers", [])
        if layer.get("attribute")
    }))


def runtime_switch_for_usage(records, key, statement, reset_map, all_records):
    """Build a runtime selector for generated snapshots, with the live image as fallback."""
    if not records or records[0][0].get("kind") != "layeredimage":
        return None, []
    path = get_config().game_root.parent / key[0]
    if not path.is_file():
        return None, []

    exemplar = records[0][0]
    signatures = {attribute_signature(record) for record, _ in records}
    if len(signatures) != 1:
        return None, sorted(set().union(*(condition_variables(r) for r, _ in records)))
    signature = next(iter(signatures))
    definition_key = (exemplar.get("definition", {}).get("script"),
                      exemplar.get("definition", {}).get("line"))
    variants = [
        record for record in all_records
        if record.get("kind") == "layeredimage"
        and record.get("name") == exemplar.get("name")
        and (record.get("definition", {}).get("script"),
             record.get("definition", {}).get("line")) == definition_key
        and record.get("status") == "static"
        and not record.get("dynamic_conditions")
        and attribute_signature(record) == signature
    ]
    if not variants:
        return None, sorted(set().union(*(condition_variables(r) for r, _ in records)))

    universe = source_condition_universe(exemplar)
    if universe is None or not universe:
        return None, sorted(set().union(*(condition_variables(r) for r in variants)))
    universe_conditions = {
        condition for conditions in universe.values() for condition in conditions
    }

    # Composite records include only active layers. The source definition supplies
    # the full condition universe, so each selector can require both its true and
    # false branch outcomes and cannot accidentally match a partial state.
    by_state = {}
    ambiguous_states = set()
    for record in variants:
        layer_lines = [layer.get("line") for layer in record.get("ordered_layers", [])]
        if any(line not in universe for line in layer_lines):
            continue
        positive = {
            condition for line in layer_lines for condition in universe[line]
        }
        if not positive.issubset(universe_conditions):
            continue
        state_key = tuple(sorted(positive))
        tag = Path(record["composite_path"]).stem
        previous = by_state.get(state_key)
        if previous is not None and previous != tag:
            ambiguous_states.add(state_key)
        else:
            by_state[state_key] = tag

    branches = []
    for state_key, tag in sorted(by_state.items(), key=lambda item: repr(item[0])):
        if state_key in ambiguous_states:
            continue
        positive = set(state_key)
        tests = [f"({cond})" for cond in sorted(positive)]
        tests.extend(f"not ({cond})" for cond in sorted(universe_conditions - positive))
        condition = " and ".join(tests)
        if condition:
            branches.extend((condition, tag))
    if not branches:
        return None, sorted(set().union(*(condition_variables(r) for r in variants)))

    statement_tokens = statement.rstrip().removesuffix(":").split()
    displayable = []
    for token in statement_tokens[2:]:
        if token in KEYWORDS:
            break
        displayable.append(token)
    fallback = " ".join([statement_tokens[1], *displayable])
    digest = hashlib.sha1(
        f"{exemplar['name']}:{signature}:{fallback}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "tag": f"compositeRuntime__{digest}",
        "branches": branches,
        "fallback": fallback,
        "statement": statement,
    }, sorted(set().union(*(condition_variables(r) for r in variants)))


# --------------------------------------------------------------------------- #
# Main apply pass
# --------------------------------------------------------------------------- #


def apply(config: GameConfig | None = None):
    """Run the full apply pass for ``config`` and return the (manifest, report) tuple."""
    config = config or get_config()
    set_config(config)

    manifest_path = _manifest_path()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image_tags, missing_generated = generated_composites(manifest)
    reset_map = reset_defaults_map(config)
    mapping, used = {}, {}
    skipped, runtime_switches = [], {}
    usage_records = defaultdict(list)
    already_direct = 0
    for record in manifest["composites"]:
        if record["status"] != "static" or record["kind"] == "screen":
            continue
        for loc in record["usage_locations"]:
            if "/archive/" in loc["script"]:
                continue
            if not SHOW_RE.match(loc["statement"].strip()):
                continue
            key = (loc["script"], loc["line"])
            usage_records[key].append((record, loc))

    for key, candidates in usage_records.items():
        statement = candidates[0][1]["statement"]
        path = get_config().game_root.parent / key[0]
        if not path.exists():
            continue
        composite_tags = {Path(record["composite_path"]).stem for record, _ in candidates}
        variables = set().union(*(condition_variables(record) for record, _ in candidates))
        written = written_before(path, key[1], reset_map)
        if len(composite_tags) == 1 and variables.issubset(written):
            name = next(iter(composite_tags))
            if line_uses_tag(path, key[1], name):
                already_direct += 1
            else:
                mapping[key] = {"name": name, "statement": statement}
            used[name] = next(iter(candidates))[0]["composite_path"]
            continue

        if variables - written:
            runtime_switch, _ = runtime_switch_for_usage(
                candidates, key, statement, reset_map, manifest["composites"]
            )
            if runtime_switch:
                runtime_switches[key] = runtime_switch
                used[runtime_switch["tag"]] = None
                continue
            skipped.append({"script": key[0], "line": key[1],
                            "statement": statement,
                            "global_defaults": sorted(variables - written)})

        elif len(composite_tags) == 1:
            name = next(iter(composite_tags))
            mapping[key] = {"name": name, "statement": statement}
            used[name] = next(iter(candidates))[0]["composite_path"]

    by_file = {}
    for (script, line), info in mapping.items():
        rel = script[len("game/"):] if script.startswith("game/") else script
        by_file.setdefault(rel, []).append((line, info))

    applied = unchanged = 0
    for script, entries in sorted(by_file.items()):
        path = _game_root() / script
        lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        newline = "\r\n" if any("\r\n" in line for line in lines[:50]) else "\n"
        changed = 0
        for line_no, info in sorted(entries, reverse=True):
            index = line_no - 1
            if (not (0 <= index < len(lines))
                    or lines[index].split("#", 1)[0].strip() != info["statement"]):
                unchanged += 1
                continue
            rewritten = rewrite_script_line(lines[index], info["statement"], info["name"])
            if not rewritten.endswith(("\n", "\r")):
                rewritten += newline
            lines[index] = rewritten
            changed += 1
        applied += changed
        path.write_text("".join(lines), encoding="utf-8-sig")
        print(f"{script}: {changed} replacements")

    # Runtime wrappers cover only exact states represented by generated PNGs.
    # Other values use the original live image/tag as the ConditionSwitch fallback.
    runtime_by_file = defaultdict(list)
    for (script, line), info in runtime_switches.items():
        rel = script[len("game/"):] if script.startswith("game/") else script
        runtime_by_file[rel].append((line, info))
    runtime_applied = 0
    runtime_already_applied = 0
    for script, entries in sorted(runtime_by_file.items()):
        path = _game_root() / script
        lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        changed = False
        for line_no, info in sorted(entries, reverse=True):
            index = line_no - 1
            if 0 <= index < len(lines) and re.match(
                    rf"^(?:show|scene)\s+{re.escape(info['tag'])}(?:\s|:|$)",
                    lines[index].strip()):
                runtime_already_applied += 1
                continue
            if (not (0 <= index < len(lines))
                    or lines[index].split("#", 1)[0].strip() != info["statement"]):
                skipped.append({"script": f"game/{script}", "line": line_no,
                                "statement": info["statement"], "reason": "source_line_changed"})
                continue
            lines[index] = rewrite_script_line(lines[index], info["statement"], info["tag"])
            runtime_applied += 1
            changed = True
        if changed:
            path.write_text("".join(lines), encoding="utf-8-sig")

    runtime_paths_replaced = replace_generated_screen_paths(image_tags)
    declarations = ["# Auto-generated composite image declarations",
                    "# Includes all generated composites and dynamic screen-state images.",
                    "# Regenerate with tools/compose_script_characters.py and tools/apply_composite_tags.py",
                    ""]
    for name, rel in sorted(image_tags.items()):
        declarations.append(f'image {name} = "../{rel.replace(chr(92), "/")}"')
    wrapper_declarations = {info["tag"]: info for info in runtime_switches.values()}
    for info in sorted(wrapper_declarations.values(), key=lambda item: item["tag"]):
        declarations.extend([
            f'image {info["tag"]} = ConditionSwitch(',
            *[f'    {json.dumps(condition)}, {json.dumps(tag)},'
              for condition, tag in zip(info["branches"][::2], info["branches"][1::2])],
            f'    "True", {json.dumps(info["fallback"])},',
            ')',
        ])
    _declaration_path().write_text("\n".join(declarations) + "\n", encoding="utf-8-sig")

    applied += runtime_applied + runtime_already_applied + already_direct
    report = {"applied": applied, "unchanged": unchanged,
              "skipped_runtime_state": len(skipped), "skipped": skipped,
              "declared_generated_composites": len(image_tags),
              "runtime_paths_replaced": runtime_paths_replaced,
              "runtime_switch_uses": runtime_applied + runtime_already_applied,
              "runtime_switch_declarations": len(wrapper_declarations),
              "runtime_switch_updates_this_run": runtime_applied,
              "missing_generated_files": missing_generated}
    _report_path().parent.mkdir(parents=True, exist_ok=True)
    _report_path().write_text(json.dumps(report, indent=2), encoding="utf-8")
    manifest.setdefault("summary", {})["adoption"] = {
        "applied_statements": applied,
        "skipped_runtime_state": len(skipped),
        "adopted_composites": len(used),
        "declared_generated_composites": len(image_tags),
        "runtime_paths_replaced": runtime_paths_replaced,
        "runtime_switch_uses": runtime_applied + runtime_already_applied,
        "runtime_switch_declarations": len(wrapper_declarations),
        "missing_generated_files": len(missing_generated),
        "policy": ("use an exact generated condition state when available; otherwise runtime selectors fall "
                   "back to the original image displayable"),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"applied {applied}, unchanged {unchanged}, skipped for runtime state {len(skipped)}, "
          f"adopted {len(used)} composites, declared {len(image_tags)} generated images, "
          f"runtime selector uses {runtime_applied + runtime_already_applied} "
          f"({len(wrapper_declarations)} wrappers), "
          f"updated {runtime_paths_replaced} "
          f"screen image paths, skipped without safe state maps {len(skipped)}, "
          f"missing files {len(missing_generated)}")
    return manifest, report


def main():
    ap = argparse.ArgumentParser(description="Adopt generated composites into a Ren'Py game's scripts.")
    ap.add_argument("--game", help="Game name (used with default config convention)")
    ap.add_argument("--game-root", help="Absolute path to the Ren'Py \\game folder")
    ap.add_argument("--config", help="Path to a GameConfig JSON file")
    ap.add_argument("--archive-dir", help="Absolute path to the extracted asset archive")
    ap.add_argument("--manifest", help="Path to the manifest JSON (default: <output>/reports/manifest.json)")
    args = ap.parse_args()

    if args.config:
        config = load_config(args.config)
    else:
        game_root = Path(args.game_root).resolve() if args.game_root else None
        if game_root is None and args.game:
            game_root = (Path("src/renpy_game") / args.game).resolve()
            candidates = [p for p in game_root.iterdir() if p.is_dir() and p.name.endswith("-pc")]
            if candidates:
                game_root = (candidates[0] / "game").resolve()
        if game_root is None:
            game_root = Path("src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game").resolve()
        archive_dir = Path(args.archive_dir).resolve() if args.archive_dir else None
        config = default_config(args.game or game_root.parent.name, game_root, archive_dir)
        if args.manifest:
            config = replace(config, manifest_path=Path(args.manifest).resolve())

    set_config(config)
    apply(config)


if __name__ == "__main__":
    main()

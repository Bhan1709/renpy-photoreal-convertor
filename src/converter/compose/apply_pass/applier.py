"""Apply pass orchestration.

Responsibility: read the manifest produced by the compose pass and adopt the
generated composites into the game's live Ren'Py scripts:

- Rewrite static ``show``/``scene`` statements to point at generated composite tags.
- Emit runtime ``ConditionSwitch`` wrappers when state depends on globals.
- Declare every generated composite in ``game/composite_images.rpy``.
- Write an adoption report.

No image compositing happens here — only script rewriting and declaration generation.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import NotRequired, TypedDict

from cli import counted, loading, note, print_summary, progress_bar

from ...utility import atomic_write_json
from ..conditions import condition_variables, get_label_assignments_map
from ..config import GameConfig
from ..errors import ApplyError, ManifestError
from ..manifest import KNOWN_FAMILIES, ManifestWriter, resolve_category_manifest_paths
from ..models import AdoptionReport, CompositeManifest, CompositeRecord
from ..regexp import SHOW_RE

KEYWORDS = {"at", "with", "zorder", "as", "parent", "to"}


class _CompositeUsageInfo(TypedDict):
    """Shared info for a show/scene statement adoption, direct or runtime."""

    statement: str
    name: NotRequired[str]
    tag: NotRequired[str]
    branches: NotRequired[list[str]]
    fallback: NotRequired[str]


class Applier:
    """Runs the full apply pass for a :class:`GameConfig`."""

    def __init__(
        self,
        config: GameConfig,
        *,
        manifest_index_path: Path | None = None,
        report_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.manifests = ManifestWriter(
            config,
            manifest_index_path=manifest_index_path,
            report_dir=report_dir,
        )
        # Single cache for raw script lines, shared by all file-reading methods.
        self._source_text_cache: dict[str, tuple[str, ...]] = {}
        # Derived cache for (labels, writes) computed from raw lines.
        self._written_cache: dict[str, tuple[list[int], list[tuple[int, str]]]] = {}
        self._definition_condition_cache: dict[
            tuple[str, int, str], dict[int, tuple[str, ...]] | None
        ] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def apply(self) -> tuple[CompositeManifest, AdoptionReport]:
        """Run the apply pass and return ``(manifest, report)``.

        Composites are loaded and applied one category manifest at a time
        (bg, char, scene, gui, …) with a progress bar, rather than merging all
        manifests into a single in-memory list up front.
        """
        # Global index payload (dynamic states, declarations, summary) is read
        # once; composites are streamed per category manifest below.
        index_payload = self._load_index_payload()
        dynamic_states = index_payload["dynamic_states"]
        summary = dict(index_payload.get("summary", {}))

        with loading("Loading script assignment states..."):
            reset_map = get_label_assignments_map(self.config)

        mapping: dict[tuple[str, int], _CompositeUsageInfo] = {}
        used: dict[str, str | None] = {}
        skipped: list[dict] = []
        runtime_switches: dict[tuple[str, int], _CompositeUsageInfo] = {}
        already_direct = 0
        all_records: list[CompositeRecord] = []

        usage_records: dict[tuple[str, int], list[tuple]] = defaultdict(list)

        category_files = self._category_manifest_files()
        with progress_bar(
            total=len(category_files),
            desc="Loading manifests",
            unit="manifests",
        ) as bar:
            for cat_path in category_files:
                cat_data = json.loads(cat_path.read_text(encoding="utf-8"))
                for c in cat_data.get("composites", []):
                    record = CompositeRecord.from_dict(c)
                    all_records.append(record)
                    if record.status != "static":
                        continue
                    for loc in record.usage_locations:
                        if "/archive/" in loc.script:
                            continue
                        if not SHOW_RE.match(loc.statement.strip()):
                            continue
                        key = (loc.script, loc.line)
                        usage_records[key].append((record, loc))
                bar.update(1)

        with progress_bar(
            total=len(usage_records),
            desc="Resolving composite usages",
            unit="usages",
        ) as usage_bar:
            for key, candidates in usage_records.items():
                try:
                    statement = candidates[0][1].statement
                    path = self.config.game_root.parent / key[0]
                    if not path.exists():
                        continue
                    composite_tags = {
                        Path(record.composite_path).stem for record, _ in candidates
                    }
                    variables = set().union(
                        *(_condition_variables(record) for record, _ in candidates)
                    )
                    written = self._written_before(path, key[1], reset_map)
                    if len(composite_tags) == 1 and variables.issubset(written):
                        name = next(iter(composite_tags))
                        if self._line_uses_tag(path, key[1], name):
                            already_direct += 1
                        else:
                            mapping[key] = {"name": name, "statement": statement}
                        used[name] = next(iter(candidates))[0].composite_path
                        continue

                    # Multiple variants (runtime-dependent) — try to build a
                    # ConditionSwitch so the live script can select at runtime.
                    runtime_switch, _ = self._runtime_switch_for_usage(
                        candidates, key, statement, all_records
                    )
                    if runtime_switch:
                        runtime_switches[key] = runtime_switch
                        used[runtime_switch["tag"]] = None  # type: ignore[typeddict-item]
                        continue
                    elif len(composite_tags) == 1:
                        # Single variant — direct mapping even without a switch.
                        name = next(iter(composite_tags))
                        mapping[key] = {"name": name, "statement": statement}
                        used[name] = next(iter(candidates))[0].composite_path
                        continue
                    skipped.append(
                        {
                            "script": key[0],
                            "line": key[1],
                            "statement": statement,
                            "global_defaults": sorted(variables - written),
                        }
                    )
                finally:
                    usage_bar.update(1)

        # Build the merged manifest from the per-category composites streamed
        # above, so the apply pass can resolve generated images / missing files.
        full_manifest = CompositeManifest(
            composites=tuple(all_records),
            dynamic_states=dynamic_states,
            image_declarations=index_payload["image_declarations"],
            dynamic_declarations=index_payload["dynamic_declarations"],
            unresolved=tuple(index_payload.get("unresolved", [])),
            summary=summary,
            cfg_processed=int(index_payload.get("cfg_processed", 0)),
            cfg_truncated=bool(index_payload.get("cfg_truncated", False)),
        )
        with loading("Checking generated composite files..."):
            image_tags, missing_generated = self._generated_composites(full_manifest)

        # --- Rewrite direct mappings ---
        by_file = self._group_by_file(mapping)
        applied = 0
        unchanged = 0
        with progress_bar(
            total=len(by_file),
            desc="Updating game scripts",
            unit="files",
        ) as script_bar:
            for script, entries in sorted(by_file.items()):
                path = self.config.game_root / script
                lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
                newline = "\r\n" if any("\r\n" in line for line in lines[:50]) else "\n"
                changed = 0
                for line_no, info in sorted(entries, reverse=True):
                    index = line_no - 1
                    if (
                        not (0 <= index < len(lines))
                        or lines[index].split("#", 1)[0].strip() != info["statement"]
                    ):
                        unchanged += 1
                        continue
                    rewritten = _rewrite_script_line(
                        lines[index], info["statement"], info["name"]
                    )  # type: ignore[typeddict-item]
                    if not rewritten.endswith(("\n", "\r")):
                        rewritten += newline
                    lines[index] = rewritten
                    changed += 1
                applied += changed
                path.write_text("".join(lines), encoding="utf-8-sig")
                note(f"{script}: {counted(changed, 'replacement')}.")
                script_bar.update(1)

        # --- Runtime ConditionSwitch wrappers ---
        runtime_by_file: dict[str, list[tuple[int, _CompositeUsageInfo]]] = {}
        for (script, line), info in runtime_switches.items():
            rel = script.removeprefix("game/")
            runtime_by_file.setdefault(rel, []).append((line, info))
        runtime_applied = 0
        runtime_already_applied = 0
        with progress_bar(
            total=len(runtime_by_file),
            desc="Applying runtime selectors",
            unit="files",
        ) as runtime_bar:
            for script, entries in sorted(runtime_by_file.items()):
                path = self.config.game_root / script
                lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
                changed = False
                for line_no, info in sorted(entries, reverse=True):
                    index = line_no - 1
                    if 0 <= index < len(lines) and re.match(
                        rf"^(?:show|scene)\s+{re.escape(info['tag'])}(?:\s|:|$)",  # type: ignore[typeddict-item]
                        lines[index].strip(),
                    ):
                        runtime_already_applied += 1
                        continue
                    if (
                        not (0 <= index < len(lines))
                        or lines[index].split("#", 1)[0].strip() != info["statement"]
                    ):
                        skipped.append(
                            {
                                "script": f"game/{script}",
                                "line": line_no,
                                "statement": info["statement"],
                                "reason": "source_line_changed",
                            }
                        )
                        continue
                    lines[index] = _rewrite_script_line(
                        lines[index], info["statement"], info["tag"]
                    )  # type: ignore[typeddict-item]
                    runtime_applied += 1
                    changed = True
                if changed:
                    path.write_text("".join(lines), encoding="utf-8-sig")
                runtime_bar.update(1)

        # --- Replace raw generated paths in screens with declared tags ---
        with loading("Replacing generated paths in screen declarations..."):
            runtime_paths_replaced = self._replace_generated_screen_paths(image_tags)

        # --- Write composite_images.rpy declarations ---
        declarations = [
            "# Auto-generated composite image declarations",
            "# Includes all generated composites and dynamic screen-state images.",
            "# Regenerate with the compose subpackage apply pass.",
            "",
        ]
        for name, rel in sorted(image_tags.items()):
            declarations.append(f'image {name} = "../{rel.replace(chr(92), "/")}"')
        wrapper_declarations = {info["tag"]: info for info in runtime_switches.values()}  # type: ignore[typeddict-item]
        for info in sorted(
            wrapper_declarations.values(), key=lambda item: str(item["tag"])
        ):  # type: ignore[typeddict-item]
            branches = info["branches"]  # type: ignore[typeddict-item]
            declarations.append(f"image {info['tag']} = ConditionSwitch(")  # type: ignore[typeddict-item]
            declarations.extend(
                f"    {json.dumps(condition)}, {json.dumps(tag)},"
                for condition, tag in zip(branches[::2], branches[1::2])
            )
            declarations.append(f'    "True", {json.dumps(info["fallback"])},')  # type: ignore[typeddict-item]
            declarations.append(")")
        declaration_path = self._declaration_path()
        declaration_path.write_text(
            "\n".join(declarations) + "\n", encoding="utf-8-sig"
        )

        applied += runtime_applied + runtime_already_applied + already_direct
        report = AdoptionReport(
            applied=applied,
            unchanged=unchanged,
            skipped_runtime_state=len(skipped),
            skipped=skipped,
            declared_generated_composites=len(image_tags),
            runtime_paths_replaced=runtime_paths_replaced,
            runtime_switch_uses=runtime_applied + runtime_already_applied,
            runtime_switch_declarations=len(wrapper_declarations),
            runtime_switch_updates_this_run=runtime_applied,
            missing_generated_files=missing_generated,
        )
        report_path = self._report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

        # Update manifest index summary with adoption info.
        full_manifest.summary["adoption"] = {
            "applied_statements": applied,
            "skipped_runtime_state": len(skipped),
            "adopted_composites": len(used),
            "declared_generated_composites": len(image_tags),
            "runtime_paths_replaced": runtime_paths_replaced,
            "runtime_switch_uses": runtime_applied + runtime_already_applied,
            "runtime_switch_declarations": len(wrapper_declarations),
            "missing_generated_files": len(missing_generated),
            "policy": (
                "use an exact generated condition state when available; otherwise runtime selectors fall "
                "back to the original image displayable"
            ),
        }
        # Write updated index manifest (per-category manifests are unchanged;
        # only the index carries the adoption summary).
        index_path = self.manifests.manifest_index_path
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_data = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.is_file()
            else {}
        )
        index_data["summary"] = full_manifest.summary
        atomic_write_json(index_path, index_data)
        print_summary(
            "Apply summary",
            [
                ("Applied statements", str(applied)),
                ("Unchanged statements", str(unchanged)),
                ("Skipped for runtime state", str(len(skipped))),
                ("Adopted composites", str(len(used))),
                ("Declared generated images", str(len(image_tags))),
                (
                    "Runtime selector uses",
                    str(runtime_applied + runtime_already_applied),
                ),
                ("Runtime selector declarations", str(len(wrapper_declarations))),
                ("Updated screen image paths", str(runtime_paths_replaced)),
                ("Skipped without safe state maps", str(len(skipped))),
                ("Missing generated files", str(len(missing_generated))),
            ],
        )
        return full_manifest, report

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #

    def _declaration_path(self) -> Path:
        return self.config.resolved_paths()["declaration_path"]

    def _report_path(self) -> Path:
        if self.config.report_path is not None:
            return self.config.resolved_paths()["report_path"]
        return self.manifests.report_dir / "adoption_report.json"

    def _read_source_lines(self, path: Path) -> tuple[str, ...] | None:
        """Read and cache raw lines from a script file, shared by all readers."""
        key = str(path)
        if key in self._source_text_cache:
            return self._source_text_cache[key]
        try:
            lines = tuple(
                path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            )
        except OSError:
            return None
        self._source_text_cache[key] = lines
        return lines

    # ------------------------------------------------------------------ #
    # Composite discovery
    # ------------------------------------------------------------------ #

    def _generated_composites(
        self, manifest: CompositeManifest
    ) -> tuple[dict[str, str], list[str]]:
        """Return every extant generated image path keyed by its unique image tag."""
        paths: dict[str, str] = {}
        missing: list[str] = []
        entries = list(manifest.composites_list)
        for state in manifest.dynamic_states:
            for entry in state.get("generated_states", []):
                rel = entry.get("composite_path")
                if not rel:
                    continue
                p = self.config.game_root.parent / rel
                if not p.is_file():
                    missing.append(rel)
                    continue
                tag = Path(rel).stem
                prev = paths.get(tag)
                if prev and prev != rel:
                    raise ApplyError(
                        f"Generated composite image tag collision: {tag}: {prev}, {rel}"
                    )
                paths[tag] = rel
        for record in entries:
            rel = record.composite_path
            if not rel:
                continue
            p = self.config.game_root.parent / rel
            if not p.is_file():
                missing.append(rel)
                continue
            tag = Path(rel).stem
            prev = paths.get(tag)
            if prev and prev != rel:
                raise ApplyError(
                    f"Generated composite image tag collision: {tag}: {prev}, {rel}"
                )
            paths[tag] = rel
        return paths, missing

    # ------------------------------------------------------------------ #
    # Script analysis helpers
    # ------------------------------------------------------------------ #

    def _written_before(
        self, path: Path, line: int, reset_map: dict[str, list[tuple[str, str]]]
    ) -> set[str]:
        """Return variable names written before ``line`` in ``path``."""
        key = str(path)
        if key in self._written_cache:
            labels, writes = self._written_cache[key]
        else:
            lines = self._read_source_lines(path)
            if lines is None:
                return set()
            labels = [0]
            writes = []
            for number, raw in enumerate(lines, 1):
                text = raw.split("#", 1)[0].strip()
                if re.match(r"label\s+[\w.]+(?:\([^\n]*\))?\s*:", text):
                    labels.append(number)
                m = re.match(r"\$\s*([A-Za-z_]\w*)\s*=", text)
                if m:
                    writes.append((number, m.group(1)))
                m_call = re.match(r"call\s+([A-Za-z_]\w*)\b", text)
                if m_call and m_call.group(1) in reset_map:
                    writes.extend(
                        (number, name) for name, _value in reset_map[m_call.group(1)]
                    )
                for name in re.findall(
                    r"SetVariable\(\s*['\"]([A-Za-z_]\w*)['\"]", text
                ):
                    writes.append((number, name))
            self._written_cache[key] = (labels, writes)
        start = labels[max(0, bisect.bisect_right(labels, line - 1) - 1)]
        return {name for number, name in writes if start <= number < line}

    def _line_uses_tag(self, path: Path, line: int, tag: str) -> bool:
        lines = self._read_source_lines(path)
        if lines is None:
            return False
        if not 1 <= line <= len(lines):
            return False
        return bool(
            re.match(
                rf"^(?:show|scene)\s+{re.escape(tag)}(?:\s|:|$)",
                lines[line - 1].strip(),
            )
        )

    def _runtime_switch_for_usage(
        self,
        candidates: list[tuple],
        key: tuple[str, int],
        statement: str,
        all_records,
    ) -> tuple[_CompositeUsageInfo | None, list[str]]:
        """Build a runtime selector for generated snapshots, with the live image as fallback."""
        if not candidates or candidates[0][0].kind not in ("layeredimage", "screen"):
            return None, []
        exemplar = candidates[0][0]
        signatures = {_attribute_signature(record) for record, _ in candidates}
        if len(signatures) != 1:
            return None, sorted(
                set().union(*(_condition_variables(r) for r, _ in candidates))
            )
        signature = next(iter(signatures))
        definition_key = (exemplar.definition.script, exemplar.definition.line)
        variants = [
            record
            for record in all_records
            if record.kind in ("layeredimage", "screen")
            and record.name == exemplar.name
            and (record.definition.script, record.definition.line) == definition_key
            and record.status == "static"
            and not record.dynamic_conditions
            and _attribute_signature(record) == signature
        ]
        if not variants:
            return None, sorted(
                set().union(*(_condition_variables(r) for r in variants))
            )

        universe = self._source_condition_universe(exemplar)
        if universe is None or not universe:
            return None, sorted(
                set().union(*(_condition_variables(r) for r in variants))
            )
        universe_conditions = {
            condition for conditions in universe.values() for condition in conditions
        }

        by_state: dict[tuple[str, ...], str] = {}
        ambiguous_states: set[tuple[str, ...]] = set()
        for record in variants:
            layer_lines = [layer.line for layer in record.ordered_layers]
            if any(line not in universe for line in layer_lines):
                continue
            positive = {
                condition for line in layer_lines for condition in universe[line]
            }
            if not positive.issubset(universe_conditions):
                continue
            state_key = tuple(sorted(positive))
            tag = Path(record.composite_path).stem
            previous = by_state.get(state_key)
            if previous is not None and previous != tag:
                ambiguous_states.add(state_key)
            else:
                by_state[state_key] = tag

        branches: list[str] = []
        for state_key, tag in sorted(by_state.items(), key=lambda item: repr(item[0])):
            if state_key in ambiguous_states:
                continue
            positive = set(state_key)
            tests = [f"({cond})" for cond in sorted(positive)]
            tests.extend(
                f"not ({cond})" for cond in sorted(universe_conditions - positive)
            )
            condition = " and ".join(tests)
            if condition:
                branches.extend((condition, tag))
        if not branches:
            return None, sorted(
                set().union(*(_condition_variables(r) for r in variants))
            )

        statement_tokens = statement.rstrip().removesuffix(":").split()
        displayable = []
        for token in statement_tokens[2:]:
            if token in KEYWORDS:
                break
            displayable.append(token)
        fallback = " ".join([statement_tokens[1], *displayable])
        digest = hashlib.sha1(
            f"{exemplar.name}:{signature}:{fallback}".encode()
        ).hexdigest()[:12]
        return {
            "tag": f"compositeRuntime__{digest}",
            "branches": branches,
            "fallback": fallback,
            "statement": statement,
        }, sorted(set().union(*(_condition_variables(r) for r in variants)))

    def _source_condition_universe(self, record) -> dict[int, tuple[str, ...]] | None:
        """Return effective branch conditions keyed by their source line."""
        definition = record.definition
        cache_key = (definition.script, definition.line, record.name)
        if cache_key in self._definition_condition_cache:
            return self._definition_condition_cache[cache_key]
        source = self.config.game_root.parent / (definition.script or "")
        if not source.is_file():
            self._definition_condition_cache[cache_key] = None
            return None
        lines = self._read_source_lines(source)
        if lines is None:
            self._definition_condition_cache[cache_key] = None
            return None
        start = definition.line
        if not isinstance(start, int) or start < 1 or start > len(lines):
            self._definition_condition_cache[cache_key] = None
            return None
        header = re.match(
            r"^(\s*)(?:layeredimage|screen)\s+([\w.]+)(?:\([^\n]*\))?\s*:\s*$",
            lines[start - 1],
        )
        if not header or header.group(2) != record.name:
            self._definition_condition_cache[cache_key] = None
            return None
        block_indent = len(header.group(1))
        branch_stack: list[tuple[int, str]] = []
        sibling_conditions: dict[int, list[str]] = {}
        effective_by_line: dict[int, tuple[str, ...]] = {}
        asset_re = self.config.path_regex()
        for number in range(start, len(lines)):
            raw = lines[number]
            if raw.strip() and len(raw) - len(raw.lstrip()) <= block_indent:
                break
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            indent = len(raw) - len(raw.lstrip())
            branch_stack = [
                (level, cond) for level, cond in branch_stack if level < indent
            ]
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
                branch_stack = [
                    (level, value) for level, value in branch_stack if level < indent
                ]
                branch_stack.append((indent, effective))
                continue
            if asset_re.search(text):
                effective_by_line[number + 1] = tuple(cond for _, cond in branch_stack)
        self._definition_condition_cache[cache_key] = effective_by_line
        return effective_by_line

    def _replace_generated_screen_paths(self, image_tags: dict[str, str]) -> int:
        """Use declared image tags in screen switches instead of raw generated PNG paths."""
        replacements = {
            f'"../{rel.replace(chr(92), "/")}"': f'"{tag}"'
            for tag, rel in image_tags.items()
        }
        changed = 0
        for path in self.config.game_root.rglob("*.rpy"):
            if "archive" in path.parts or path == self._declaration_path():
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            # Fast-path: skip files that reference no generated composite path.
            if not any(old in text for old in replacements):
                continue
            updated = text
            for old, new in replacements.items():
                updated = updated.replace(old, new)
            if updated != text:
                changed += sum(text.count(old) for old in replacements if old in text)
                path.write_text(updated, encoding="utf-8-sig")
        return changed

    def _group_by_file(
        self, mapping: dict[tuple[str, int], _CompositeUsageInfo]
    ) -> dict[str, list[tuple[int, _CompositeUsageInfo]]]:
        by_file: dict[str, list[tuple[int, _CompositeUsageInfo]]] = {}
        for (script, line), info in mapping.items():
            rel = script.removeprefix("game/")
            by_file.setdefault(rel, []).append((line, info))
        return dict(by_file)

    def _load_index_payload(self) -> dict:
        """Load the global manifest index payload (without category records)."""
        index_path = self.manifests.manifest_index_path
        if not index_path.is_file():
            raise ManifestError(f"Manifest index not found: {index_path}")
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"Could not read manifest index {index_path}: {exc}"
            ) from exc

    def _category_manifest_files(self) -> list[Path]:
        """Return referenced category manifest files in canonical order."""
        index_path = self.manifests.manifest_index_path
        index_payload = self._load_index_payload()
        category_paths = resolve_category_manifest_paths(
            index_payload,
            index_path,
            self.manifests.report_dir,
        )
        return [
            category_paths[category]
            for category in KNOWN_FAMILIES
            if category in category_paths
        ]


# ---------------------------------------------------------------------- #
# Module-level helpers (pure functions, no config needed)
# ---------------------------------------------------------------------- #


def _condition_variables(record) -> set[str]:
    names: set[str] = set()
    for layer in record.ordered_layers:
        for cond in layer.conditions:
            names.update(condition_variables(cond))
    return names


def _attribute_signature(record) -> tuple:
    return tuple(
        sorted({layer.attribute for layer in record.ordered_layers if layer.attribute})
    )


def _rewrite_script_line(raw_line: str, statement: str, composite_name: str) -> str:
    """Replace the show/scene displayable while retaining an inline comment."""
    stripped = statement.strip()
    has_colon = stripped.endswith(":")
    content = stripped[:-1].rstrip() if has_colon else stripped
    tokens = content.split()
    result = [tokens[0], composite_name]
    for index in range(2, len(tokens)):
        if tokens[index] in KEYWORDS:
            result.extend(tokens[index:])
            break
    replacement = " ".join(result) + (":" if has_colon else "")

    code, marker, comment = raw_line.rstrip("\r\n").partition("#")
    newline = (
        "\r\n" if raw_line.endswith("\r\n") else "\n" if raw_line.endswith("\n") else ""
    )
    indent = code[: len(code) - len(code.lstrip())]
    replacement = indent + replacement
    if marker:
        replacement += " #" + comment
    return replacement + newline

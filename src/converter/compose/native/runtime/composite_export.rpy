init -999 python in _composite_export:

    import ctypes
    import hashlib
    import json
    import os
    import re
    import struct
    import sys
    import traceback

    try:
        import builtins as _builtins
    except ImportError:
        import __builtin__ as _builtins

    import renpy

    PROTOCOL = "renpy-compose-native-v2"
    SCHEMA_VERSION = 2

    try:
        _STRING_TYPES = (basestring,)
    except NameError:
        _STRING_TYPES = (_builtins.str,)

    _MISSING = _builtins.object()

    _IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
    _EQ_RE = re.compile(
        r"\b([A-Za-z_]\w*)\s*==\s*"
        r"(-?\d+(?:\.\d+)?|True|False|None|'[^']*'|\"[^\"]*\")"
    )
    _KEYWORDS = set(
        [
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
        ]
    )

    def _log(message):
        try:
            sys.stderr.write("composite_export: " + message + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _load_config():
        path = os.environ.get("RENPY_COMPOSITE_CONFIG")
        if not path:
            raise RuntimeError("RENPY_COMPOSITE_CONFIG is not set")
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        if not isinstance(data, _builtins.dict):
            raise RuntimeError("native compose config is not an object")
        return data

    def _write_result(data):
        path = os.environ.get("RENPY_COMPOSITE_OUTPUT")
        if not path:
            raise RuntimeError("RENPY_COMPOSITE_OUTPUT is not set")
        text = json.dumps(data, sort_keys=True)
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))

    def _write_error(message):
        try:
            _write_result(
                {
                    "protocol": PROTOCOL,
                    "schema_version": SCHEMA_VERSION,
                    "status": "error",
                    "error": {"message": message},
                }
            )
        except Exception:
            pass

    def _states_signature(config):
        """Hash the inputs that affect state enumeration, not batching."""
        payload = {
            "format": 2,
            "game_root": config.get("game_root") or "",
            "modes": config.get("modes") or [],
            "defaults": config.get("defaults") or {},
            "max_synthetic_states": int(config.get("max_synthetic_states") or 1024),
            "max_condition_assignments": int(
                config.get("max_condition_assignments") or 64
            ),
            "max_cfg_states": int(config.get("max_cfg_states") or 1000000),
            "max_cfg_depth": int(config.get("max_cfg_depth") or 6),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _save_states(path, signature, states, walk_stats):
        """Freeze the enumerated states so every batch slices the same list.

        Usage lists are interned: the states share the same usage dict objects,
        so a cheap ``id()`` tuple identifies each distinct list and only one
        copy is written to disk. Embedding them per state produced multi-GB
        caches that took minutes to parse on every batch.
        """
        usage_lists = []
        usage_refs = {}
        compact = []
        for entry in states:
            usages = _dedupe_usages(entry.get("usages") or [])
            key = tuple(id(usage) for usage in usages)
            ref = usage_refs.get(key)
            if ref is None:
                ref = len(usage_lists)
                usage_refs[key] = ref
                usage_lists.append(usages)
            compact.append(
                {
                    "kind": entry["kind"],
                    "tag": entry["tag"],
                    "attributes": list(entry["attributes"]),
                    "condition_assignments": dict(entry["condition_assignments"]),
                    "synthetic": bool(entry.get("synthetic")),
                    "usage_ref": ref,
                }
            )
        payload = {
            "signature": signature,
            "walk": walk_stats,
            "usage_lists": usage_lists,
            "states": compact,
        }
        temp_path = path + ".tmp"
        with open(temp_path, "wb") as handle:
            handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.replace(temp_path, path)

    def _load_states(path, signature):
        """Return the frozen ``(states, walk_stats)``, or None if unusable."""
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
            if payload.get("signature") != signature:
                return None
            usage_lists = payload.get("usage_lists") or []
            states = []
            for entry in payload["states"]:
                ref = int(entry.get("usage_ref") or 0)
                usages = (
                    usage_lists[ref] if 0 <= ref < len(usage_lists) else []
                )
                states.append(
                    {
                        "kind": entry["kind"],
                        "tag": entry["tag"],
                        "attributes": tuple(entry["attributes"]),
                        "condition_assignments": dict(
                            entry["condition_assignments"]
                        ),
                        "synthetic": bool(entry.get("synthetic")),
                        "usages": list(usages),
                    }
                )
            return states, dict(payload.get("walk") or {})
        except Exception:
            return None

    def _pygame():
        import pygame_sdl2

        return pygame_sdl2

    # Mutable module state (avoids ``global`` in store namespaces).
    _STATE = {"progress_path": None}

    def _now():
        import time

        return time.time()

    def _progress(message):
        """Append one progress line when a progress log was configured."""
        path = _STATE.get("progress_path")
        if not path:
            return
        try:
            with open(path, "a") as handle:
                handle.write(message + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Display lifecycle
    # ------------------------------------------------------------------ #

    def _setup_display(config):
        try:
            renpy.config.gl2 = False
        except Exception:
            pass

        try:
            renpy.game.preferences.renderer = "sw"
        except Exception:
            pass

        try:
            renpy.session["renderer"] = "sw"
        except Exception:
            pass

        try:
            renpy.config.save = False
            renpy.config.save_persistent = False
        except Exception:
            pass

        try:
            renpy.config.extra_savedirs = []
        except Exception:
            pass

        width = int(config.get("screen_width") or renpy.config.screen_width)
        height = int(config.get("screen_height") or renpy.config.screen_height)
        renpy.config.screen_width = width
        renpy.config.screen_height = height

        interface = renpy.display.interface
        if interface is None:
            raise RuntimeError("Ren'Py display interface is unavailable")
        interface.start()

        draw = renpy.display.draw
        if draw is None:
            raise RuntimeError("Ren'Py draw object is unavailable")

        info = getattr(draw, "info", None) or {}
        renderer = info.get("renderer")
        if renderer != "sw":
            raise RuntimeError(
                "expected the software renderer, got {0!r}".format(renderer)
            )

        return width, height

    # ------------------------------------------------------------------ #
    # Script / image helpers
    # ------------------------------------------------------------------ #

    def _script_rel(filename, game_root):
        if not filename:
            return ""
        name = str(filename).replace("\\", "/")
        if name.startswith("game/"):
            return name
        if os.path.isabs(name):
            root = os.path.abspath(game_root or "")
            try:
                relative = os.path.relpath(os.path.abspath(name), root)
            except Exception:
                return name
            if relative.startswith(".."):
                return name
            return "game/" + relative.replace("\\", "/")
        return "game/" + name

    def _node_statement(node, name):
        if isinstance(node, renpy.ast.Scene):
            prefix = "scene"
        else:
            prefix = "show"
        if not name:
            return prefix
        return prefix + " " + " ".join(name)

    def _parse_imspec(imspec):
        if imspec is None:
            return None
        if len(imspec) == 7:
            name, expression, tag, _at_list, layer, _zorder, _behind = imspec
        elif len(imspec) == 6:
            name, expression, tag, _at_list, layer, _zorder = imspec
        else:
            name, _at_list, layer = imspec
            expression = None
            tag = None
        return name, expression, tag, layer

    def _resolve_image(state, name, tag, layer):
        if not name:
            return None, None
        if not isinstance(name, tuple):
            name = tuple(name.split())
        if tag is None:
            tag = name[0]
        layer = renpy.exports.default_layer(layer, tag or name)

        info = renpy.display.image.ShownImageInfo()
        for (key_layer, key_tag), attrs in state.items():
            info.attributes[(key_layer, key_tag)] = tuple(attrs)

        try:
            resolved = info.apply_attributes(layer, tag, name)
        except Exception:
            resolved = None

        if resolved is None:
            resolved = name

        return resolved, layer

    def _resolve_label(node):
        try:
            if isinstance(node, renpy.ast.Call):
                if node.expression:
                    return None
                label = node.label
                if isinstance(label, _STRING_TYPES) and label.startswith("."):
                    label = node.global_label + label
                return renpy.game.script.lookup_or_none(label)
            if isinstance(node, renpy.ast.Jump):
                if node.expression:
                    return None
                target = node.target
                if isinstance(target, _STRING_TYPES) and target.startswith("."):
                    target = node.global_label + target
                return renpy.game.script.lookup_or_none(target)
        except Exception:
            return None
        return None

    def _usage(node, resolved, layer, kind, game_root):
        return {
            "script": _script_rel(getattr(node, "filename", ""), game_root),
            "line": int(getattr(node, "linenumber", 0) or 0),
            "statement": _node_statement(node, resolved),
            "image_name": list(resolved),
            "attributes": list(resolved[1:]),
            "tag": resolved[0],
            "layer": layer,
            "kind": kind,
        }

    def _apply_show(node, state, game_root):
        parsed = _parse_imspec(node.imspec)
        if parsed is None:
            return dict(state), None
        name, expression, tag, layer = parsed
        if expression is not None:
            return dict(state), None

        resolved, layer = _resolve_image(state, name, tag, layer)
        if resolved is None:
            return dict(state), None

        new_state = dict(state)
        new_state[(layer, resolved[0])] = tuple(resolved[1:])
        return new_state, _usage(node, resolved, layer, "show", game_root)

    def _apply_scene(node, state, game_root):
        new_state = dict(state)
        scene_layer = node.layer or "master"
        for key_layer, key_tag in list(new_state.keys()):
            if key_layer == scene_layer:
                del new_state[(key_layer, key_tag)]

        if node.imspec is None:
            return new_state, None

        parsed = _parse_imspec(node.imspec)
        if parsed is None:
            return new_state, None
        name, expression, tag, layer = parsed
        if expression is not None:
            return new_state, None

        resolved, layer = _resolve_image(new_state, name, tag, layer)
        if resolved is None:
            return new_state, None

        new_state[(layer, resolved[0])] = tuple(resolved[1:])
        return new_state, _usage(node, resolved, layer, "scene", game_root)

    def _apply_hide(node, state):
        parsed = _parse_imspec(node.imspec)
        if parsed is None:
            return dict(state)
        name, _expression, tag, layer = parsed
        if tag is None and name:
            tag = name[0]
        layer = renpy.exports.default_layer(
            layer, tag or (name[0] if name else None)
        )
        new_state = dict(state)
        new_state.pop((layer, tag), None)
        return new_state

    def _roots():
        """Return every label node, in deterministic source-name order."""
        namemap = renpy.game.script.namemap
        roots = []
        label_names = [
            name for name in namemap.keys() if isinstance(name, _STRING_TYPES)
        ]
        for name in sorted(label_names):
            roots.append(namemap[name])
        return roots

    def _dedupe_usages(usages):
        seen = set()
        unique = []
        for usage in usages:
            key = (
                usage["script"],
                usage["line"],
                usage["statement"],
                tuple(usage["image_name"]),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(usage)
        return unique

    def _walk(roots, game_root, max_states, max_depth=6):
        """Linear, path-insensitive walk over every label.

        Show/scene statements are applied in source order, so each usage sees
        the sticky attributes established by the statements before it.  Branch
        bodies are all entered in order, which keeps the walk linear in the
        number of statements instead of exponential in branch nesting.  Called
        labels are inlined (bounded depth) so their show statements are
        attributed to the calling state.

        Returns ``(usages, stats)``.
        """
        usages = []
        visited = set()
        stack = []
        stats = {
            "roots": len(roots),
            "processed": 0,
            "truncated": False,
            "show_usages": 0,
            "scene_usages": 0,
            "max_depth": max_depth,
        }

        for root in roots:
            stack.append((root, {}, 0))

        processed = 0
        while stack:
            node, state, depth = stack.pop()
            if node is None:
                continue
            if processed >= max_states:
                stats["truncated"] = True
                break
            processed += 1

            key = (id(node), depth)
            if key in visited:
                continue
            visited.add(key)

            if isinstance(node, renpy.ast.Show):
                new_state, usage = _apply_show(node, state, game_root)
                if usage is not None:
                    usages.append(usage)
                    stats["show_usages"] += 1
                stack.append((node.next, new_state, depth))
                continue

            if isinstance(node, renpy.ast.Scene):
                new_state, usage = _apply_scene(node, state, game_root)
                if usage is not None:
                    usages.append(usage)
                    stats["scene_usages"] += 1
                stack.append((node.next, new_state, depth))
                continue

            if isinstance(node, renpy.ast.Hide):
                stack.append((node.next, _apply_hide(node, state), depth))
                continue

            if isinstance(node, renpy.ast.If):
                for _condition, block in node.entries:
                    if block:
                        stack.append((block[0], dict(state), depth))
                stack.append((node.next, dict(state), depth))
                continue

            if isinstance(node, renpy.ast.Menu):
                for _label, _condition, block in node.items:
                    if block:
                        stack.append((block[0], dict(state), depth))
                stack.append((node.next, dict(state), depth))
                continue

            if isinstance(node, renpy.ast.While):
                if node.block:
                    stack.append((node.block[0], dict(state), depth))
                stack.append((node.next, dict(state), depth))
                continue

            if isinstance(node, renpy.ast.Call):
                target = _resolve_label(node)
                if target is not None and depth < max_depth:
                    stack.append((target, dict(state), depth + 1))
                stack.append((node.next, dict(state), depth))
                continue

            if isinstance(node, renpy.ast.Jump):
                target = _resolve_label(node)
                if target is not None:
                    stack.append((target, dict(state), depth))
                continue

            if isinstance(node, renpy.ast.Return):
                continue

            stack.append((node.next, dict(state), depth))

        stats["processed"] = processed
        return _dedupe_usages(usages), stats

    # ------------------------------------------------------------------ #
    # Layered images and synthetic states
    # ------------------------------------------------------------------ #

    def _is_layered_image(value):
        return (
            value is not None
            and value.__class__.__name__ == "LayeredImage"
            and hasattr(value, "attributes")
            and hasattr(value, "layers")
        )

    def _layered_registry():
        result = {}
        for name, value in list(renpy.display.image.images.items()):
            if not name:
                continue
            if _is_layered_image(value):
                result[name[0]] = (name, value)
        return result

    def _find_image_node(tag):
        try:
            statements = renpy.game.script.all_stmts
        except Exception:
            statements = []
        for node in statements or []:
            if not isinstance(node, renpy.ast.Image):
                continue
            if len(node.imgname) == 1 and node.imgname[0] == tag:
                return node
        return None

    def _definition(node, game_root):
        if node is None:
            return None
        return {
            "script": _script_rel(getattr(node, "filename", ""), game_root),
            "line": int(getattr(node, "linenumber", 0) or 0),
        }

    def _layered_metadata(tag, image):
        groups = {}
        attributes = []
        defaults = []
        for attribute in image.attributes:
            name = attribute.attribute
            if name not in attributes:
                attributes.append(name)
            if attribute.group is not None:
                bucket = groups.setdefault(attribute.group, [])
                if name not in bucket:
                    bucket.append(name)
            if attribute.default:
                defaults.append(name)

        return {
            "name": tag,
            "groups": groups,
            "attributes": attributes,
            "defaults": defaults,
            "layer_count": len(getattr(image, "layers", [])),
            "image_format": getattr(image, "image_format", None),
            "has_attribute_function": getattr(image, "attribute_function", None)
            is not None,
        }

    def _synthetic_states(image, limit):
        groups = {}
        ungrouped = []
        for attribute in image.attributes:
            name = attribute.attribute
            if attribute.group is None:
                if name not in ungrouped:
                    ungrouped.append(name)
            else:
                bucket = groups.setdefault(attribute.group, [])
                if name not in bucket:
                    bucket.append(name)

        choices = []
        for group_name in sorted(groups.keys()):
            choices.append([None] + groups[group_name])
        for name in sorted(ungrouped):
            choices.append([None, name])

        total = 1
        for choice in choices:
            total *= len(choice)

        if total <= limit:
            indices = list(range(total))
        else:
            indices = [index * total // limit for index in range(limit)]

        states = []
        for index in indices:
            selected = []
            cursor = index
            for choice in choices:
                count = len(choice)
                pick = choice[cursor % count]
                cursor //= count
                if pick:
                    selected.append(pick)
            states.append(tuple(selected))
        return states

    # ------------------------------------------------------------------ #
    # Conditions
    # ------------------------------------------------------------------ #

    def _condition_texts(image):
        texts = []
        for layer in getattr(image, "layers", []):
            condition = getattr(layer, "condition", None)
            if isinstance(condition, _STRING_TYPES):
                texts.append(condition)
            conditions = getattr(layer, "conditions", None)
            if conditions:
                for item in conditions:
                    value = getattr(item, "condition", None)
                    if isinstance(value, _STRING_TYPES):
                        texts.append(value)
        return texts

    def _condition_variables(image, defaults):
        names = set()
        for text in _condition_texts(image):
            for match in _IDENT_RE.finditer(text):
                name = match.group(0)
                if name in _KEYWORDS:
                    continue
                if name not in defaults:
                    continue
                value = defaults[name]
                if isinstance(value, (bool, int, float)) or isinstance(
                    value, _STRING_TYPES
                ):
                    names.add(name)
        return sorted(names)

    def _parse_literal(text):
        text = text.strip()
        if text == "True":
            return True
        if text == "False":
            return False
        if text == "None":
            return None
        if text[:1] in ("'", '"') and text[-1:] == text[:1]:
            return text[1:-1]
        try:
            if "." in text:
                return float(text)
            return int(text)
        except Exception:
            return text

    def _condition_assignments(image, defaults, limit):
        names = _condition_variables(image, defaults)
        if not names:
            return []

        texts = _condition_texts(image)
        values = {}
        for name in names:
            bucket = [defaults[name]]
            for text in texts:
                for match in _EQ_RE.finditer(text):
                    if match.group(1) != name:
                        continue
                    value = _parse_literal(match.group(2))
                    if value not in bucket:
                        bucket.append(value)

            default = defaults[name]
            if isinstance(default, bool):
                if (not default) not in bucket:
                    bucket.append(not default)
            elif isinstance(default, int):
                for delta in (-1, 1):
                    value = default + delta
                    if value not in bucket:
                        bucket.append(value)
            values[name] = bucket

        result = []
        seen = set()

        def add(environment):
            key = tuple(
                sorted((name, repr(value)) for name, value in environment.items())
            )
            if key in seen:
                return
            seen.add(key)
            result.append(environment)

        for name in names:
            for value in values[name]:
                if value == defaults[name]:
                    continue
                add({name: value})
                if len(result) >= limit:
                    return result

        total = 1
        for name in names:
            total *= len(values[name])

        if total > 1:
            count = min(limit, total)
            for index in range(count):
                cursor = index * total // count
                environment = {}
                for name in names:
                    bucket = values[name]
                    environment[name] = bucket[cursor % len(bucket)]
                    cursor //= len(bucket)
                if any(
                    environment[name] != defaults[name] for name in names
                ):
                    add(environment)
                if len(result) >= limit:
                    break

        return result[:limit]

    # ------------------------------------------------------------------ #
    # Layer / source inspection
    # ------------------------------------------------------------------ #

    def _displayable_sources(displayable, seen=None):
        if seen is None:
            seen = set()
        if displayable is None:
            return []

        identity = id(displayable)
        if identity in seen:
            return []
        seen.add(identity)

        sources = []
        filename = getattr(displayable, "filename", None)
        if isinstance(filename, _STRING_TYPES):
            sources.append(filename.replace("\\", "/"))

        for attribute in ("child", "target", "what"):
            child = getattr(displayable, attribute, None)
            if child is not None:
                sources.extend(_displayable_sources(child, seen))

        children = getattr(displayable, "children", None)
        if children:
            for child in children:
                sources.extend(_displayable_sources(child, seen))

        return sources

    def _dedupe_sources(sources):
        ordered = []
        for source in sources:
            if source and source not in ordered:
                ordered.append(source)
        return ordered

    def _eval_condition(condition):
        """Evaluate one layer condition without hiding evaluation failures.

        Returns ``(value, error)``.  ``error`` is ``None`` for a successful
        evaluation and contains the original exception details otherwise.
        """
        if not isinstance(condition, _STRING_TYPES) or not condition.strip():
            error = "condition evaluation failed: condition must be a non-empty string"
            return False, error
        try:
            return bool(renpy.python.py_eval(condition)), None
        except Exception as exc:
            error = "condition evaluation failed for {0!r}: {1}: {2}".format(
                condition,
                exc.__class__.__name__,
                exc,
            )
            return False, error

    def _source_entry(
        sources,
        attribute,
        group,
        conditions,
        unresolved=(),
        unresolved_errors=(),
    ):
        ordered = []
        for source in sources:
            if source and source not in ordered:
                ordered.append(source)
        return {
            "sources": ordered,
            "attribute": attribute,
            "group": group,
            "conditions": list(conditions),
            "unresolved": list(unresolved),
            "unresolved_errors": list(unresolved_errors),
        }

    def _selected_layers(image, attributes):
        """Return selected layer provenance and condition diagnostics.

        Conditions are evaluated against the store as it stands when this is
        called, so the caller must apply any condition assignments first.
        Evaluation failures are returned instead of being interpreted as false.
        """
        attributes = set(attributes)
        result = []
        errors = []

        for layer in getattr(image, "layers", []):
            cls = layer.__class__.__name__

            if cls == "Attribute":
                attribute = getattr(layer, "attribute", None)
                if attribute is not None and attribute not in attributes:
                    continue
                try:
                    if not layer.check(attributes):
                        continue
                except Exception as exc:
                    errors.append(
                        "layer attribute check failed: {0}: {1}".format(
                            exc.__class__.__name__,
                            exc,
                        )
                    )
                    continue
                sources = _displayable_sources(getattr(layer, "image", None))
                result.append(
                    _source_entry(
                        sources, attribute, getattr(layer, "group", None), ()
                    )
                )

            elif cls == "Always":
                try:
                    if not layer.check(attributes):
                        continue
                except Exception as exc:
                    errors.append(
                        "layer check failed: {0}: {1}".format(
                            exc.__class__.__name__,
                            exc,
                        )
                    )
                    continue
                sources = _displayable_sources(getattr(layer, "image", None))
                result.append(_source_entry(sources, None, None, ()))

            elif cls == "Condition":
                try:
                    if not layer.check(attributes):
                        continue
                except Exception as exc:
                    errors.append(
                        "condition layer check failed: {0}: {1}".format(
                            exc.__class__.__name__,
                            exc,
                        )
                    )
                    continue
                condition = getattr(layer, "condition", None)
                value, error = _eval_condition(condition)
                if error is not None:
                    errors.append(error)
                    continue
                if not value:
                    continue
                sources = _displayable_sources(getattr(layer, "image", None))
                result.append(_source_entry(sources, None, None, (condition,)))

            elif cls == "ConditionGroup":
                # An if/elif/else chain shows only its first true branch.  An
                # evaluation error stops the group rather than being treated
                # as a false branch.
                for condition_layer in getattr(layer, "conditions", []):
                    try:
                        if not condition_layer.check(attributes):
                            continue
                    except Exception as exc:
                        errors.append(
                            "condition group check failed: {0}: {1}".format(
                                exc.__class__.__name__,
                                exc,
                            )
                        )
                        break
                    condition = getattr(condition_layer, "condition", None)
                    value, error = _eval_condition(condition)
                    if error is not None:
                        errors.append(error)
                        break
                    if not value:
                        continue
                    sources = _displayable_sources(
                        getattr(condition_layer, "image", None)
                    )
                    result.append(_source_entry(sources, None, None, (condition,)))
                    break

            else:
                try:
                    displayable = layer.get_displayable(attributes)
                except Exception:
                    displayable = None
                if displayable is None:
                    continue
                sources = _displayable_sources(displayable)
                result.append(
                    _source_entry(
                        sources,
                        getattr(layer, "attribute", None),
                        getattr(layer, "group", None),
                        (),
                    )
                )

        return result, errors

    # ------------------------------------------------------------------ #
    # State construction
    # ------------------------------------------------------------------ #

    def _state_image_name(state):
        if state["kind"] == "image":
            return tuple(state["attributes"])
        return (state["tag"],) + tuple(state["attributes"])

    def _candidate_id(state):
        parts = [state["kind"], state["tag"], " ".join(state["attributes"])]
        for name, value in sorted(state["condition_assignments"].items()):
            parts.append("{0}={1!r}".format(name, value))
        return "|".join(parts)

    def _build_states(config, usages, layered, defaults):
        modes = config.get("modes") or ["cfg", "layered_all", "conditions"]
        max_synthetic = int(config.get("max_synthetic_states") or 1024)
        max_conditions = int(config.get("max_condition_assignments") or 64)
        include = config.get("include_layered_images")
        include_set = set(include) if include else None

        states = {}

        def add(kind, tag, attributes, environment, usage, synthetic=False):
            key = (
                kind,
                tag,
                tuple(attributes),
                tuple(
                    sorted(
                        (name, repr(value))
                        for name, value in environment.items()
                    )
                ),
            )
            entry = states.get(key)
            if entry is None:
                entry = {
                    "kind": kind,
                    "tag": tag,
                    "attributes": tuple(attributes),
                    "condition_assignments": dict(environment),
                    "synthetic": synthetic,
                    "usages": [],
                }
                states[key] = entry
            if usage is not None:
                entry["usages"].append(usage)
            return entry

        for usage in usages:
            tag = usage["tag"]
            if tag in layered:
                if include_set is not None and tag not in include_set:
                    continue
                add("layeredimage", tag, usage["attributes"], {}, usage)
            else:
                add("image", tag, usage["image_name"], {}, usage)

        if "layered_all" in modes:
            for tag in sorted(layered.keys()):
                if include_set is not None and tag not in include_set:
                    continue
                image = layered[tag][1]
                for attributes in _synthetic_states(image, max_synthetic):
                    add("layeredimage", tag, attributes, {}, None, synthetic=True)

        if "conditions" in modes:
            for tag in sorted(layered.keys()):
                if include_set is not None and tag not in include_set:
                    continue
                image = layered[tag][1]
                # Condition variants build on the attribute sets the CFG
                # actually discovered; layering them on every synthetic
                # combination would multiply the two caps into millions of
                # renders.
                base_entries = [
                    entry
                    for entry in states.values()
                    if entry["kind"] == "layeredimage"
                    and entry["tag"] == tag
                    and not entry["synthetic"]
                    and not entry["condition_assignments"]
                ]
                base_attributes = sorted(
                    set(entry["attributes"] for entry in base_entries)
                )
                if not base_attributes:
                    base_attributes = [tuple()]
                base_usages = []
                for entry in base_entries:
                    base_usages.extend(entry["usages"])

                for attributes in base_attributes:
                    for environment in _condition_assignments(
                        image, defaults, max_conditions
                    ):
                        entry = add(
                            "layeredimage", tag, attributes, environment, None
                        )
                        for usage in base_usages:
                            entry["usages"].append(usage)

        result = list(states.values())
        result.sort(
            key=lambda entry: (
                entry["kind"],
                entry["tag"],
                entry["attributes"],
                tuple(sorted(entry["condition_assignments"].items())),
            )
        )
        return result

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    # Byte-order signatures for the 32-bit layouts SDL produces.  ``masks`` and
    # ``shifts`` are in (R, G, B, A) order.
    _RGBA_MASKS = (255, 65280, 16711680, 4278190080)
    _RGBA_SHIFTS = (0, 8, 16, 24)
    _BGRA_MASKS = (16711680, 65280, 255, 4278190080)
    _BGRA_SHIFTS = (16, 8, 0, 24)

    def _surface_rows(surface, row_bytes, height):
        """Return the tightly-packed pixel rows of ``surface`` as bytes."""
        address = surface._pixels_address
        pitch = surface.get_pitch()
        if pitch == row_bytes:
            return ctypes.string_at(address, row_bytes * height)
        chunks = []
        for row in range(height):
            chunks.append(ctypes.string_at(address + row * pitch, row_bytes))
        return b"".join(chunks)

    def _surface_rgba(surface):
        """Return canonical RGBA bytes for ``surface``.

        The software renderer hands back 32-bit surfaces whose byte order
        depends on the SDL video format (BGRA on Windows).  The hash must be
        canonical RGBA, so the R and B planes are swapped with a C-speed slice
        assignment instead of a per-pixel loop.
        """
        width, height = surface.get_size()
        row_bytes = width * 4
        pitch = surface.get_pitch()
        if pitch < row_bytes:
            return _surface_rgba_slow(surface)

        masks = tuple(surface.get_masks())
        shifts = tuple(surface.get_shifts())

        try:
            raw = _surface_rows(surface, row_bytes, height)
        except Exception:
            return _surface_rgba_slow(surface)

        if masks == _RGBA_MASKS and shifts == _RGBA_SHIFTS:
            return raw

        if masks == _BGRA_MASKS and shifts == _BGRA_SHIFTS:
            buffer = bytearray(raw)
            buffer[0::4], buffer[2::4] = buffer[2::4], buffer[0::4]
            return bytes(buffer)

        return _surface_rgba_slow(surface)

    def _surface_rgba_slow(surface):
        width, height = surface.get_size()
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                color = surface.get_at((x, y))
                pixels.extend(
                    struct.pack("BBBB", color[0], color[1], color[2], color[3])
                )
        return bytes(pixels)

    def _surface_hash(surface):
        width, height = surface.get_size()
        digest = hashlib.sha256()
        digest.update(("{0}x{1}:".format(width, height)).encode("ascii"))
        digest.update(_surface_rgba(surface))
        return digest.hexdigest()

    _MISSING_NAME_RE = re.compile(
        r"name '([A-Za-z_]\w*)' is not defined"
    )

    # Neutral seeds tried, in order, for condition variables that were never
    # given a ``default`` value anywhere in the game scripts.
    _NEUTRAL_SEEDS = [0, False, ""]

    def _render_one(
        image_name,
        environment,
        defaults,
        layered_image,
        attributes,
        render_dir,
        saved,
        screen_width,
        screen_height,
        timings,
    ):
        """Render one state and return its content and diagnostic provenance.

        Condition variables are seeded from ``defaults`` and then overridden by
        ``environment`` for the duration of the render, so that layered-image
        conditions and ConditionSwitch branches pick the intended variants even
        when a variable was never assigned in-game.
        """
        attempts = list(_NEUTRAL_SEEDS)
        extra = {}
        while True:
            try:
                return _render_attempt(
                    image_name,
                    environment,
                    defaults,
                    extra,
                    layered_image,
                    attributes,
                    render_dir,
                    saved,
                    screen_width,
                    screen_height,
                    timings,
                )
            except Exception:
                match = _MISSING_NAME_RE.search(traceback.format_exc())
                if match is None or not attempts:
                    raise
                extra[match.group(1)] = attempts.pop(0)

    def _render_attempt(
        image_name,
        environment,
        defaults,
        extra,
        layered_image,
        attributes,
        render_dir,
        saved,
        screen_width,
        screen_height,
        timings,
    ):
        store = renpy.store
        previous = {}

        merged = dict(defaults or {})
        merged.update(environment or {})
        merged.update(extra or {})

        try:
            started = _now()
            for name, value in merged.items():
                previous[name] = getattr(store, name, _MISSING)
                setattr(store, name, value)

            try:
                displayable = renpy.display.image.ImageReference(tuple(image_name))
            except Exception:
                displayable = renpy.easy.displayable(" ".join(image_name))

            try:
                tree = renpy.display.render.render(
                    displayable, screen_width, screen_height, 0.0, 0.0
                )
            except Exception as exc:
                # A condition can raise while Ren'Py builds the displayable.
                # Re-check the layer conditions so that diagnostic survives
                # the failed render instead of being lost with the exception.
                if layered_image is not None:
                    try:
                        _layers, condition_errors = _selected_layers(
                            layered_image,
                            attributes or (),
                        )
                        if condition_errors:
                            setattr(
                                exc,
                                "composite_unresolved_errors",
                                list(condition_errors),
                            )
                    except Exception:
                        pass
                raise
            timings["render"] += _now() - started
            width = int(tree.width)
            height = int(tree.height)

            layers = []
            sources = []
            unresolved_errors = []
            if layered_image is not None:
                layers, unresolved_errors = _selected_layers(
                    layered_image,
                    attributes or (),
                )
            else:
                sources = _dedupe_sources(
                    _displayable_sources(getattr(displayable, "target", displayable))
                )

            if width <= 0 or height <= 0:
                # Nothing is visible for this state (every layer hidden).
                return None, 0, 0, None, layers, sources, unresolved_errors

            canvas = renpy.display.render.Render(width, height)
            canvas.blit(tree, (0, 0), focus=False)
            surface = renpy.display.draw.render_to_texture(canvas, True)

            actual = surface.get_size()
            if actual != (width, height):
                raise RuntimeError(
                    "surface size {0!r} does not match {1!r}".format(
                        actual, (width, height)
                    )
                )

            hash_started = _now()
            content_hash = _surface_hash(surface)
            timings["hash"] += _now() - hash_started

            if len(saved) <= 1 and not _STATE.get("format_logged"):
                _STATE["format_logged"] = True
                _progress(
                    "surface format size={0} pitch={1} masks={2} shifts={3} "
                    "flags={4}".format(
                        (surface.get_width(), surface.get_height()),
                        surface.get_pitch(),
                        tuple(surface.get_masks()),
                        tuple(surface.get_shifts()),
                        surface.get_flags(),
                    )
                )

            if content_hash not in saved:
                save_started = _now()
                path = os.path.join(render_dir, content_hash + ".png")
                _pygame().image.save(surface, path)
                timings["save"] += _now() - save_started
                saved.add(content_hash)

            return (
                content_hash,
                width,
                height,
                content_hash + ".png",
                layers,
                sources,
                unresolved_errors,
            )

        finally:
            for name, value in previous.items():
                if value is _MISSING:
                    try:
                        delattr(store, name)
                    except Exception:
                        pass
                else:
                    try:
                        setattr(store, name, value)
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # Characters
    # ------------------------------------------------------------------ #

    def _characters():
        result = {}
        for name, value in list(vars(renpy.store).items()):
            try:
                if not isinstance(value, renpy.character.ADVCharacter):
                    continue
            except Exception:
                continue

            display = getattr(value, "name", None)
            if not isinstance(display, _STRING_TYPES):
                display = None

            result[name] = {
                "display_name": display,
                "image_tag": getattr(value, "image_tag", None),
            }
        return result

    # ------------------------------------------------------------------ #
    # Command entry point
    # ------------------------------------------------------------------ #

    def _run():
        config = _load_config()
        game_root = config.get("game_root") or ""
        _STATE["progress_path"] = config.get("progress_path") or None

        render_dir = config.get("render_dir")
        if not render_dir:
            raise RuntimeError("native compose config is missing render_dir")
        if not os.path.isdir(render_dir):
            os.makedirs(render_dir)

        screen_width, screen_height = _setup_display(config)

        defaults = config.get("defaults") or {}
        layered = _layered_registry()
        states_path = config.get("states_path") or None
        signature = _states_signature(config)
        cached = _load_states(states_path, signature)
        if cached is None:
            roots = _roots()
            usages, walk_stats = _walk(
                roots,
                game_root,
                int(config.get("max_cfg_states") or 1000000),
                int(config.get("max_cfg_depth") or 6),
            )
            states = _build_states(config, usages, layered, defaults)
            if states_path:
                _save_states(states_path, signature, states, walk_stats)
        else:
            states, walk_stats = cached

        state_counts = {}
        for state in states:
            state_counts[state["kind"]] = state_counts.get(state["kind"], 0) + 1

        offset = int(config.get("state_offset") or 0)
        limit = int(config.get("max_render_states") or 4096)
        total = len(states)
        selected = states[offset : offset + limit]

        saved = set()
        candidates = []
        timings = {"render": 0.0, "hash": 0.0, "save": 0.0}
        empty = 0
        batch_start = _now()

        _progress(
            "batch {0} start offset={1} selected={2} total={3}".format(
                int(config.get("batch_index") or 0), offset, len(selected), total
            )
        )

        for index, state in enumerate(selected):
            image_name = _state_image_name(state)
            if (index + 1) % 25 == 0:
                _progress(
                    "render {0}/{1} elapsed={2:.1f}s render={3:.1f}s hash={4:.1f}s "
                    "save={5:.1f}s".format(
                        index + 1,
                        len(selected),
                        _now() - batch_start,
                        timings["render"],
                        timings["hash"],
                        timings["save"],
                    )
                )
            error = None
            content_hash = None
            width = 0
            height = 0
            render_file = None
            layers = []
            sources = []
            unresolved_errors = []

            layered_image = None
            if state["kind"] == "layeredimage" and state["tag"] in layered:
                layered_image = layered[state["tag"]][1]

            try:
                (
                    content_hash,
                    width,
                    height,
                    render_file,
                    layers,
                    sources,
                    unresolved_errors,
                ) = _render_one(
                    image_name,
                    state["condition_assignments"],
                    defaults,
                    layered_image,
                    state["attributes"],
                    render_dir,
                    saved,
                    screen_width,
                    screen_height,
                    timings,
                )
            except Exception as exc:
                error = traceback.format_exc()
                unresolved_errors = list(
                    getattr(exc, "composite_unresolved_errors", []) or []
                )
                _log(
                    "render failed for {0}: {1}".format(
                        " ".join(image_name), error
                    )
                )

            candidate = {
                "candidate_id": _candidate_id(state),
                "name": state["tag"],
                "kind": state["kind"],
                "attributes": list(state["attributes"]),
                "image_name": list(image_name),
                "condition_assignments": dict(state["condition_assignments"]),
                "content_hash": content_hash,
                "width": width,
                "height": height,
                "render_file": render_file,
                "layers": layers,
                "sources": sources,
                "unresolved_errors": list(unresolved_errors),
                "usages": _dedupe_usages(state["usages"]),
                "error": error,
            }

            candidates.append(candidate)
            if error is None and content_hash is None:
                empty += 1

            if (index + 1) % 250 == 0:
                renpy.exports.free_memory()

        renpy.exports.free_memory()

        layered_result = []
        for tag in sorted(layered.keys()):
            metadata = _layered_metadata(tag, layered[tag][1])
            metadata["definition"] = _definition(_find_image_node(tag), game_root)
            layered_result.append(metadata)

        image_definitions = {}
        for state in states:
            if state["kind"] != "image":
                continue
            tag = state["tag"]
            if tag in image_definitions:
                continue
            image_definitions[tag] = _definition(_find_image_node(tag), game_root)

        characters = _characters()

        result = {
            "protocol": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "batch": {
                "index": int(config.get("batch_index") or 0),
                "state_offset": offset,
                "rendered": len(candidates),
                "total_states": total,
                "next_offset": offset + len(selected)
                if offset + len(selected) < total
                else None,
                "renpy_version": getattr(
                    renpy, "version_only", getattr(renpy, "version", "unknown")
                ),
                "renderer": "sw",
                "screen": [screen_width, screen_height],
                "state_counts": state_counts,
                "walk": walk_stats,
                "empty_renders": empty,
                "layered_images": len(layered_result),
                "characters": len(characters),
            },
            "candidates": candidates,
            "layered_images": layered_result,
            "image_definitions": image_definitions,
            "characters": characters,
        }
        _write_result(result)

    def run_command():
        try:
            _run()
        except renpy.game.QuitException:
            raise
        except Exception:
            message = traceback.format_exc()
            _log(message)
            _write_error(message)
            raise renpy.game.QuitException(status=1)
        return False


init -998 python:
    renpy.arguments.register_command(
        "composite_export",
        store._composite_export.run_command,
        uses_display=True,
    )

    if renpy.game.args.command == "composite_export":
        renpy.config.save = False
        renpy.config.save_persistent = False

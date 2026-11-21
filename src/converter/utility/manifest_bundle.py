"""Cross-process locking and crash recovery for compose manifest bundles."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any

from .atomic import (
    AtomicWriteError,
    atomic_write_json,
    json_payload_digest,
    recover_transaction_journal,
)
from .manifest_paths import MANIFEST_CATEGORIES

BUNDLE_AUXILIARY_FILES: tuple[str, ...] = (
    "characters.json",
    "review_pending.json",
)
BUNDLE_JOURNAL_NAME = ".manifest-bundle.journal"
BUNDLE_OWNER_REGISTRY_NAME = ".manifest-bundle.owners.json"


class ManifestBundlePathError(AtomicWriteError):
    """Raised when a manifest path collides with a reserved bundle file."""


_LOCKS: dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()
_THREAD_STATE = threading.local()


def bundle_lock_path(
    manifest_index_path: str | Path,
    report_dir: str | Path | None = None,
) -> Path:
    """Return the cross-process lock-file path for one manifest bundle.

    The lock lives in the operating system's temporary directory so cleaning
    the converter output cannot remove an active lock.  Bundle identity is
    based on the report directory rather than the index filename: two index
    names in one report directory address the same category and auxiliary
    files and therefore must not publish concurrently.

    ``report_dir`` is optional for source compatibility.  When omitted, the
    manifest index's parent directory is used as the bundle scope.
    """
    scope = Path(report_dir) if report_dir is not None else Path(manifest_index_path).parent
    resolved = scope.resolve()
    digest = hashlib.sha256(
        os.path.normcase(str(resolved)).encode("utf-8")
    ).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"renpy-compose-manifest-{digest}.lock"


def game_script_lock_path(game_root: str | Path) -> Path:
    """Return the cross-process lock path for mutations of one game folder."""
    resolved = Path(game_root).resolve()
    digest = hashlib.sha256(
        os.path.normcase(str(resolved)).encode("utf-8")
    ).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"renpy-compose-game-{digest}.lock"


def bundle_journal_path(
    manifest_index_path: str | Path,
    report_dir: str | Path | None = None,
) -> Path:
    """Return the report-scoped durable journal path for one manifest bundle.

    All index names that share a report directory also share the canonical
    category and auxiliary files.  They therefore share one journal so a
    crash cannot strand a rollback record outside the directory that another
    publisher will update.  ``manifest_index_path`` remains in the signature
    for source compatibility; when *report_dir* is omitted, its parent is the
    bundle scope.
    """
    scope = Path(report_dir) if report_dir is not None else Path(manifest_index_path).parent
    return scope.resolve() / BUNDLE_JOURNAL_NAME


def bundle_owner_registry_path(report_dir: str | Path) -> Path:
    """Return the trusted owner-registry path for one report scope."""
    return Path(report_dir).resolve() / BUNDLE_OWNER_REGISTRY_NAME


def bundle_support_paths(report_dir: str | Path) -> tuple[Path, Path]:
    """Return the canonical journal and owner-registry paths for one scope."""
    scope = Path(report_dir).resolve()
    return (
        scope / BUNDLE_JOURNAL_NAME,
        scope / BUNDLE_OWNER_REGISTRY_NAME,
    )


def bundle_journal_metadata(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> dict[str, str]:
    """Return the scope metadata stored in a report-wide bundle journal."""
    return {
        "report_dir": str(Path(report_dir).resolve()),
        "index_path": str(_comparable_path(manifest_index_path)),
    }


def bundle_paths(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> list[Path]:
    """Return the stable files that make up one manifest bundle.

    The index is returned last because it is the bundle's commit marker.
    """
    index_path = Path(manifest_index_path)
    reports = Path(report_dir)
    paths = [reports / f"manifest_{category}.json" for category in MANIFEST_CATEGORIES]
    paths.extend(reports / name for name in BUNDLE_AUXILIARY_FILES)
    paths.append(index_path)
    return paths


def _resolved_path(path: Path) -> Path:
    """Return a fully resolved path without failing on an invalid symlink."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _paths_match(left: Path, right: Path) -> bool:
    """Return whether two paths name the same lexical or resolved location."""
    if _comparable_path(left) == _comparable_path(right):
        return True
    return _resolved_path(left) == _resolved_path(right)


def _participant_path_aliases(path: Path) -> list[Path]:
    """Return lexical and parent-resolved paths for one participant index.

    Atomic transactions intentionally preserve the lexical final path, but a
    directory junction or symlink in a parent directory can make the same
    participant appear under two absolute names.  Recovery must authorize both
    names when a legacy journal records the canonical target.
    """
    lexical = _comparable_path(path)
    try:
        resolved = _comparable_path(_resolved_path(path.parent) / path.name)
    except (OSError, RuntimeError):
        return [lexical]
    if resolved == lexical:
        return [lexical]
    return [lexical, resolved]


def validate_manifest_index_path(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> None:
    """Reject an index path that aliases a reserved bundle file.

    The index must remain distinct from every category/auxiliary file and from
    the shared journal and owner registry.  Both lexical and resolved paths
    are compared so a symlink cannot turn a reserved support file into the
    transaction commit marker.
    """
    index_path = Path(manifest_index_path)
    stable_paths = bundle_paths(index_path, report_dir)[:-1]
    reserved_paths = [*stable_paths, *bundle_support_paths(report_dir)]
    for reserved_path in reserved_paths:
        if _paths_match(index_path, reserved_path):
            raise ManifestBundlePathError(
                f"Manifest index path collides with a bundle file: {index_path}"
            )


def category_digest_map(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Return SHA-256 digests for category payloads in deterministic order."""
    return {
        str(category): json_payload_digest(payload)
        for category, payload in sorted(payloads.items())
    }


def _lock_key(path: Path) -> str:
    # ``normcase`` makes aliases such as differently-cased Windows paths share
    # one in-process lock as well as one operating-system lock.
    return os.path.normcase(str(path.resolve()))


def _thread_lock_for(key: str) -> Any:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _thread_depths() -> dict[str, int]:
    depths = getattr(_THREAD_STATE, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_STATE.depths = depths
    return depths


def _prepare_lock_file(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    return handle


def _try_lock(handle: Any, path: Path) -> bool:
    """Try to acquire an exclusive OS-level lock without blocking."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    fcntl_module: Any = fcntl
    try:
        flock = fcntl_module.flock
        lock_ex = fcntl_module.LOCK_EX
        lock_nb = fcntl_module.LOCK_NB
        flock(handle.fileno(), lock_ex | lock_nb)
    except OSError:
        return False
    return True


def _unlock(handle: Any) -> None:
    """Release an OS-level lock."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    fcntl_module: Any = fcntl
    try:
        flock = fcntl_module.flock
        lock_un = fcntl_module.LOCK_UN
        flock(handle.fileno(), lock_un)
    except OSError:
        pass


@contextmanager
def _exclusive_path_lock(
    lock_path: Path,
    *,
    timeout: float,
    description: str,
) -> Iterator[Path]:
    """Hold one reentrant, cross-process path lock."""
    key = _lock_key(lock_path)
    deadline = time.monotonic() + max(0.0, timeout)
    thread_lock = _thread_lock_for(key)
    try:
        acquired_thread = thread_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )
    except TimeoutError as exc:
        raise TimeoutError(f"Timed out waiting for {description}: {lock_path}") from exc
    if not acquired_thread:
        raise TimeoutError(f"Timed out waiting for {description}: {lock_path}")
    depths = _thread_depths()
    depth = depths.get(key, 0)
    handle: Any = None
    acquired = False
    try:
        if depth == 0:
            handle = _prepare_lock_file(lock_path)
            while True:
                if _try_lock(handle, lock_path):
                    acquired = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for {description}: {lock_path}"
                    )
                time.sleep(min(0.05, remaining))
        depths[key] = depth + 1
        try:
            yield lock_path
        finally:
            depths[key] = depth
            if depth == 0:
                depths.pop(key, None)
    finally:
        if handle is not None:
            if acquired:
                _unlock(handle)
            handle.close()
        thread_lock.release()


@contextmanager
def manifest_bundle_lock(
    manifest_index_path: str | Path,
    *,
    report_dir: str | Path | None = None,
    timeout: float = 30.0,
) -> Iterator[Path]:
    """Hold an exclusive, cross-process lock for one manifest bundle.

    The lock is reentrant within one thread.  This lets high-level operations
    (for example, interactive review) hold the bundle lock while calling
    lower-level helpers that also protect their own reads and writes.  When
    *report_dir* is supplied, all indexes in that directory share one lock
    because they address the same category and auxiliary files.
    """
    lock_path = bundle_lock_path(manifest_index_path, report_dir)
    with _exclusive_path_lock(
        lock_path,
        timeout=timeout,
        description="manifest bundle lock",
    ) as acquired_path:
        yield acquired_path


@contextmanager
def game_script_lock(
    game_root: str | Path,
    *,
    timeout: float = 30.0,
) -> Iterator[Path]:
    """Serialize apply/restore operations that mutate live game scripts."""
    lock_path = game_script_lock_path(game_root)
    with _exclusive_path_lock(
        lock_path,
        timeout=timeout,
        description="game script lock",
    ) as acquired_path:
        yield acquired_path


def _referenced_category_paths(
    manifest_index_path: Path,
    report_dir: Path,
) -> list[Path]:
    """Return report-contained category paths referenced by an index.

    Recovery may authorize custom category files only when the authenticated
    owner index names them and they resolve inside the report scope.  Relative
    references are resolved against the index directory, which permits an
    explicitly configured index outside the report directory.
    """
    if not manifest_index_path.is_file():
        return []
    try:
        payload = json.loads(manifest_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, Mapping):
        return []
    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, Mapping):
        return []

    report_root = report_dir.resolve()
    excluded_targets = {
        _comparable_path(manifest_index_path),
        _comparable_path(bundle_journal_path(manifest_index_path, report_dir)),
        _comparable_path(bundle_owner_registry_path(report_dir)),
    }
    paths: list[Path] = []
    for reference in raw_categories.values():
        if isinstance(reference, Mapping):
            reference = reference.get("path")
        if not isinstance(reference, str) or not reference.strip():
            continue
        if "\x00" in reference:
            continue

        native = Path(reference)
        windows = PureWindowsPath(reference)
        if windows.drive and not windows.root:
            continue
        if native.anchor or windows.anchor:
            if not native.anchor:
                continue
            candidate = native
        else:
            candidate = manifest_index_path.parent / Path(
                reference.replace("\\", "/")
            )

        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(report_root):
            continue
        if (
            resolved in excluded_targets
            or _comparable_path(resolved) in excluded_targets
        ):
            continue
        paths.append(resolved)
    return paths


def _comparable_path(path: str | Path) -> Path:
    """Return a normalized absolute path without resolving its final symlink."""
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _read_bundle_owner_paths(report_dir: Path) -> set[Path]:
    """Return manifest indexes registered as trusted owners in this scope.

    The registry is separate from the transaction journal.  Journal metadata
    is untrusted input and therefore cannot authorize itself by naming an
    arbitrary owner index.
    """
    registry_path = bundle_owner_registry_path(report_dir)
    if registry_path.is_symlink():
        raise AtomicWriteError(
            f"Invalid manifest bundle owner registry: {registry_path}"
        )
    if not registry_path.exists():
        return set()
    if not registry_path.is_file():
        raise AtomicWriteError(
            f"Invalid manifest bundle owner registry: {registry_path}"
        )
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicWriteError(
            f"Could not read manifest bundle owner registry {registry_path}: {exc}"
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        raise AtomicWriteError(
            f"Invalid manifest bundle owner registry: {registry_path}"
        )
    raw_paths = payload.get("index_paths")
    if not isinstance(raw_paths, list):
        raise AtomicWriteError(
            f"Invalid manifest bundle owner registry: {registry_path}"
        )
    owners: set[Path] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path:
            raise AtomicWriteError(
                f"Invalid manifest bundle owner registry: {registry_path}"
            )
        path = Path(raw_path)
        if not path.is_absolute():
            raise AtomicWriteError(
                f"Invalid manifest bundle owner registry: {registry_path}"
            )
        try:
            validate_manifest_index_path(path, report_dir)
        except ManifestBundlePathError as exc:
            raise AtomicWriteError(
                f"Invalid manifest bundle owner registry: {registry_path}"
            ) from exc
        owners.add(_comparable_path(path))
    return owners


def register_bundle_owner(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> None:
    """Register one index as a trusted participant in a report bundle scope.

    Publishers call this before creating a shared journal.  A peer may then
    recover an interrupted publication owned by that index, while a journal
    that merely names an unregistered path cannot expand the recovery
    allowlist.
    """
    reports_path = Path(report_dir).resolve()
    validate_manifest_index_path(manifest_index_path, reports_path)
    owner = _comparable_path(manifest_index_path)
    with manifest_bundle_lock(owner, report_dir=reports_path):
        owners = _read_bundle_owner_paths(reports_path)
        owners.add(owner)
        atomic_write_json(
            bundle_owner_registry_path(reports_path),
            {
                "version": 1,
                "index_paths": [
                    str(path)
                    for path in sorted(owners, key=lambda item: str(item).casefold())
                ],
            },
        )


def _legacy_bundle_journal_path(manifest_index_path: Path) -> Path:
    """Return the pre-Phase-2 index-local journal path for recovery."""
    return manifest_index_path.with_name(f".{manifest_index_path.name}.journal")


def _journal_payload(journal_path: Path) -> Mapping[str, Any] | None:
    """Return a journal payload when it is readable and structurally valid."""
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, Mapping)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        return None
    return payload


def _journal_target_paths(journal_path: Path) -> list[Path]:
    """Return target paths recorded in one transaction journal.

    A malformed journal is left for :func:`recover_transaction_journal` to
    report with its normal validation error.
    """
    payload = _journal_payload(journal_path)
    if payload is None:
        return []
    entries = payload.get("files")
    if not isinstance(entries, list):
        return []
    targets: list[Path] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_target = entry.get("path")
        if isinstance(raw_target, str) and raw_target:
            targets.append(_comparable_path(raw_target))
    return targets


def _path_is_within(path: Path, directory: Path) -> bool:
    """Return whether *path* is lexically within *directory*."""
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _without_bundle_support_paths(
    paths: list[Path],
    report_dir: Path,
) -> list[Path]:
    """Return paths that do not alias the shared journal or owner registry."""
    support_paths = bundle_support_paths(report_dir)
    return [
        path
        for path in paths
        if not any(_paths_match(path, support) for support in support_paths)
    ]


def _journal_is_committed(journal_path: Path) -> bool:
    """Return whether every recorded target already has its new digest."""
    payload = _journal_payload(journal_path)
    if payload is None:
        return False
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        return False

    raw_commit_path = payload.get("commit_path")
    raw_commit_digest = payload.get("commit_digest")
    if (raw_commit_path is None) != (raw_commit_digest is None):
        return False
    if raw_commit_path is not None and raw_commit_digest is not None:
        if not isinstance(raw_commit_path, str) or not isinstance(raw_commit_digest, str):
            return False
        final_entry = entries[-1]
        if not isinstance(final_entry, Mapping):
            return False
        final_target = final_entry.get("path")
        final_digest = final_entry.get("new_digest")
        if not isinstance(final_target, str) or not isinstance(final_digest, str):
            return False
        if _comparable_path(final_target) != _comparable_path(raw_commit_path):
            return False
        if final_digest.lower() != raw_commit_digest.lower():
            return False

    for entry in entries:
        if not isinstance(entry, Mapping):
            return False
        raw_target = entry.get("path")
        expected = entry.get("new_digest")
        if not isinstance(raw_target, str) or not isinstance(expected, str):
            return False
        try:
            actual = hashlib.sha256(
                _comparable_path(raw_target).read_bytes()
            ).hexdigest()
        except OSError:
            return False
        if actual != expected.lower():
            return False
    return True


def _foreign_journal_allowed_paths(
    journal_path: Path,
    report_dir: Path,
    trusted_index_path: Path,
) -> list[Path] | None:
    """Return recovery scope for a sibling journal, or ``None`` to skip it.

    A report directory may contain more than one index name, but all such
    indexes share the canonical category and auxiliary files.  A journal for
    a sibling index is therefore relevant to the report-wide bundle.  Its
    scope is still restricted: the sibling index and category files explicitly
    referenced by that index are allowed, in addition to the canonical bundle
    paths.  Treating every file below ``report_dir`` as recoverable would
    turn a malformed journal into an arbitrary-file write primitive.
    """
    journal_name = journal_path.name
    if not (journal_name.startswith(".") and journal_name.endswith(".journal")):
        return None

    foreign_index_name = journal_name[1:-len(".journal")]
    if not foreign_index_name or foreign_index_name in {".", ".."}:
        return None
    try:
        foreign_index = _comparable_path(
            journal_path.with_name(foreign_index_name)
        )
    except ValueError:
        return None
    report_scope = _comparable_path(report_dir)
    if not _path_is_within(foreign_index, report_scope):
        # This journal is not for an index in the current report directory.
        return None
    current_owner = _comparable_path(trusted_index_path)
    foreign_is_current = _paths_match(foreign_index, current_owner)
    if any(
        _paths_match(foreign_index, support)
        for support in bundle_support_paths(report_dir)
    ):
        # Legacy naming such as ``..manifest-bundle.owners.json.journal``
        # must not reinterpret a support file as a recoverable index.
        return None

    allowed = _without_bundle_support_paths(
        bundle_paths(foreign_index, report_dir),
        report_dir,
    )
    allowed.extend(_participant_path_aliases(foreign_index))
    for category_path in _referenced_category_paths(foreign_index, report_dir):
        comparable_category = _comparable_path(category_path)
        if (
            not foreign_is_current
            and _paths_match(comparable_category, current_owner)
        ):
            # A sibling journal may restore its own index, but not the current
            # consumer's index, even when the consumer used a path alias.
            continue
        allowed.append(comparable_category)

    # ``recover_transaction_journal`` performs the final membership check and
    # reports an unexpected target as an integrity error.  Do not broaden the
    # scope merely because a target happens to be below report_dir.
    return _without_bundle_support_paths(allowed, report_dir)


def _recovery_allowed_paths(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> list[Path]:
    """Return paths a bundle journal is permitted to restore."""
    index_path = Path(manifest_index_path)
    reports_path = Path(report_dir)
    paths = bundle_paths(index_path, reports_path)
    paths.extend(_participant_path_aliases(index_path))
    paths.extend(_referenced_category_paths(index_path, reports_path))
    return _without_bundle_support_paths(paths, reports_path)


def _shared_journal_allowed_paths(
    journal_path: Path,
    report_dir: Path,
    *,
    trusted_index_path: Path,
    trusted_owner_paths: set[Path],
) -> list[Path]:
    """Return recovery scope for the report-wide shared journal.

    New journals identify the report scope and the index that participated in
    the transaction.  The index must also be the journal's commit marker and
    must be the current trusted index or a participant registered in the
    report-scoped owner registry.  Journal metadata alone never expands the
    recovery allowlist.  Even an authenticated owner can restore only the
    stable bundle files, its own index, and report-contained category files
    referenced by that index.
    """
    payload = _journal_payload(journal_path)
    if payload is None:
        raise AtomicWriteError(
            f"Invalid atomic transaction journal metadata: {journal_path}"
        )

    metadata = payload.get("metadata")
    if metadata is None:
        raise AtomicWriteError(
            f"Invalid atomic transaction journal metadata: {journal_path}"
        )
    if not isinstance(metadata, Mapping):
        raise AtomicWriteError(
            f"Invalid atomic transaction journal metadata: {journal_path}"
        )

    raw_report_dir = metadata.get("report_dir")
    raw_index_path = metadata.get("index_path")
    if not isinstance(raw_report_dir, str) or not isinstance(raw_index_path, str):
        raise AtomicWriteError(
            f"Invalid atomic transaction journal metadata: {journal_path}"
        )
    if not Path(raw_report_dir).is_absolute() or not Path(raw_index_path).is_absolute():
        raise AtomicWriteError(
            f"Invalid atomic transaction journal metadata: {journal_path}"
        )
    if Path(raw_report_dir).resolve() != Path(report_dir).resolve():
        raise AtomicWriteError(
            f"Atomic transaction journal belongs to a different bundle: "
            f"{journal_path}"
        )

    index_target = _comparable_path(raw_index_path)
    current_owner = _comparable_path(trusted_index_path)
    owner_is_current = _paths_match(index_target, current_owner)
    owner_is_registered = any(
        _paths_match(index_target, owner) for owner in trusted_owner_paths
    )
    if not owner_is_current and not owner_is_registered:
        raise AtomicWriteError(
            "Atomic transaction journal owner index is not registered for this "
            f"report bundle: {raw_index_path}"
        )
    commit_path = payload.get("commit_path")
    if (
        not isinstance(commit_path, str)
        or not Path(commit_path).is_absolute()
        or not _paths_match(Path(commit_path), Path(raw_index_path))
    ):
        raise AtomicWriteError(
            f"Atomic transaction journal index does not match its commit marker: "
            f"{journal_path}"
        )

    allowed = bundle_paths(index_target, report_dir)
    allowed.extend(_participant_path_aliases(index_target))
    for category_path in _referenced_category_paths(
        Path(raw_index_path),
        report_dir,
    ):
        comparable_category = _comparable_path(category_path)
        if not owner_is_current and _paths_match(comparable_category, current_owner):
            # A foreign owner's category list must not authorize the peer's
            # index, even when the peer accessed it through a path alias.
            continue
        allowed.append(comparable_category)
    return _without_bundle_support_paths(allowed, report_dir)


def _recover_manifest_bundle_unlocked(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> bool:
    """Recover bundle journals while the caller holds its lock."""
    index_path = Path(manifest_index_path)
    reports_path = Path(report_dir)
    validate_manifest_index_path(index_path, reports_path)
    own_journal = _comparable_path(bundle_journal_path(index_path, reports_path))
    legacy_own_journal = _comparable_path(_legacy_bundle_journal_path(index_path))
    base_allowed = _recovery_allowed_paths(index_path, reports_path)

    # The report-wide journal is shared by every index in this directory.  The
    # legacy index-local journal is still scanned so an interrupted publication
    # from an older converter version can be recovered safely.
    legacy_candidates: set[Path] = {legacy_own_journal}
    for directory in (reports_path, index_path.parent):
        try:
            legacy_candidates.update(
                _comparable_path(path) for path in directory.glob(".*.journal")
            )
        except OSError:
            continue
    legacy_candidates.discard(own_journal)

    def _journal_order(path: Path) -> tuple[float, str]:
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return modified, path.name

    def _legacy_scope(journal: Path) -> list[Path] | None:
        if journal == legacy_own_journal:
            return base_allowed
        return _foreign_journal_allowed_paths(
            journal,
            reports_path,
            index_path,
        )

    def _discard_superseded(journals: list[Path]) -> None:
        for journal in journals:
            try:
                journal.unlink(missing_ok=True)
            except OSError as exc:
                raise AtomicWriteError(
                    f"Could not remove superseded manifest journal {journal}: {exc}"
                ) from exc

    # The shared journal is authoritative for the report scope.  If it exists,
    # recover it first and discard older legacy records instead of applying an
    # older snapshot underneath the newer transaction.
    if own_journal.is_file():
        trusted_owner_paths = _read_bundle_owner_paths(reports_path)
        allowed = _shared_journal_allowed_paths(
            own_journal,
            reports_path,
            trusted_index_path=index_path,
            trusted_owner_paths=trusted_owner_paths,
        )
        recover_transaction_journal(own_journal, allowed_paths=allowed)
        relevant_legacy: list[Path] = []
        for journal in legacy_candidates:
            if not journal.is_file():
                continue
            if _legacy_scope(journal) is not None:
                relevant_legacy.append(journal)
        _discard_superseded(relevant_legacy)
        return True

    # During migration, recover only the newest relevant legacy journal.  Any
    # older journal is superseded and must not roll the bundle back again.
    relevant_legacy = [
        journal
        for journal in legacy_candidates
        if journal.is_file() and _legacy_scope(journal) is not None
    ]
    if not relevant_legacy:
        return False
    ordered = sorted(relevant_legacy, key=_journal_order, reverse=True)
    newest = ordered[0]
    legacy_allowed = _legacy_scope(newest)
    if legacy_allowed is None:  # pragma: no cover - filtered above
        return False
    recover_transaction_journal(newest, allowed_paths=legacy_allowed)
    _discard_superseded(ordered[1:])
    return True


def recover_manifest_bundle(
    manifest_index_path: str | Path,
    report_dir: str | Path,
) -> bool:
    """Recover an interrupted manifest publication under the bundle lock.

    The journal is allowed to restore only the stable bundle files and
    category files explicitly referenced by the index.  A malformed or
    unrelated journal is reported as :class:`AtomicWriteError` rather than
    silently allowing consumers to read a mixed snapshot.  The helper is
    reentrant, so callers may hold the bundle lock while invoking it.
    """
    index_path = Path(manifest_index_path)
    validate_manifest_index_path(index_path, report_dir)
    with manifest_bundle_lock(index_path, report_dir=report_dir):
        return _recover_manifest_bundle_unlocked(index_path, report_dir)


__all__ = [
    "BUNDLE_AUXILIARY_FILES",
    "BUNDLE_JOURNAL_NAME",
    "BUNDLE_OWNER_REGISTRY_NAME",
    "AtomicWriteError",
    "ManifestBundlePathError",
    "bundle_journal_metadata",
    "bundle_journal_path",
    "bundle_lock_path",
    "bundle_owner_registry_path",
    "bundle_paths",
    "bundle_support_paths",
    "category_digest_map",
    "game_script_lock",
    "game_script_lock_path",
    "manifest_bundle_lock",
    "recover_manifest_bundle",
    "register_bundle_owner",
    "validate_manifest_index_path",
]

"""Atomic file replacement helpers shared by converter stages."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


class AtomicWriteError(RuntimeError):
    """Raised when an atomic JSON publication cannot be fully rolled back."""


def json_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the exact UTF-8 bytes used for an atomic JSON payload."""
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def json_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of an atomic JSON payload."""
    return hashlib.sha256(json_payload_bytes(payload)).hexdigest()


def _write_temporary_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Serialize *payload* to a temporary sibling of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(json_payload_bytes(payload).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _write_temporary_bytes(path: Path, payload: bytes) -> Path:
    """Write *payload* to a temporary sibling of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _restore_previous(target: Path, previous: bytes | None) -> None:
    """Atomically restore one target to its pre-publication contents."""
    if previous is None:
        target.unlink(missing_ok=True)
        return

    temporary_path = _write_temporary_bytes(target, previous)
    try:
        os.replace(temporary_path, target)
        _fsync_parent(target)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Atomically replace *path* with one JSON document."""
    target = Path(path)
    temporary_path: Path | None = None
    try:
        temporary_path = _write_temporary_json(target, payload)
        os.replace(temporary_path, target)
        _fsync_parent(target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def atomic_copy_file(source: str | Path, target: str | Path) -> Path:
    """Atomically copy one file to *target* without exposing partial bytes."""
    source_path = Path(source)
    target_path = Path(target)
    if not source_path.is_file():
        raise OSError(f"Cannot copy missing file: {source_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=target_path.parent,
            delete=False,
        ) as destination:
            temporary_path = Path(destination.name)
            with source_path.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, target_path)
        _fsync_parent(target_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target_path


def _resolved_path(path: str | Path) -> Path:
    """Return a normalized absolute path without following its final symlink.

    Bundle replacement uses ``os.replace`` on the named path itself.  Resolving
    that final component would therefore record the symlink target in a
    transaction journal and restore the wrong file after a crash.  Lexical
    normalization keeps ``a/../b.json`` and ``b.json`` equivalent while
    preserving the actual directory entry that the publication replaces.
    """
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _fsync_parent(path: Path) -> None:
    """Best-effort fsync of a directory after replacing one of its entries."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_temporary_siblings(path: Path) -> None:
    """Remove stale temporary siblings left by an interrupted atomic write."""
    pattern = f".{path.name}.*.tmp"
    try:
        candidates = list(path.parent.glob(pattern))
    except OSError:
        return
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _write_transaction_journal(
    journal_path: Path,
    targets: Iterable[Path],
    *,
    new_digests: Mapping[Path, str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Persist the pre-publication contents needed to undo a transaction.

    New journals also record the digest of every staged payload.  Those
    digests let recovery distinguish a transaction that reached its final
    commit marker from one that was interrupted part-way through publication.
    """
    target_list = [Path(target) for target in targets]
    digest_by_path: dict[Path, str] = {}
    if new_digests is not None:
        digest_by_path = {
            _resolved_path(path): digest for path, digest in new_digests.items()
        }

    entries: list[dict[str, object]] = []
    for target in target_list:
        path = Path(target)
        if path.exists() and not path.is_file():
            raise OSError(f"Cannot journal non-file transaction target: {path}")
        previous = path.read_bytes() if path.is_file() else None
        entry: dict[str, object] = {
            "path": str(_resolved_path(path)),
            "previous": (
                None
                if previous is None
                else base64.b64encode(previous).decode("ascii")
            ),
        }
        new_digest = digest_by_path.get(_resolved_path(path))
        if new_digest is not None:
            entry["new_digest"] = new_digest
        entries.append(entry)

    payload: dict[str, object] = {
        "version": 1,
        "files": entries,
    }
    if metadata is not None:
        payload["metadata"] = dict(metadata)
    if target_list and new_digests is not None:
        commit_target = target_list[-1]
        payload["commit_path"] = str(_resolved_path(commit_target))
        payload["commit_digest"] = digest_by_path.get(_resolved_path(commit_target))

    atomic_write_json(journal_path, payload)


def recover_transaction_journal(
    journal_path: str | Path,
    *,
    allowed_paths: Iterable[str | Path] | None = None,
) -> bool:
    """Restore files recorded in a pending transaction journal.

    A journal is deliberately retained until every target has been restored.
    If restoration fails, the journal remains available for a later recovery
    attempt and :class:`AtomicWriteError` reports the incomplete recovery.

    Args:
        journal_path: Path to the durable transaction journal.
        allowed_paths: Optional set of paths that may be restored.  This is
            used by bundle consumers to reject a tampered or unrelated
            journal.  Stale temporary siblings for these paths are also
            removed during recovery.

    Returns:
        ``True`` when a journal was found and fully recovered, otherwise
        ``False``.
    """
    path = Path(journal_path)
    _remove_temporary_siblings(path)

    allowed: set[Path] | None = None
    if allowed_paths is not None:
        allowed = {_resolved_path(candidate) for candidate in allowed_paths}
        # A process can die after staging a payload but before writing the
        # journal.  Remove those stale siblings as part of the next bundle
        # recovery, even when no journal is present.
        for candidate in allowed:
            _remove_temporary_siblings(candidate)

    if not path.is_file():
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicWriteError(
            f"Could not read atomic transaction journal {path}: {exc}"
        ) from exc

    if (
        not isinstance(payload, Mapping)
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
    ):
        raise AtomicWriteError(f"Invalid atomic transaction journal: {path}")

    entries = payload.get("files")
    if not isinstance(entries, list):
        raise AtomicWriteError(f"Invalid atomic transaction journal files: {path}")
    if not entries:
        raise AtomicWriteError(f"Invalid atomic transaction journal files: {path}")

    raw_commit_path = payload.get("commit_path")
    raw_commit_digest = payload.get("commit_digest")
    if (raw_commit_path is None) != (raw_commit_digest is None):
        raise AtomicWriteError(
            f"Invalid atomic transaction journal commit metadata: {path}"
        )
    commit_target: Path | None = None
    commit_digest: str | None = None
    if raw_commit_path is not None and raw_commit_digest is not None:
        if not isinstance(raw_commit_path, str) or not raw_commit_path:
            raise AtomicWriteError(
                f"Invalid atomic transaction journal commit metadata: {path}"
            )
        if (
            not isinstance(raw_commit_digest, str)
            or len(raw_commit_digest) != 64
            or any(
                char not in "0123456789abcdef"
                for char in raw_commit_digest.lower()
            )
        ):
            raise AtomicWriteError(
                f"Invalid atomic transaction journal commit metadata: {path}"
            )
        commit_target = _resolved_path(raw_commit_path)
        commit_digest = raw_commit_digest.lower()

    prepared: list[tuple[Path, bytes | None, str | None]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AtomicWriteError(f"Invalid atomic transaction journal entry: {path}")
        raw_target = entry.get("path")
        if not isinstance(raw_target, str) or not raw_target:
            raise AtomicWriteError(f"Invalid atomic transaction journal target: {path}")
        target = Path(raw_target)
        if allowed is not None and _resolved_path(target) not in allowed:
            raise AtomicWriteError(
                f"Atomic transaction journal contains an unexpected target: {target}"
            )
        previous = entry.get("previous")
        if previous is not None:
            if not isinstance(previous, str):
                raise AtomicWriteError(
                    f"Invalid atomic transaction journal contents: {path}"
                )
            try:
                previous_bytes = base64.b64decode(previous, validate=True)
            except (ValueError, TypeError) as exc:
                raise AtomicWriteError(
                    f"Invalid atomic transaction journal contents: {path}"
                ) from exc
        else:
            previous_bytes = None

        new_digest: str | None = None
        if "new_digest" in entry:
            raw_digest = entry.get("new_digest")
            if (
                not isinstance(raw_digest, str)
                or len(raw_digest) != 64
                or any(char not in "0123456789abcdef" for char in raw_digest.lower())
            ):
                raise AtomicWriteError(
                    f"Invalid atomic transaction journal digest: {path}"
                )
            new_digest = raw_digest.lower()
        prepared.append((target, previous_bytes, new_digest))
        _remove_temporary_siblings(target)

    if commit_target is not None and commit_digest is not None:
        final_target, _final_previous, final_digest = prepared[-1]
        if _resolved_path(final_target) != commit_target:
            raise AtomicWriteError(
                f"Atomic transaction journal commit marker is not the final "
                f"target: {path}"
            )
        if final_digest is None or final_digest != commit_digest:
            raise AtomicWriteError(
                f"Atomic transaction journal commit digest does not match its "
                f"final target: {path}"
            )

    # A journal written by the current transaction format includes the new
    # digest for every target.  If all targets already contain those bytes,
    # the final commit marker was reached and the transaction is complete;
    # retain the published bundle rather than rolling it back merely because
    # the journal could not be unlinked before the process stopped.
    if prepared and all(new_digest is not None for _, _, new_digest in prepared):
        committed = True
        for target, _previous, expected_digest in prepared:
            try:
                current_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                committed = False
                break
            if current_digest != expected_digest:
                committed = False
                break
        if committed:
            try:
                path.unlink()
                _fsync_parent(path)
            except OSError as exc:
                raise AtomicWriteError(
                    f"Atomic transaction recovery completed but the journal could not be "
                    f"removed: {path}: {exc}"
                ) from exc
            return True

    failures: list[str] = []
    for target, previous, _new_digest in reversed(prepared):
        try:
            _restore_previous(target, previous)
        except OSError as exc:
            failures.append(f"{target}: {exc}")
    if failures:
        details = "; ".join(failures)
        raise AtomicWriteError(
            f"Atomic transaction recovery was incomplete for {path}: {details}"
        )

    try:
        path.unlink()
        _fsync_parent(path)
    except OSError as exc:
        raise AtomicWriteError(
            f"Atomic transaction recovery completed but the journal could not be "
            f"removed: {path}: {exc}"
        ) from exc
    return True


def atomic_write_json_files(
    payloads: Mapping[Path, Mapping[str, Any]],
    *,
    journal_path: str | Path | None = None,
    allowed_paths: Iterable[str | Path] | None = None,
    journal_metadata: Mapping[str, Any] | None = None,
) -> None:
    """Stage and atomically replace a group of JSON documents.

    Every document is serialized before any live target is changed.  The files
    are then replaced in insertion order, with the caller's final entry acting
    as the commit marker.  If a replacement fails, every potentially replaced
    file is restored atomically from its previous contents before the original
    error is raised.  An incomplete or uncertain rollback is reported as
    :class:`AtomicWriteError` and retains the journal.

    When *journal_path* is supplied, the previous contents are written to a
    durable journal before the first live replacement.  A process that dies
    during publication can therefore be rolled back by
    :func:`recover_transaction_journal` on the next bundle access.  If
    *allowed_paths* is supplied, it restricts which files a pending journal
    may restore; this prevents a journal at a bundle location from touching
    an unrelated path.  *journal_metadata* records additional, non-authoritative
    context (such as the bundle scope); a higher-level recovery routine must
    authenticate participant paths through trusted configuration or a registry.
    """
    journal_target: Path | None = None
    recovery_allowed: tuple[Path, ...] | None = None
    if allowed_paths is not None:
        recovery_allowed = tuple(Path(path) for path in allowed_paths)

    # Resolve the complete target set before creating any staged files.  The
    # allowlist is checked after recovering an older journal so a malformed
    # prior transaction is still reported as a recovery failure.
    target_paths: set[Path] = set()
    for target in payloads:
        target_path = Path(target)
        if target_path.is_symlink():
            raise ValueError(
                "Atomic transaction does not support symlink targets: "
                f"{target_path}"
            )
        target_paths.add(_resolved_path(target_path))

    if journal_path is not None:
        journal_target = Path(journal_path)
        if _resolved_path(journal_target) in target_paths:
            raise ValueError("Transaction journal cannot be a transaction target")
        # Recover a prior interrupted transaction before preparing a new one.
        recover_transaction_journal(
            journal_target,
            allowed_paths=recovery_allowed,
        )

    if len(target_paths) != len(payloads):
        raise ValueError("Atomic transaction contains a duplicate target")
    if recovery_allowed is not None:
        allowed_targets = {
            _resolved_path(Path(path)) for path in recovery_allowed
        }
        unexpected = sorted(
            str(path) for path in target_paths - allowed_targets
        )
        if unexpected:
            raise ValueError(
                "Atomic transaction target is outside the allowed paths: "
                + ", ".join(unexpected)
            )

    if not payloads:
        return

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, bytes | None] = {}
    attempted: list[Path] = []
    journal_created = False
    journal = journal_target
    try:
        for target, payload in payloads.items():
            path = Path(target)
            backups[path] = path.read_bytes() if path.is_file() else None
            staged.append((path, _write_temporary_json(path, payload)))

        if journal is not None:
            new_digests = {
                Path(target): hashlib.sha256(json_payload_bytes(payload)).hexdigest()
                for target, payload in payloads.items()
            }
            _write_transaction_journal(
                journal,
                payloads.keys(),
                new_digests=new_digests,
                metadata=journal_metadata,
            )
            journal_created = True

        try:
            for target, temporary_path in staged:
                # Record the target before the replacement.  An asynchronous
                # exception (for example KeyboardInterrupt) can arrive after
                # os.replace() has succeeded but before control returns here;
                # such a target must still be restored during rollback.
                attempted.append(target)
                try:
                    os.replace(temporary_path, target)
                except OSError:
                    # A normal os.replace failure leaves the old target intact.
                    # Keep the target in the rollback set when its contents do
                    # not match the backup, because the replacement state is
                    # then uncertain.
                    previous = backups.get(target)
                    current: bytes | None | object
                    try:
                        current = target.read_bytes() if target.is_file() else None
                    except OSError:
                        current = object()
                    if current == previous:
                        attempted.pop()
                    raise
                _fsync_parent(target)
        except BaseException as exc:
            rollback_failures: list[str] = []
            for target in reversed(attempted):
                previous = backups.get(target)
                try:
                    _restore_previous(target, previous)
                except OSError as rollback_exc:
                    rollback_failures.append(f"{target}: {rollback_exc}")
            if rollback_failures:
                details = "; ".join(rollback_failures)
                error = AtomicWriteError(
                    "Atomic JSON publication failed "
                    f"({exc}) and rollback was incomplete: {details}"
                )
                raise error from exc
            if journal_created and journal is not None:
                try:
                    journal.unlink(missing_ok=True)
                    _fsync_parent(journal)
                except OSError as journal_exc:
                    exc.add_note(
                        "Atomic transaction journal could not be removed after "
                        f"rollback: {journal}: {journal_exc}"
                    )
            raise
    finally:
        for _target, temporary_path in staged:
            temporary_path.unlink(missing_ok=True)

    if journal_created and journal is not None:
        try:
            journal.unlink(missing_ok=True)
            _fsync_parent(journal)
        except OSError as exc:
            raise AtomicWriteError(
                "Atomic JSON publication completed but the transaction journal "
                f"could not be removed: {journal}: {exc}"
            ) from exc


__all__ = [
    "AtomicWriteError",
    "atomic_copy_file",
    "atomic_write_json",
    "atomic_write_json_files",
    "json_payload_bytes",
    "json_payload_digest",
    "recover_transaction_journal",
]

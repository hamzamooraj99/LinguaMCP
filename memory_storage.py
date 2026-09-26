"""Shared safe storage primitives for LinguaMCP learner Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable


ABSENT_VERSION = "absent"
CUMULATIVE_LIMIT_CHARS = 128_000
VOLATILE_LIMIT_CHARS = 16_000
NOTE_LIMIT_CHARS = 128_000
DELIVERY_LIMIT_CHARS = 16_000
READ_PAGE_LIMIT_CHARS = 16_000
MAX_READ_BYTES = 2 * 1024 * 1024
CONTEXT_LIMIT_CHARS = 64_000
LESSON_CONTRACT_LIMIT_BYTES = 128 * 1024

CUMULATIVE_FILES = (
    "00-profile.md",
    "01-lesson-plan.md",
    "02-progress.md",
    "03-vocabulary.md",
    "04-mistakes.md",
    "05-scenarios.md",
)
VOLATILE_FILES = (
    "latest-summary.md",
    "active-session.md",
    "latest-homework.md",
)
DELIVERY_FILES = ("delivery/latest-email.md", "delivery/latest-whatsapp.md")
LESSON_CONTRACT_FILE = "current-lesson-contract.md"
NOTE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,98}\.md$", re.IGNORECASE)
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z\.md$"
)
OPERATION_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

MEMORY_LOCK = threading.RLock()


class StorageError(ValueError):
    """Base class for storage failures safe to report to a caller."""


class VersionConflictError(StorageError):
    """The content changed after the caller read it."""


class OversizedFileError(StorageError):
    """A file exceeds a documented storage limit."""


class RecoveryRequiredError(StorageError):
    """A prepared write group could not safely be completed."""


def version_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def version_text(content: str) -> str:
    return version_bytes(content.encode("utf-8"))


def read_limited_bytes(path: Path, maximum_bytes: int = MAX_READ_BYTES) -> bytes:
    """Read at most the allowed bytes plus one sentinel byte."""
    if path.is_symlink():
        raise StorageError("Refusing to read a symbolic link in learner storage.")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise StorageError("Refusing to read a non-regular learner-memory file.")
        with path.open("rb") as stream:
            content = stream.read(maximum_bytes + 1)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StorageError("Could not read learner memory.") from exc
    if len(content) > maximum_bytes:
        raise OversizedFileError("File exceeds its read limit.")
    return content


def _safe_root(root: Path) -> Path:
    return root.resolve(strict=False)


def _safe_path(root: Path, relative: str, *, internal: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise StorageError("Invalid storage path.")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise StorageError("Invalid storage path.")
    if not internal and any(part.startswith(".") for part in pure.parts):
        raise StorageError("Invalid storage path.")
    base = _safe_root(root)
    candidate = base.joinpath(*pure.parts)
    resolved = candidate.resolve(strict=False)
    if resolved != base and base not in resolved.parents:
        raise StorageError("Invalid storage path.")
    current = base
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise StorageError("Refusing to use a symbolic link in learner storage.")
    return candidate


def _target_allowed(relative: str) -> bool:
    if relative in CUMULATIVE_FILES or relative in VOLATILE_FILES or relative in DELIVERY_FILES:
        return True
    if relative == LESSON_CONTRACT_FILE:
        return True
    if relative.startswith("notes/") and NOTE_PATTERN.fullmatch(relative.removeprefix("notes/")):
        return True
    if relative.startswith("sessions/") and TIMESTAMP_PATTERN.fullmatch(relative.removeprefix("sessions/")):
        return True
    return False


def _backup_allowed(target: str, backup: str) -> bool:
    if target in CUMULATIVE_FILES:
        prefix = "archives/" + target.removesuffix(".md") + "/"
        return backup.startswith(prefix) and TIMESTAMP_PATTERN.fullmatch(backup.removeprefix(prefix)) is not None
    if target in VOLATILE_FILES or target in DELIVERY_FILES or target == LESSON_CONTRACT_FILE or target.startswith("notes/"):
        return backup == ".backups/" + target
    return False


def file_version(path: Path) -> str:
    try:
        content = read_limited_bytes(path)
    except FileNotFoundError:
        return ABSENT_VERSION
    except OversizedFileError:
        raise
    except (OSError, StorageError) as exc:
        raise StorageError("Could not read learner memory.") from exc
    return version_bytes(content)


def check_version(path: Path, expected_version: str | None) -> str:
    if not isinstance(expected_version, str) or not expected_version:
        raise StorageError("expected_version is required; read the file first.")
    actual = file_version(path)
    if actual != expected_version:
        raise VersionConflictError(
            "Version conflict: the file changed after it was read. Reread and merge before retrying."
        )
    return actual


def content_limit(relative: str) -> tuple[int, bool]:
    if relative in CUMULATIVE_FILES:
        return CUMULATIVE_LIMIT_CHARS, False
    if relative in VOLATILE_FILES or (relative.startswith("sessions/") and _target_allowed(relative)):
        return VOLATILE_LIMIT_CHARS, False
    if relative in DELIVERY_FILES:
        return DELIVERY_LIMIT_CHARS, True
    if relative == LESSON_CONTRACT_FILE:
        return LESSON_CONTRACT_LIMIT_BYTES, False
    if relative.startswith("notes/") and _target_allowed(relative):
        return NOTE_LIMIT_CHARS, False
    raise StorageError("Invalid learner-memory filename.")


def validate_content(relative: str, content: str) -> bytes:
    if not isinstance(content, str):
        raise StorageError("Content must be a string containing Markdown.")
    limit, allow_blank = content_limit(relative)
    encoded = content.encode("utf-8")
    size = len(encoded) if relative == LESSON_CONTRACT_FILE else len(content)
    if size > limit:
        unit = "bytes" if relative == LESSON_CONTRACT_FILE else "characters"
        raise OversizedFileError(f"{relative} exceeds its {limit} {unit} write limit.")
    if not allow_blank and not content.strip():
        raise StorageError(f"{relative} cannot be empty or whitespace.")
    return encoded


def atomic_write(path: Path, content: bytes) -> None:
    """Replace one file atomically, preserving its current mode when possible."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StorageError("Refusing to replace a symbolic link.")
    old_mode: int | None = None
    try:
        old_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        if old_mode is not None:
            os.chmod(temporary, old_mode)
        else:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    except Exception:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def paged_read(
    path: Path,
    *,
    offset_chars: int = 0,
    limit_chars: int = READ_PAGE_LIMIT_CHARS,
    expected_version: str | None = None,
) -> dict[str, Any]:
    if isinstance(offset_chars, bool) or not isinstance(offset_chars, int) or offset_chars < 0:
        raise StorageError("offset_chars must be a non-negative integer.")
    if isinstance(limit_chars, bool) or not isinstance(limit_chars, int) or not 1 <= limit_chars <= READ_PAGE_LIMIT_CHARS:
        raise StorageError(f"limit_chars must be between 1 and {READ_PAGE_LIMIT_CHARS}.")
    if offset_chars and expected_version is None:
        raise StorageError("expected_version is required for later pages.")
    try:
        raw = read_limited_bytes(path)
    except OversizedFileError:
        raise
    except (OSError, StorageError) as exc:
        raise StorageError("Could not read learner memory.") from exc
    version = version_bytes(raw)
    if expected_version is not None and version != expected_version:
        raise VersionConflictError("Version conflict: this file changed between pages. Restart the read.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError("Learner memory is not valid UTF-8.") from exc
    if offset_chars > len(text):
        raise StorageError("offset_chars is past the end of the file.")
    end = min(offset_chars + limit_chars, len(text))
    complete = end >= len(text)
    return {
        "content": text[offset_chars:end],
        "version": version,
        "offset_chars": offset_chars,
        "next_offset_chars": None if complete else end,
        "total_chars": len(text),
        "complete": complete,
    }


def normalize_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not OPERATION_PATTERN.fullmatch(operation_id):
        raise StorageError("operation_id must be a UUID generated for this intended save.")
    try:
        parsed = uuid.UUID(operation_id)
    except ValueError as exc:
        raise StorageError("operation_id must be a UUID generated for this intended save.") from exc
    return str(parsed)


def request_fingerprint(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageError("The save request cannot be fingerprinted.") from exc
    if len(encoded) > MAX_READ_BYTES:
        raise OversizedFileError("The serialized save request exceeds the 2 MiB safety limit.")
    return version_bytes(encoded)


def _write_stage_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _safe_internal_dir(language_dir: Path, relative: str) -> Path:
    path = _safe_path(language_dir, relative, internal=True)
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise RecoveryRequiredError("Private storage state is not a safe directory.")
    return path


def _read_receipt(language_dir: Path, operation_id: str, fingerprint: str) -> dict[str, Any] | None:
    _safe_internal_dir(language_dir, ".operations")
    path = _safe_path(language_dir, f".operations/{operation_id}.json", internal=True)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RecoveryRequiredError("The saved operation receipt is not a regular file.")
    try:
        if path.stat().st_size > 64 * 1024:
            raise RecoveryRequiredError("The saved operation receipt exceeds its size limit.")
        receipt = json.loads(read_limited_bytes(path, 64 * 1024).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, StorageError) as exc:
        raise RecoveryRequiredError("The saved operation receipt cannot be read.") from exc
    if receipt.get("operation_id") != operation_id or receipt.get("fingerprint") != fingerprint:
        raise StorageError("operation_id was already used with a different payload.")
    result = receipt.get("result")
    if not isinstance(result, dict):
        raise RecoveryRequiredError("The saved operation receipt is invalid.")
    return result


def check_operation(
    language_dir: Path,
    operation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a committed retry or recover prepared work before fresh validation."""
    operation_id = normalize_operation_id(operation_id)
    fingerprint = request_fingerprint(payload)
    result = _read_receipt(language_dir, operation_id, fingerprint)
    if result is not None:
        pending = _safe_internal_dir(language_dir, ".transactions/pending")
        if pending.exists():
            manifest = _load_manifest(pending)
            if manifest.get("operation_id") == operation_id:
                shutil.rmtree(pending, ignore_errors=True)
        return result
    recover_pending(language_dir)
    return _read_receipt(language_dir, operation_id, fingerprint)


def _backup_destination(language_dir: Path, target: str, backup: str, old_bytes: bytes) -> None:
    if not _backup_allowed(target, backup):
        raise RecoveryRequiredError("A prepared write has an invalid backup destination.")
    backup_path = _safe_path(language_dir, backup, internal=backup.startswith(".backups/"))
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.is_symlink():
        raise RecoveryRequiredError("A backup destination is a symbolic link.")
    if backup.startswith("archives/") and backup_path.exists():
        try:
            existing = read_limited_bytes(backup_path)
        except (OSError, StorageError) as exc:
            raise RecoveryRequiredError("A required archive backup cannot be read.") from exc
        if existing != old_bytes:
            raise RecoveryRequiredError("An archive backup path is already in use.")
        return
    if backup_path.exists():
        try:
            if read_limited_bytes(backup_path) == old_bytes:
                return
        except StorageError as exc:
            raise RecoveryRequiredError("An existing backup cannot be verified.") from exc
    atomic_write(backup_path, old_bytes)


def _receipt_path(language_dir: Path, operation_id: str) -> Path:
    operations = _safe_internal_dir(language_dir, ".operations")
    operations.mkdir(parents=True, exist_ok=True)
    return _safe_path(language_dir, f".operations/{operation_id}.json", internal=True)


def _manifest_path(pending: Path) -> Path:
    path = pending / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise RecoveryRequiredError("The pending write record is missing or unsafe.")
    if path.stat().st_size > 256 * 1024:
        raise RecoveryRequiredError("The pending write record exceeds its size limit.")
    return path


def _load_manifest(pending: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(read_limited_bytes(_manifest_path(pending), 256 * 1024).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, StorageError) as exc:
        raise RecoveryRequiredError("The pending write record cannot be read.") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RecoveryRequiredError("The pending write record has an unsupported format.")
    try:
        manifest["operation_id"] = normalize_operation_id(manifest.get("operation_id"))
    except StorageError as exc:
        raise RecoveryRequiredError("The pending write record has an invalid operation ID.") from exc
    if not isinstance(manifest.get("fingerprint"), str) or not isinstance(manifest.get("targets"), list):
        raise RecoveryRequiredError("The pending write record is incomplete.")
    return manifest


def _stage_bytes(pending: Path, relative: Any) -> bytes:
    if not isinstance(relative, str) or not re.fullmatch(r"(?:new|old)/[0-9]{4}\.bin", relative):
        raise RecoveryRequiredError("A staged file path is invalid.")
    path = pending / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise RecoveryRequiredError("A staged file is missing or unsafe.")
        if path.stat().st_size > MAX_READ_BYTES:
            raise RecoveryRequiredError("A staged file exceeds its size limit.")
        return read_limited_bytes(path)
    except (OSError, StorageError) as exc:
        raise RecoveryRequiredError("A staged file cannot be read.") from exc


def _write_receipt(language_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    operation_id = manifest["operation_id"]
    path = _receipt_path(language_dir, operation_id)
    receipt = {
        "schema_version": 1,
        "operation_id": operation_id,
        "fingerprint": manifest["fingerprint"],
        "result": manifest["result"],
    }
    if path.exists():
        try:
            existing = json.loads(read_limited_bytes(path, 64 * 1024).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, StorageError) as exc:
            raise RecoveryRequiredError("The operation receipt cannot be verified.") from exc
        if existing != receipt:
            raise RecoveryRequiredError("A different result already uses this operation ID.")
    else:
        atomic_write(path, _json_bytes(receipt))
    return manifest["result"]


def _finish_pending(language_dir: Path, pending: Path) -> dict[str, Any]:
    manifest = _load_manifest(pending)
    targets = manifest["targets"]
    if not targets:
        raise RecoveryRequiredError("The pending write has no targets.")
    for item in targets:
        if not isinstance(item, dict):
            raise RecoveryRequiredError("The pending write contains an invalid target.")
        relative = item.get("path")
        if not _target_allowed(relative):
            raise RecoveryRequiredError("The pending write contains an unsupported target.")
        target = _safe_path(language_dir, relative)
        old_bytes = _stage_bytes(pending, item.get("old_stage")) if item.get("before_version") != ABSENT_VERSION else b""
        new_bytes = _stage_bytes(pending, item.get("new_stage"))
        if version_bytes(old_bytes) != item.get("before_version") and item.get("before_version") != ABSENT_VERSION:
            raise RecoveryRequiredError("A staged previous copy failed its integrity check.")
        if version_bytes(new_bytes) != item.get("after_version"):
            raise RecoveryRequiredError("A staged replacement failed its integrity check.")
        backup = item.get("backup")
        current = file_version(target)
        if current not in {item.get("before_version"), item.get("after_version")}:
            raise RecoveryRequiredError("A target changed unexpectedly during recovery; inspect it before continuing.")
        if backup is not None and item.get("before_version") != ABSENT_VERSION:
            _backup_destination(language_dir, relative, backup, old_bytes)
        if current != item.get("after_version"):
            atomic_write(target, new_bytes)
    return _write_receipt(language_dir, manifest)


def recover_pending(language_dir: Path, *, read_only: bool = False) -> dict[str, Any] | None:
    """Finish a published transaction, or report it without writes in read-only mode."""
    pending = _safe_internal_dir(language_dir, ".transactions/pending")
    if not pending.exists():
        if not read_only:
            _cleanup_unpublished_staging(language_dir)
        return None
    if read_only:
        raise RecoveryRequiredError("recovery_required: a prepared memory save needs writable recovery.")
    try:
        result = _finish_pending(language_dir, pending)
    except RecoveryRequiredError:
        raise
    except Exception as exc:
        raise RecoveryRequiredError("recovery_required: a prepared memory save could not be completed.") from exc
    try:
        shutil.rmtree(pending)
    except OSError:
        # Receipt publication is the commit point; a later request can clean up.
        pass
    _cleanup_unpublished_staging(language_dir)
    return result


def _cleanup_unpublished_staging(language_dir: Path) -> None:
    transactions = _safe_internal_dir(language_dir, ".transactions")
    if not transactions.exists():
        return
    for entry in transactions.iterdir():
        if not entry.name.startswith(".staging-") or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            normalize_operation_id(entry.name.removeprefix(".staging-"))
        except StorageError:
            continue
        shutil.rmtree(entry)


def recover_data_root(data_root: Path, *, read_only: bool = False) -> list[str]:
    recovered: list[str] = []
    if not data_root.exists():
        return recovered
    for language_dir in sorted(data_root.iterdir(), key=lambda item: item.name):
        if not language_dir.is_dir() or language_dir.is_symlink() or language_dir.name.startswith("."):
            continue
        with MEMORY_LOCK:
            result = recover_pending(language_dir, read_only=read_only)
        if result:
            recovered.append(language_dir.name)
    return recovered


def commit_write_group(
    language_dir: Path,
    operation_id: str,
    payload: dict[str, Any],
    targets: list[dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Prepare and forward-commit a bounded group of allowlisted file writes."""
    operation_id = normalize_operation_id(operation_id)
    fingerprint = request_fingerprint(payload)
    receipt = _read_receipt(language_dir, operation_id, fingerprint)
    if receipt is not None:
        pending = _safe_internal_dir(language_dir, ".transactions/pending")
        if pending.exists():
            manifest = _load_manifest(pending)
            if manifest.get("operation_id") == operation_id:
                shutil.rmtree(pending, ignore_errors=True)
        return receipt

    previous = recover_pending(language_dir)
    if previous is not None:
        pending_receipt = _read_receipt(language_dir, operation_id, fingerprint)
        if pending_receipt is not None:
            return pending_receipt
        # The just-recovered operation may have reused this ID with another payload.
        receipt_path = _safe_path(language_dir, f".operations/{operation_id}.json", internal=True)
        if receipt_path.exists():
            _read_receipt(language_dir, operation_id, fingerprint)

    transaction_root = _safe_internal_dir(language_dir, ".transactions")
    transaction_root.mkdir(parents=True, exist_ok=True)
    pending = _safe_internal_dir(language_dir, ".transactions/pending")
    staging = _safe_internal_dir(language_dir, f".transactions/.staging-{operation_id}")
    if pending.exists():
        raise RecoveryRequiredError("A previous memory save must be recovered first.")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "new").mkdir()
    (staging / "old").mkdir()

    manifest_targets: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(targets):
            relative = item.get("path")
            if not _target_allowed(relative):
                raise StorageError("An unsupported write-group target was requested.")
            content = item.get("content")
            if not isinstance(content, bytes):
                raise StorageError("A write-group replacement must be UTF-8 bytes.")
            target = _safe_path(language_dir, relative)
            if target.exists() and (target.is_symlink() or not target.is_file()):
                raise StorageError("Refusing to replace a non-regular learner-memory file.")
            try:
                old = read_limited_bytes(target) if target.exists() else None
            except (OSError, StorageError) as exc:
                raise StorageError("Could not prepare learner-memory save.") from exc
            before = version_bytes(old) if old is not None else ABSENT_VERSION
            expected = item.get("expected_version")
            if expected is not None and before != expected:
                raise VersionConflictError(
                    f"Version conflict for {relative}: reread and merge before retrying."
                )
            after = version_bytes(content)
            backup = item.get("backup") if old is not None else None
            if backup is not None and not _backup_allowed(relative, backup):
                raise StorageError("An unsupported backup destination was requested.")
            new_stage = f"new/{index:04d}.bin"
            old_stage = f"old/{index:04d}.bin" if old is not None else None
            _write_stage_file(staging / new_stage, content)
            if old is not None and old_stage is not None:
                _write_stage_file(staging / old_stage, old)
            manifest_targets.append({
                "path": relative,
                "before_version": before,
                "after_version": after,
                "backup": backup,
                "old_stage": old_stage,
                "new_stage": new_stage,
            })

        normalized_result = dict(result)
        normalized_result["operation_id"] = operation_id
        normalized_result["committed"] = True
        manifest = {
            "schema_version": 1,
            "language": language_dir.name,
            "operation_id": operation_id,
            "fingerprint": fingerprint,
            "targets": manifest_targets,
            "result": normalized_result,
        }
        _write_stage_file(staging / "manifest.json", _json_bytes(manifest))
        _fsync_directory(staging)
        os.replace(staging, pending)
        _fsync_directory(transaction_root)
        try:
            result = _finish_pending(language_dir, pending)
        except RecoveryRequiredError:
            raise
        except Exception as exc:
            raise RecoveryRequiredError("recovery_required: the prepared memory save needs recovery.") from exc
        try:
            shutil.rmtree(pending)
        except OSError:
            pass
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def save_single_file(
    language_dir: Path,
    relative: str,
    content: str,
    expected_version: str,
    *,
    backup: str | None = None,
) -> str:
    """Check one read version, back up prior bytes, and atomically replace."""
    if not _target_allowed(relative):
        raise StorageError("Invalid learner-memory filename.")
    new_bytes = validate_content(relative, content)
    path = _safe_path(language_dir, relative)
    current = check_version(path, expected_version)
    if current == version_bytes(new_bytes):
        return current
    if current != ABSENT_VERSION and backup is not None:
        if not _backup_allowed(relative, backup):
            raise StorageError("Invalid backup destination.")
        old = read_limited_bytes(path)
        _backup_destination(language_dir, relative, backup, old)
    atomic_write(path, new_bytes)
    return version_bytes(new_bytes)


class WriterLock:
    """Exclusive lifetime lock for one writable MCP process per data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.path = data_root / ".writer.lock"
        self._stream: Any = None

    def acquire(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("The LinguaMCP writer lock path is unsafe.")
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"\0")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._stream.close()
            self._stream = None
            raise RuntimeError("Another writable LinguaMCP server already uses this data root.") from exc

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "WriterLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()

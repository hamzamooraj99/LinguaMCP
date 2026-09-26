"""Read-only discovery of LinguaMCP Markdown learner memory."""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
LANGUAGE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
TIMESTAMP_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:-\d{6})?Z)\.md$",
    re.IGNORECASE,
)

GROUP_ORDER = ("current", "sessions", "archives", "delivery", "other")
GROUP_LABELS = {
    "current": "Current memory",
    "sessions": "Sessions",
    "archives": "Archives",
    "delivery": "Delivery",
    "other": "Other",
}

CURRENT_LABELS = {
    "00-profile.md": "Profile",
    "01-lesson-plan.md": "Lesson plan",
    "02-progress.md": "Progress",
    "03-vocabulary.md": "Vocabulary",
    "04-mistakes.md": "Mistakes",
    "05-scenarios.md": "Scenarios",
    "active-session.md": "Active session",
    "latest-summary.md": "Latest summary",
    "latest-homework.md": "Homework",
    "current-lesson-contract.md": "Lesson contract",
}

DELIVERY_LABELS = {
    "delivery/latest-email.md": "Email draft",
    "delivery/latest-whatsapp.md": "WhatsApp draft",
}


class ViewerStorageError(Exception):
    """Base class for storage errors that can be shown without file paths."""

    code = "storage_error"
    message = "The learner data could not be read."

    def __init__(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        super().__init__(self.message)


class InvalidRequestError(ViewerStorageError):
    code = "invalid_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LanguageNotFoundError(ViewerStorageError):
    code = "not_found"
    message = "Language not found."


class DocumentNotFoundError(ViewerStorageError):
    code = "not_found"
    message = "Document not found."


class DocumentTooLargeError(ViewerStorageError):
    code = "too_large"
    message = "Document exceeds the 2 MiB limit."


class UnreadableDocumentError(ViewerStorageError):
    code = "unreadable"
    message = "Document could not be read."


class InvalidUTF8Error(UnreadableDocumentError):
    message = "Document could not be read as UTF-8."


class StorageFailureError(ViewerStorageError):
    code = "storage_error"
    message = "The learner data could not be read."


class RecoveryUnavailableError(ViewerStorageError):
    code = "recovery_required"
    message = "Memory is temporarily unavailable while a saved update is being recovered. Try again shortly."


@dataclass(frozen=True)
class DocumentRecord:
    """Metadata for one safe, regular Markdown file."""

    path: str
    label: str
    group: str
    file_path: Path
    modified_at: str
    modified_datetime: datetime
    size_bytes: int
    timestamp: datetime | None = None
    archive_source: str = ""

    def metadata(self) -> dict[str, object]:
        return {
            "path": self.path,
            "label": self.label,
            "modifiedAt": self.modified_at,
            "sizeBytes": self.size_bytes,
        }


def default_data_root() -> Path:
    """Return the repository-local development data root."""

    return Path(__file__).resolve().parents[1] / "tutor_data"


def resolve_data_root(explicit: str | Path | None = None) -> Path:
    """Apply the Phase 1 data-root precedence without reading learner data."""

    value: str | Path | None = explicit
    if value is None:
        value = os.environ.get("LINGUAMCP_DATA_ROOT")
    if value is None or str(value).strip() == "":
        return default_data_root()
    return Path(value).expanduser().resolve(strict=False)


def _root_path(data_root: str | Path | None) -> Path:
    return resolve_data_root(data_root)


def _friendly_words(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).strip()
    if not words:
        return "Untitled"
    return words[:1].upper() + words[1:]


def friendly_language_label(language_id: str) -> str:
    return _friendly_words(language_id)


def _friendly_filename(filename: str) -> str:
    stem = re.sub(r"\.md$", "", filename, flags=re.IGNORECASE)
    return _friendly_words(stem)


def _parse_timestamp(filename: str) -> datetime | None:
    match = TIMESTAMP_PATTERN.fullmatch(filename)
    if not match:
        return None
    value = match.group("timestamp")
    formats = ("%Y-%m-%dT%H-%M-%S-%fZ", "%Y-%m-%dT%H-%M-%SZ")
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d %b %Y")


def _modified_datetime(stat_result: os.stat_result) -> datetime:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)


def _modified_at(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _archive_source(relative_path: str) -> str:
    parts = relative_path.split("/")
    if len(parts) >= 3:
        return parts[1]
    return "archive"


def _classify(relative_path: str) -> tuple[str, str]:
    lower_path = relative_path.lower()
    if lower_path in CURRENT_LABELS:
        return "current", CURRENT_LABELS[lower_path]
    if lower_path.startswith("sessions/"):
        timestamp = _parse_timestamp(relative_path.rsplit("/", 1)[-1])
        label = (
            f"Session · {_format_timestamp(timestamp)}"
            if timestamp
            else _friendly_filename(relative_path.rsplit("/", 1)[-1])
        )
        return "sessions", label
    if lower_path.startswith("archives/"):
        timestamp = _parse_timestamp(relative_path.rsplit("/", 1)[-1])
        source_key = _archive_source(relative_path)
        source = CURRENT_LABELS.get(
            f"{source_key.lower()}.md",
            _friendly_filename(source_key + ".md"),
        )
        label = f"{source} · {_format_timestamp(timestamp)}" if timestamp else source
        return "archives", label
    if lower_path in DELIVERY_LABELS:
        return "delivery", DELIVERY_LABELS[lower_path]
    return "other", _friendly_filename(relative_path.rsplit("/", 1)[-1])


def _timestamp_for_sort(relative_path: str) -> datetime | None:
    return _parse_timestamp(relative_path.rsplit("/", 1)[-1])


def _validate_language(language: str) -> str:
    if not isinstance(language, str) or not LANGUAGE_PATTERN.fullmatch(language):
        raise InvalidRequestError(
            "Invalid language identifier. Use lowercase letters, digits, or hyphens."
        )
    return language


def validate_relative_path(relative_path: str) -> str:
    """Validate a URL path without normalizing traversal attempts."""

    if not isinstance(relative_path, str) or not relative_path:
        raise InvalidRequestError("Invalid document path.")
    if "\x00" in relative_path or "\\" in relative_path:
        raise InvalidRequestError("Invalid document path.")
    if relative_path.startswith("/") or ":" in relative_path:
        raise InvalidRequestError("Invalid document path.")

    parts = relative_path.split("/")
    if any(
        not part or part in {".", ".."} or part.startswith(".") for part in parts
    ):
        raise InvalidRequestError("Invalid document path.")
    if not parts[-1].lower().endswith(".md"):
        raise InvalidRequestError("Invalid document path.")
    return "/".join(parts)


def _scan_language_directories(root: Path) -> list[tuple[str, Path]]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise StorageFailureError("The configured learner data root is not a directory.")

    languages: list[tuple[str, Path]] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if not LANGUAGE_PATTERN.fullmatch(entry.name):
                    continue
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                languages.append((entry.name, Path(entry.path)))
    except OSError as error:
        raise StorageFailureError from error
    return sorted(languages, key=lambda item: item[0])


def _language_directory(language: str, data_root: str | Path | None) -> Path:
    normalized = _validate_language(language)
    root = _root_path(data_root)
    for language_id, path in _scan_language_directories(root):
        if language_id == normalized:
            _ensure_language_ready(path)
            return path
    raise LanguageNotFoundError


def _ensure_language_ready(language_directory: Path) -> None:
    transactions = language_directory / ".transactions"
    pending = transactions / "pending"
    if transactions.is_symlink() or pending.is_symlink():
        raise RecoveryUnavailableError()
    if pending.exists():
        raise RecoveryUnavailableError()


def ensure_language_ready(language: str, *, data_root: str | Path | None = None) -> None:
    """Recheck recovery visibility immediately before returning document data."""
    _ensure_language_ready(_language_directory(language, data_root))


def _walk_markdown_files(language_directory: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    def walk(directory: Path, parent_parts: tuple[str, ...]) -> Iterator[tuple[str, Path, os.stat_result]]:
        try:
            with os.scandir(directory) as entries:
                sorted_entries = sorted(entries, key=lambda entry: entry.name.casefold())
                for entry in sorted_entries:
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    parts = parent_parts + (entry.name,)
                    if entry.is_dir(follow_symlinks=False):
                        yield from walk(Path(entry.path), parts)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.lower().endswith(".md"):
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise StorageFailureError from error
                    if not stat.S_ISREG(entry_stat.st_mode):
                        continue
                    yield "/".join(parts), Path(entry.path), entry_stat
        except OSError as error:
            raise StorageFailureError from error

    yield from walk(language_directory, ())


def _document_record(relative_path: str, file_path: Path, entry_stat: os.stat_result) -> DocumentRecord:
    group, label = _classify(relative_path)
    modified_datetime = _modified_datetime(entry_stat)
    return DocumentRecord(
        path=relative_path,
        label=label,
        group=group,
        file_path=file_path,
        modified_at=_modified_at(modified_datetime),
        modified_datetime=modified_datetime,
        size_bytes=entry_stat.st_size,
        timestamp=_timestamp_for_sort(relative_path),
        archive_source=_archive_source(relative_path) if group == "archives" else "",
    )


def _sort_documents(group: str, documents: list[DocumentRecord]) -> list[DocumentRecord]:
    if group == "current":
        order = {filename: index for index, filename in enumerate(CURRENT_LABELS)}
        return sorted(
            documents,
            key=lambda document: (
                order.get(document.path.lower(), len(order)),
                document.path.casefold(),
            ),
        )
    if group in {"sessions", "archives"}:
        if group == "archives":
            return sorted(
                documents,
                key=lambda document: (
                    document.archive_source.casefold(),
                    -(document.timestamp or document.modified_datetime).timestamp(),
                    document.path.casefold(),
                ),
            )
        return sorted(
            documents,
            key=lambda document: (
                (document.timestamp or document.modified_datetime).timestamp(),
                document.path.casefold(),
            ),
            reverse=True,
        )
    return sorted(documents, key=lambda document: document.path.casefold())


def discover_documents(language: str, *, data_root: str | Path | None = None) -> list[DocumentRecord]:
    language_directory = _language_directory(language, data_root)
    return [
        _document_record(relative_path, file_path, entry_stat)
        for relative_path, file_path, entry_stat in _walk_markdown_files(language_directory)
    ]


def list_languages(*, data_root: str | Path | None = None) -> list[dict[str, str]]:
    root = _root_path(data_root)
    return [
        {"id": language_id, "label": friendly_language_label(language_id)}
        for language_id, _ in _scan_language_directories(root)
    ]


def list_document_groups(
    language: str, *, data_root: str | Path | None = None
) -> list[dict[str, object]]:
    documents = discover_documents(language, data_root=data_root)
    grouped = {group: [] for group in GROUP_ORDER}
    for document in documents:
        grouped[document.group].append(document)

    return [
        {
            "id": group,
            "label": GROUP_LABELS[group],
            "documents": [
                document.metadata() for document in _sort_documents(group, grouped[group])
            ],
        }
        for group in GROUP_ORDER
    ]


def find_document(
    language: str,
    relative_path: str,
    *,
    data_root: str | Path | None = None,
) -> DocumentRecord:
    safe_path = validate_relative_path(relative_path)
    for document in discover_documents(language, data_root=data_root):
        if document.path == safe_path:
            return document
    raise DocumentNotFoundError


def read_document(document: DocumentRecord) -> str:
    """Read one already-discovered regular file without following symlinks."""

    if document.file_path.is_symlink():
        raise DocumentNotFoundError

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(document.file_path, flags | no_follow)
    except OSError as error:
        if no_follow and error.errno == errno.ELOOP:
            raise DocumentNotFoundError from error
        raise UnreadableDocumentError from error

    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise DocumentNotFoundError
            if file_stat.st_size > MAX_DOCUMENT_BYTES:
                raise DocumentTooLargeError
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
    except ViewerStorageError:
        raise
    except OSError as error:
        raise UnreadableDocumentError from error
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass

    if len(content) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLargeError
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidUTF8Error from error

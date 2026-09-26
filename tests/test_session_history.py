from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memory_storage import VersionConflictError
from server import (
    ServerConfig,
    append_session,
    configure_runtime_security,
    initialize_profile,
    list_sessions,
    read_context,
    read_session,
    save_checkpoint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SessionHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "tutor_data"
        self.language_dir = self.root / "german"
        initialize_profile(
            "german",
            "# Profile\n\nLearns Deutsch.",
            "# Plan\n\nPractise introductions.",
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
        )

    def tearDown(self) -> None:
        configure_runtime_security(ServerConfig())
        self.temporary_directory.cleanup()

    def summary_version(self) -> str:
        return read_context("german", data_root=self.root)["_meta"]["file_versions"]["latest-summary.md"]

    def log(self, summary: str, now: datetime | None = None, operation_id: str | None = None):
        return append_session(
            "german",
            summary,
            {"latest-summary.md": self.summary_version()},
            operation_id or str(uuid.uuid4()),
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
            now=now,
        )

    def test_standalone_log_preserves_checkpoint_and_retries_once(self) -> None:
        context = read_context("german", data_root=self.root)
        checkpoint = "# Checkpoint\n\nResume with delayed retrieval of Wie komme ich."
        save_checkpoint(
            "german",
            checkpoint,
            context["_meta"]["file_versions"]["active-session.md"],
            data_root=self.root,
        )
        summary = "# Paused Session\n\nLearner needs another delayed attempt."
        versions = {"latest-summary.md": self.summary_version()}
        operation_id = str(uuid.uuid4())
        now = datetime(2026, 6, 21, 10, 11, 12, 123456, tzinfo=timezone.utc)
        first = append_session(
            "german",
            summary,
            versions,
            operation_id,
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
            now=now,
        )
        retry = append_session(
            "german",
            summary,
            versions,
            operation_id,
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
            now=now,
        )

        self.assertFalse(first["active_session_cleared"])
        self.assertEqual(first, retry)
        self.assertEqual((self.language_dir / "active-session.md").read_text("utf-8"), checkpoint)
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)
        self.assertEqual((self.language_dir / "latest-summary.md").read_text("utf-8"), summary)

    def test_session_history_cursor_is_exclusive_and_stable_when_new_logs_arrive(self) -> None:
        fixed = datetime(2026, 6, 21, 11, 0, 0, tzinfo=timezone.utc)
        for number in range(3):
            self.log(f"# Session {number}\n\nSummary {number}", fixed.replace(microsecond=number))

        page = list_sessions("german", limit=2, data_root=self.root)
        self.assertEqual(len(page["sessions"]), 2)
        self.assertIsNotNone(page["next_before"])
        cursor = page["next_before"]
        self.log("# New Session\n\nA newer log.", fixed.replace(hour=12))
        older = list_sessions("german", before=cursor, limit=2, data_root=self.root)
        self.assertTrue(older["sessions"])
        self.assertLess(older["sessions"][0], cursor)
        self.assertNotIn(cursor, older["sessions"])

    def test_session_pages_are_version_checked_and_unicode_safe(self) -> None:
        summary = "Ä漢" * 4500
        result = self.log(summary, datetime(2026, 6, 21, 13, 0, 0, tzinfo=timezone.utc))
        filename = Path(result["session_file"]).name
        first = read_session(filename=filename, language="german", limit_chars=6000, data_root=self.root)
        second = read_session(
            "german",
            filename,
            offset_chars=first["next_offset_chars"],
            limit_chars=6000,
            expected_version=first["version"],
            data_root=self.root,
        )
        self.assertEqual(first["content"] + second["content"], summary)
        self.assertTrue(second["complete"])

        path = self.language_dir / "sessions" / filename
        path.write_text(summary + "external", encoding="utf-8")
        with self.assertRaises(VersionConflictError):
            read_session(
                "german",
                filename,
                offset_chars=first["next_offset_chars"],
                expected_version=first["version"],
                data_root=self.root,
            )

    def test_session_paths_are_validated_and_reads_work_in_read_only_mode(self) -> None:
        result = self.log("# Session\n\nA history record.", datetime(2026, 6, 21, 14, 0, 0, tzinfo=timezone.utc))
        configure_runtime_security(ServerConfig(allow_writes=False, audit_enabled=False))

        listing = list_sessions("german", data_root=self.root)
        self.assertIn(Path(result["session_file"]).name, listing["sessions"])
        with self.assertRaises(ValueError):
            read_session("german", "../outside.md", data_root=self.root)
        document = read_session("german", Path(result["session_file"]).name, data_root=self.root)
        self.assertIn("A history record", document["content"])


if __name__ == "__main__":
    unittest.main()

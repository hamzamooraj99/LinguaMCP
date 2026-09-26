from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import memory_storage
from server import (
    ServerConfig,
    configure_runtime_security,
    finalize_lesson_storage,
    initialize_profile,
    read_context,
    save_checkpoint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONED_FINALIZATION_FILES = (
    "00-profile.md",
    "01-lesson-plan.md",
    "02-progress.md",
    "03-vocabulary.md",
    "04-mistakes.md",
    "05-scenarios.md",
    "latest-homework.md",
    "latest-summary.md",
    "active-session.md",
)


class FinalizationRecoveryTests(unittest.TestCase):
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

    def context_versions(self) -> dict[str, str]:
        context = read_context("german", data_root=self.root)
        return {
            name: context["_meta"]["file_versions"][name]
            for name in VERSIONED_FINALIZATION_FILES
        }

    def request(self, versions: dict[str, str], operation_id: str | None = None, session: str | None = None):
        return {
            "language": "german",
            "session_markdown": session or "# Session\n\nPractised a destination question.",
            "homework_markdown": "# Homework\n\nReview the destination question.",
            "updates": {"02-progress.md": "# Progress\n\nIndependent practice is recorded."},
            "unchanged_files": [
                "00-profile.md",
                "01-lesson-plan.md",
                "03-vocabulary.md",
                "04-mistakes.md",
                "05-scenarios.md",
            ],
            "expected_versions": versions,
            "operation_id": operation_id or str(uuid.uuid4()),
            "data_root": self.root,
            "template_root": PROJECT_ROOT / "templates",
            "now": datetime(2026, 6, 21, 14, 0, 0, 0, tzinfo=timezone.utc),
        }

    def finalize(self, **kwargs):
        return finalize_lesson_storage(**kwargs)

    def test_empty_finalization_rejects_before_any_permanent_write(self) -> None:
        versions = self.context_versions()
        original = {name: (self.language_dir / name).read_bytes() for name in VERSIONED_FINALIZATION_FILES}
        request = self.request(versions, session=" \n\t")

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            self.finalize(**request)

        self.assertEqual(
            {name: (self.language_dir / name).read_bytes() for name in VERSIONED_FINALIZATION_FILES},
            original,
        )
        self.assertEqual(list((self.language_dir / "sessions").glob("*.md")), [])

    def test_lost_response_retry_returns_one_saved_result_and_one_session(self) -> None:
        request = self.request(self.context_versions())
        first = self.finalize(**request)
        retry = self.finalize(**request)

        self.assertEqual(first, retry)
        self.assertTrue(first["committed"])
        self.assertEqual(first["operation_id"], request["operation_id"])
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)
        receipt = json.loads(
            (self.language_dir / ".operations" / f"{request['operation_id']}.json").read_text("utf-8")
        )
        self.assertNotIn("session_markdown", json.dumps(receipt))

        changed = dict(request)
        changed["session_markdown"] = "# Different payload\n\nMust be rejected."
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.finalize(**changed)
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)

    def test_failure_after_some_replacements_recovers_forward_with_same_session(self) -> None:
        request = self.request(self.context_versions())
        original_atomic_write = memory_storage.atomic_write

        def fail_on_live_summary(path: Path, content: bytes) -> None:
            if path == self.language_dir / "latest-summary.md":
                raise OSError("simulated interruption")
            original_atomic_write(path, content)

        with patch("memory_storage.atomic_write", side_effect=fail_on_live_summary):
            with self.assertRaises(memory_storage.RecoveryRequiredError):
                self.finalize(**request)

        self.assertTrue((self.language_dir / ".transactions" / "pending").is_dir())
        # Reopening storage completes the frozen operation before the read.
        memory_storage.recover_pending(self.language_dir)
        self.assertEqual((self.language_dir / "latest-summary.md").read_text("utf-8"), request["session_markdown"])
        self.assertEqual((self.language_dir / "latest-homework.md").read_text("utf-8"), request["homework_markdown"])
        self.assertEqual((self.language_dir / "02-progress.md").read_text("utf-8"), request["updates"]["02-progress.md"])
        self.assertIn("No active session checkpoint", (self.language_dir / "active-session.md").read_text("utf-8"))
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)

        receipt = json.loads(
            (self.language_dir / ".operations" / f"{request['operation_id']}.json").read_text("utf-8")
        )
        recovered = self.finalize(**request)
        self.assertEqual(recovered, receipt["result"])
        self.assertTrue((self.language_dir / recovered["session_file"]).is_file())
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)

    def test_read_only_mode_reports_pending_without_repairing(self) -> None:
        request = self.request(self.context_versions())
        original_atomic_write = memory_storage.atomic_write

        def fail_on_live_summary(path: Path, content: bytes) -> None:
            if path == self.language_dir / "latest-summary.md":
                raise OSError("simulated interruption")
            original_atomic_write(path, content)

        with patch("memory_storage.atomic_write", side_effect=fail_on_live_summary):
            with self.assertRaises(memory_storage.RecoveryRequiredError):
                self.finalize(**request)
        summary_before = (self.language_dir / "latest-summary.md").read_bytes()
        configure_runtime_security(ServerConfig(allow_writes=False, audit_enabled=False))

        with self.assertRaisesRegex(memory_storage.RecoveryRequiredError, "recovery_required"):
            read_context("german", data_root=self.root)

        self.assertEqual((self.language_dir / "latest-summary.md").read_bytes(), summary_before)
        configure_runtime_security(ServerConfig())
        context = read_context("german", data_root=self.root)
        self.assertEqual(context["latest-summary.md"], request["session_markdown"])
        self.assertFalse((self.language_dir / ".transactions" / "pending").exists())

    def test_unexpected_third_value_blocks_recovery(self) -> None:
        request = self.request(self.context_versions())
        original_atomic_write = memory_storage.atomic_write

        def fail_on_live_summary(path: Path, content: bytes) -> None:
            if path == self.language_dir / "latest-summary.md":
                raise OSError("simulated interruption")
            original_atomic_write(path, content)

        with patch("memory_storage.atomic_write", side_effect=fail_on_live_summary):
            with self.assertRaises(memory_storage.RecoveryRequiredError):
                self.finalize(**request)
        (self.language_dir / "02-progress.md").write_text("# External edit", encoding="utf-8")

        with self.assertRaisesRegex(memory_storage.RecoveryRequiredError, "changed unexpectedly"):
            memory_storage.recover_pending(self.language_dir)

        self.assertTrue((self.language_dir / ".transactions" / "pending").is_dir())

    def test_stale_unchanged_file_version_rejects_without_mutation(self) -> None:
        versions = self.context_versions()
        progress_version = versions["02-progress.md"]
        (self.language_dir / "04-mistakes.md").write_text("# Newer state", encoding="utf-8")
        request = self.request(versions)
        original_progress = (self.language_dir / "02-progress.md").read_bytes()

        with self.assertRaises(memory_storage.VersionConflictError):
            self.finalize(**request)

        self.assertEqual((self.language_dir / "02-progress.md").read_bytes(), original_progress)
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 0)
        self.assertNotEqual(progress_version, "absent")

    def test_checkpoint_reset_is_in_the_same_recoverable_group(self) -> None:
        context = read_context("german", data_root=self.root)
        checkpoint = "# Checkpoint\n\nResume delayed destination retrieval."
        save_checkpoint(
            "german",
            checkpoint,
            context["_meta"]["file_versions"]["active-session.md"],
            data_root=self.root,
        )
        request = self.request(self.context_versions())
        self.finalize(**request)
        reset = (self.language_dir / "active-session.md").read_text("utf-8")
        self.assertNotEqual(reset, checkpoint)
        self.assertIn("No active session checkpoint", reset)


if __name__ == "__main__":
    unittest.main()

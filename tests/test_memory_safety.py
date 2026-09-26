from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_storage
import server
from server import (
    ServerConfig,
    configure_runtime_security,
    context_status,
    initialize_profile,
    read_context,
    read_language_file_storage,
    write_file,
    write_note,
    _run_tool,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MemorySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "tutor_data"
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

    def version(self, filename: str) -> str:
        return read_context("german", data_root=self.root)["_meta"]["file_versions"][filename]

    def test_unicode_hash_and_paging_use_exact_file_bytes_and_characters(self) -> None:
        content = "# Wörter\n\nGrüße, schön, 漢字."
        file_path = self.root / "german" / "notes" / "unicode.md"
        file_path.parent.mkdir(exist_ok=True)
        text_path = self.root / "german" / "03-vocabulary.md"
        text_path.write_bytes(content.encode("utf-8"))
        first = read_language_file_storage(
            "german", "03-vocabulary.md", offset_chars=0, limit_chars=5, data_root=self.root
        )
        second = read_language_file_storage(
            "german",
            "03-vocabulary.md",
            offset_chars=first["next_offset_chars"],
            limit_chars=5,
            expected_version=first["version"],
            data_root=self.root,
        )
        self.assertEqual(first["version"], "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertEqual(first["content"] + second["content"], content[:10])
        self.assertEqual(second["offset_chars"], 5)
        self.assertFalse(second["complete"])

    def test_stale_replacement_is_rejected_and_previous_copy_is_kept(self) -> None:
        old_version = self.version("02-progress.md")
        original = (self.root / "german" / "02-progress.md").read_text("utf-8")
        write_file(
            "german",
            "02-progress.md",
            "# Progress\n\nA saved an observation.",
            old_version,
            data_root=self.root,
        )

        with self.assertRaises(memory_storage.VersionConflictError):
            write_file(
                "german",
                "02-progress.md",
                "# Progress\n\nB's stale replacement.",
                old_version,
                data_root=self.root,
            )

        language = self.root / "german"
        self.assertIn("A saved", (language / "02-progress.md").read_text("utf-8"))
        backups = list((language / "archives" / "02-progress").glob("*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text("utf-8"), original)

    def test_blank_memory_and_oversized_replacements_fail_before_backup(self) -> None:
        old_version = self.version("02-progress.md")
        original = (self.root / "german" / "02-progress.md").read_bytes()
        for invalid in ("", " \n\t"):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                write_file("german", "02-progress.md", invalid, old_version, data_root=self.root)
        with self.assertRaises(memory_storage.OversizedFileError):
            write_file(
                "german",
                "02-progress.md",
                "x" * (memory_storage.CUMULATIVE_LIMIT_CHARS + 1),
                old_version,
                data_root=self.root,
            )
        self.assertEqual((self.root / "german" / "02-progress.md").read_bytes(), original)
        self.assertEqual(list((self.root / "german" / "archives" / "02-progress").glob("*.md")), [])

    def test_delivery_drafts_allow_deliberate_empty_content(self) -> None:
        current = read_language_file_storage(
            "german", "delivery/latest-email.md", data_root=self.root
        )
        saved = write_file(
            "german",
            "delivery/latest-email.md",
            "",
            current["version"],
            data_root=self.root,
        )
        self.assertEqual(saved["file_version"], "sha256:" + hashlib.sha256(b"").hexdigest())
        self.assertEqual((self.root / "german" / "delivery" / "latest-email.md").read_text("utf-8"), "")

    def test_failed_atomic_replacement_keeps_old_file_and_cleans_temporary_file(self) -> None:
        note = self.root / "german" / "notes" / "new-note.md"
        with patch("memory_storage.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                write_note("german", "new-note.md", "# Note\n\nSaved", "absent", data_root=self.root)
        self.assertFalse(note.exists())
        self.assertEqual(list(note.parent.glob("*.tmp")), [])
        self.assertEqual(list(note.parent.glob(".new-note.*.tmp")), [])

    def test_audit_failure_does_not_change_success_or_hide_original_error(self) -> None:
        blocking_file = Path(self.temporary_directory.name) / "not-a-directory"
        blocking_file.write_text("x", encoding="utf-8")
        configure_runtime_security(
            ServerConfig(audit_log=blocking_file / "audit.jsonl")
        )

        self.assertEqual(_run_tool("read", lambda: {"ok": True}), {"ok": True})
        with self.assertRaisesRegex(ValueError, "the original operation failed"):
            _run_tool("read", lambda: (_ for _ in ()).throw(ValueError("the original operation failed")))

    def test_writer_lifetime_lock_rejects_a_second_writer_and_releases(self) -> None:
        first = memory_storage.WriterLock(self.root)
        second = memory_storage.WriterLock(self.root)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "Another writable"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_writer_lifetime_lock_excludes_another_process(self) -> None:
        child_script = textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path
            from memory_storage import WriterLock
            lock = WriterLock(Path(sys.argv[1]))
            lock.acquire()
            print("LOCKED", flush=True)
            time.sleep(30)
            """
        )
        child_python = getattr(sys, "_base_executable", sys.executable)
        child = subprocess.Popen(
            [child_python, "-c", child_script, str(self.root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaisesRegex(RuntimeError, "Another writable"):
                memory_storage.WriterLock(self.root).acquire()
        finally:
            child.terminate()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

        lock_after_exit = memory_storage.WriterLock(self.root)
        lock_after_exit.acquire()
        lock_after_exit.release()

    def test_server_shutdown_releases_its_writer_lock(self) -> None:
        with (
            patch("server.TUTOR_DATA_ROOT", self.root),
            patch("server.mcp.run", side_effect=RuntimeError("simulated server stop")),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated server stop"):
                server.run_server(ServerConfig(transport="stdio", audit_enabled=False))

        lock_after_shutdown = memory_storage.WriterLock(self.root)
        lock_after_shutdown.acquire()
        lock_after_shutdown.release()

    def test_oversized_file_status_does_not_read_or_recommend_unrelated_compaction(self) -> None:
        progress = self.root / "german" / "02-progress.md"
        progress.write_bytes(b"x" * (memory_storage.MAX_READ_BYTES + 1))

        status = context_status("german", data_root=self.root)

        self.assertTrue(status["context_size_incomplete"])
        self.assertTrue(status["files"]["02-progress.md"]["hard_read_limit_exceeded"])
        self.assertIsNone(status["files"]["02-progress.md"]["characters"])
        self.assertNotIn("02-progress.md", status["files_recommended_for_compaction"])
        with self.assertRaises(memory_storage.OversizedFileError):
            read_language_file_storage("german", "02-progress.md", data_root=self.root)


if __name__ == "__main__":
    unittest.main()

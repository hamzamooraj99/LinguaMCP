from __future__ import annotations

import asyncio
import inspect
import base64
import hashlib
import json
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import (
    ALLOWED_FILES,
    BearerAuthMiddleware,
    MCP_SERVER_INSTRUCTIONS,
    MEMORY_PROTOCOL,
    mcp,
    ServerConfig,
    append_session,
    available_languages,
    compact_file,
    configure_runtime_security,
    context_status,
    finalize_lesson_storage,
    initialize_profile,
    list_archives,
    list_notes,
    parse_server_config,
    read_context,
    read_language_file_storage,
    read_archive,
    read_note,
    save_checkpoint,
    write_file,
    write_note,
    _oauth_authorization_metadata,
    _oauth_authorize_submit,
    _oauth_token_response,
    _run_tool,
)
import server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StorageVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name) / "tutor_data"
        self.template_root = PROJECT_ROOT / "templates"

    def tearDown(self) -> None:
        configure_runtime_security(ServerConfig())
        self.temporary_directory.cleanup()

    def initialize(self) -> dict[str, object]:
        return initialize_profile(
            "German",
            "# Profil\n\nIch lerne Deutsch. Grüße!",
            "# Plan\n\nÜbe täglich.",
            data_root=self.data_root,
            template_root=self.template_root,
        )

    def test_initialization_creates_complete_profile_and_preserves_existing(self) -> None:
        result = self.initialize()
        language_dir = self.data_root / "german"

        self.assertEqual(set(result["created"]), set(ALLOWED_FILES))
        self.assertTrue((language_dir / "sessions").is_dir())
        self.assertTrue((language_dir / "delivery").is_dir())
        self.assertTrue((language_dir / "archives").is_dir())
        self.assertTrue((language_dir / "notes").is_dir())
        for filename in ALLOWED_FILES:
            self.assertTrue((language_dir / filename).is_file())

        self.initialize()
        self.assertIn("Grüße", (language_dir / "00-profile.md").read_text("utf-8"))

        initialize_profile(
            "german",
            "# Replaced profile",
            "# Replaced plan",
            True,
            {
                "00-profile.md": read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["00-profile.md"],
                "01-lesson-plan.md": read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["01-lesson-plan.md"],
            },
            data_root=self.data_root,
            template_root=self.template_root,
        )
        self.assertEqual(
            (language_dir / "00-profile.md").read_text("utf-8"),
            "# Replaced profile",
        )

    def test_read_context_and_utf8(self) -> None:
        self.initialize()
        context = read_context("german", data_root=self.data_root)

        self.assertEqual(context["language"], "german")
        self.assertIn("Grüße", context["00-profile.md"])
        self.assertEqual(context["memory_protocol"], MEMORY_PROTOCOL)
        self.assertIn("03-vocabulary.md", context["memory_protocol"])
        self.assertIn("04-mistakes.md", context["memory_protocol"])
        self.assertEqual(len(context), 13)
        self.assertNotIn("delivery/latest-email.md", context)
        self.assertEqual(context["_meta"]["file_versions"]["current-lesson-contract.md"], "absent")

    def test_server_instructions_are_compact_and_cross_client(self) -> None:
        self.assertLessEqual(len(MCP_SERVER_INSTRUCTIONS), 512)
        self.assertIn("read_language_context", MCP_SERVER_INSTRUCTIONS)
        self.assertIn("03 vocabulary", MCP_SERVER_INSTRUCTIONS)
        self.assertIn("04 mistakes", MCP_SERVER_INSTRUCTIONS)

    def test_tool_descriptions_explain_memory_workflow(self) -> None:
        tool_names = (
            "initialize_language_profile",
            "read_language_context",
            "write_language_file",
            "list_language_notes",
            "read_language_note",
            "write_language_note",
            "append_session_log",
            "finalize_lesson",
            "save_session_checkpoint",
            "get_language_context_status",
            "compact_language_file",
            "list_language_file_archives",
            "read_language_file_archive",
            "read_language_file",
            "list_language_sessions",
            "read_language_session",
            "create_lesson_contract",
            "read_lesson_contract",
            "update_lesson_contract_state",
            "assess_lesson_contract",
            "list_languages",
        )
        if hasattr(mcp, "get_tools"):
            tools = asyncio.run(mcp.get_tools())
            self.assertTrue(set(tool_names).issubset(tools))
            required = set(tools["finalize_lesson"].parameters["required"])
            descriptions = {
                name: tools[name].description or ""
                for name in (
                    "read_language_context", "write_language_file", "list_language_notes",
                    "read_language_note", "write_language_note", "append_session_log",
                    "read_language_file", "list_language_sessions", "read_language_session",
                    "save_session_checkpoint", "finalize_lesson", "create_lesson_contract",
                    "read_lesson_contract", "update_lesson_contract_state", "assess_lesson_contract",
                )
            }
        else:
            tools = {name: getattr(server, name) for name in tool_names}
            required = {
                name
                for name, parameter in inspect.signature(server.finalize_lesson).parameters.items()
                if parameter.default is inspect.Parameter.empty
            }
            descriptions = {
                name: tools[name].__doc__ or ""
                for name in (
                    "read_language_context", "write_language_file", "list_language_notes",
                    "read_language_note", "write_language_note", "append_session_log",
                    "read_language_file", "list_language_sessions", "read_language_session",
                    "save_session_checkpoint", "finalize_lesson", "create_lesson_contract",
                    "read_lesson_contract", "update_lesson_contract_state", "assess_lesson_contract",
                )
            }
        self.assertIn("Mandatory first read", descriptions["read_language_context"])
        self.assertIn("03 new vocabulary", descriptions["write_language_file"])
        self.assertIn("04 errors", descriptions["write_language_file"])
        self.assertIn("viewer under Other", descriptions["list_language_notes"])
        self.assertIn("Read the current note", descriptions["read_language_note"])
        self.assertIn(
            "gender-noun-conventions.md", descriptions["write_language_note"]
        )
        self.assertIn("preserves active-session.md", descriptions["append_session_log"])
        self.assertIn("not a final", descriptions["save_session_checkpoint"])
        self.assertIn(
            "enforce the cumulative-memory checklist",
            descriptions["finalize_lesson"],
        )
        self.assertIn("bounded, version-checked character pages", descriptions["read_language_file"])
        self.assertIn("bounded cursor", descriptions["list_language_sessions"])
        self.assertIn("version-checked pages", descriptions["read_language_session"])
        self.assertIn("Read full context first", descriptions["create_lesson_contract"])
        self.assertIn("active contract governs lesson scope", descriptions["read_lesson_contract"])
        self.assertIn("clear prior assessment", descriptions["update_lesson_contract_state"])
        self.assertIn("Assess every required competency", descriptions["assess_lesson_contract"])
        self.assertEqual(
            required,
            {
                "language",
                "session_markdown",
                "homework_markdown",
                "updates",
                "unchanged_files",
                "expected_versions",
                "operation_id",
            },
        )

    def test_write_allowed_file(self) -> None:
        self.initialize()
        write_file(
            "german",
            "02-progress.md",
            "# Fortschritt\n\nSchön!",
            read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["02-progress.md"],
            data_root=self.data_root,
        )
        self.assertIn(
            "Schön",
            read_context("german", data_root=self.data_root)["02-progress.md"],
        )

    def test_custom_notes_can_be_created_listed_read_and_updated(self) -> None:
        self.initialize()
        filename = "gender-noun-conventions.md"
        first = "# German noun gender\n\n## Feminine\n\n- `-ung` is usually feminine."
        updated = first + "\n\n## Masculine\n\n- Days of the week are masculine."

        created = write_note("german", filename, first, "absent", data_root=self.data_root)
        self.assertTrue(created["created"])
        self.assertEqual(
            list_notes("german", data_root=self.data_root), [filename]
        )
        self.assertEqual(
            read_note("german", filename, data_root=self.data_root)["content"],
            first,
        )

        current_version = read_note("german", filename, data_root=self.data_root)["version"]
        replaced = write_note("german", filename, updated, current_version, data_root=self.data_root)
        self.assertFalse(replaced["created"])
        self.assertEqual(
            read_note("german", filename, data_root=self.data_root)["content"],
            updated,
        )

    def test_custom_notes_reject_paths_non_markdown_and_missing_files(self) -> None:
        self.initialize()
        for filename in (
            "../outside.md",
            "nested/note.md",
            "C:\\outside.md",
            ".hidden.md",
            "note.txt",
            "two.dots.md",
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                write_note("german", filename, "bad", "absent", data_root=self.data_root)

        with self.assertRaisesRegex(ValueError, "does not exist"):
            read_note("german", "missing.md", data_root=self.data_root)

    def test_invalid_paths_and_filenames_are_rejected(self) -> None:
        self.initialize()
        invalid_languages = ("../outside", "german/../../outside", "C:\\temp")
        for language in invalid_languages:
            with self.subTest(language=language), self.assertRaises(ValueError):
                read_context(language, data_root=self.data_root)

        for filename in (
            "../notes.md",
            "sessions/evil.md",
            "delivery/../../evil.md",
            "notes.txt",
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                write_file("german", filename, "bad", "absent", data_root=self.data_root)

    def test_session_log_is_timestamped_and_updates_latest_summary(self) -> None:
        self.initialize()
        checkpoint = "# Checkpoint\n\nRetest the destination question tomorrow."
        checkpoint_version = read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["active-session.md"]
        save_checkpoint("german", checkpoint, checkpoint_version, data_root=self.data_root)
        summary = "# Zusammenfassung\n\nHeute: Grüße und Café."
        summary_version = read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["latest-summary.md"]
        fixed_time = datetime(2026, 6, 21, 10, 11, 12, 123456, tzinfo=timezone.utc)

        result = append_session(
            "german",
            summary,
            {"latest-summary.md": summary_version},
            str(uuid.uuid4()),
            data_root=self.data_root,
            template_root=self.template_root,
            now=fixed_time,
        )
        expected = "sessions/2026-06-21T10-11-12-123456Z.md"

        self.assertEqual(result["session_file"], expected)
        self.assertEqual(
            (self.data_root / "german" / expected).read_text("utf-8"), summary
        )
        self.assertEqual(
            (self.data_root / "german" / "latest-summary.md").read_text("utf-8"),
            summary,
        )
        self.assertEqual(
            (self.data_root / "german" / "active-session.md").read_text("utf-8"),
            checkpoint,
        )
        self.assertEqual(available_languages(data_root=self.data_root), ["german"])

    def test_finalize_lesson_updates_cumulative_files_before_session(self) -> None:
        self.initialize()
        session_summary = (
            "# Language Session Summary\n\n"
            "## Practiced\n\nPhase 0 German placement conversation."
        )
        homework = "# Language Homework\n\nReview greetings and corrections."
        fixed_time = datetime(2026, 6, 21, 14, 0, 0, 0, tzinfo=timezone.utc)
        context = read_context("german", data_root=self.data_root)
        expected_versions = {
            filename: context["_meta"]["file_versions"][filename]
            for filename in (
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
        }

        result = finalize_lesson_storage(
            "german",
            session_summary,
            homework,
            {
                "00-profile.md": "# Profil\n\nAvoid English grammar terminology.",
                "01-lesson-plan.md": "# Plan\n\nContinue with Phase 1.",
                "02-progress.md": "# Progress\n\nPlacement observations recorded.",
                "03-vocabulary.md": "# Vocabulary\n\n- die Begrüßung — greeting",
                "04-mistakes.md": "# Mistakes\n\n- Corrected word order in introductions.",
            },
            ["05-scenarios.md"],
            expected_versions,
            str(uuid.uuid4()),
            data_root=self.data_root,
            template_root=self.template_root,
            now=fixed_time,
        )

        language_dir = self.data_root / "german"
        self.assertTrue(result["finalized"])
        self.assertEqual(
            result["updated_files"],
            [
                "00-profile.md",
                "01-lesson-plan.md",
                "02-progress.md",
                "03-vocabulary.md",
                "04-mistakes.md",
                "latest-homework.md",
                "latest-summary.md",
            ],
        )
        self.assertEqual(result["unchanged_files"], ["05-scenarios.md"])
        self.assertIn(
            "die Begrüßung",
            (language_dir / "03-vocabulary.md").read_text("utf-8"),
        )
        self.assertIn(
            "word order",
            (language_dir / "04-mistakes.md").read_text("utf-8"),
        )
        self.assertEqual(
            (language_dir / "latest-homework.md").read_text("utf-8"),
            homework,
        )
        self.assertEqual(
            (language_dir / "latest-summary.md").read_text("utf-8"),
            session_summary,
        )
        self.assertIn(
            "No active session checkpoint",
            (language_dir / "active-session.md").read_text("utf-8"),
        )

    def test_finalize_lesson_requires_a_decision_for_every_cumulative_file(self) -> None:
        self.initialize()

        with self.assertRaisesRegex(ValueError, "Missing"):
            finalize_lesson_storage(
                "german",
                "# Summary",
                "# Homework",
                {"03-vocabulary.md": "# Vocabulary\n\n- Hallo"},
                ["00-profile.md", "01-lesson-plan.md", "05-scenarios.md"],
                {},
                str(uuid.uuid4()),
                data_root=self.data_root,
                template_root=self.template_root,
            )

    def test_checkpoint_is_bounded_and_delivery_drafts_are_writable(self) -> None:
        self.initialize()
        first = "# Checkpoint\n\nFirst state"
        second = "# Checkpoint\n\nCurrent state"
        checkpoint_version = read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["active-session.md"]
        save_checkpoint("german", first, checkpoint_version, data_root=self.data_root)
        checkpoint_version = read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["active-session.md"]
        save_checkpoint("german", second, checkpoint_version, data_root=self.data_root)
        delivery_version = read_language_file_storage(
            "german", "delivery/latest-whatsapp.md", data_root=self.data_root
        )["version"]
        write_file(
            "german",
            "delivery/latest-whatsapp.md",
            "Homework: review greetings.",
            delivery_version,
            data_root=self.data_root,
        )

        language_dir = self.data_root / "german"
        self.assertEqual(
            (language_dir / "active-session.md").read_text("utf-8"), second
        )
        self.assertEqual(
            (language_dir / "delivery" / "latest-whatsapp.md").read_text(
                "utf-8"
            ),
            "Homework: review greetings.",
        )

    def test_context_status_and_compaction_preserve_full_archive(self) -> None:
        self.initialize()
        original = "# Vocabulary\n\n" + ("Wort — word\n" * 20)
        compacted = "# Vocabulary\n\n- Wort — word"
        current_version = read_context("german", data_root=self.data_root)["_meta"]["file_versions"]["03-vocabulary.md"]
        write_result = write_file(
            "german",
            "03-vocabulary.md",
            original,
            current_version,
            data_root=self.data_root,
        )

        status = context_status(
            "german", data_root=self.data_root, threshold_chars=100
        )
        self.assertIn(
            "03-vocabulary.md", status["files_recommended_for_compaction"]
        )

        fixed_time = datetime(2026, 6, 21, 12, 0, 0, 0, tzinfo=timezone.utc)
        result = compact_file(
            "german",
            "03-vocabulary.md",
            compacted,
            write_result["file_version"],
            data_root=self.data_root,
            now=fixed_time,
        )
        archive_path = self.data_root / "german" / result["archive_file"]

        self.assertEqual(archive_path.read_text("utf-8"), original)
        self.assertEqual(
            (self.data_root / "german" / "03-vocabulary.md").read_text("utf-8"),
            compacted,
        )
        archive_name = Path(result["archive_file"]).name
        archive_names = list_archives(
            "german", "03-vocabulary.md", data_root=self.data_root
        )
        self.assertIn(archive_name, archive_names)
        self.assertEqual(len(archive_names), 2)
        self.assertEqual(
            read_archive(
                "german",
                "03-vocabulary.md",
                archive_name,
                data_root=self.data_root,
            )["content"],
            original,
        )

        total_status = context_status(
            "german",
            data_root=self.data_root,
            threshold_chars=1_000_000,
            total_threshold_chars=1,
        )
        self.assertTrue(total_status["total_context_compaction_recommended"])
        self.assertEqual(len(total_status["files_recommended_for_compaction"]), 1)

        with self.assertRaises(ValueError):
            compact_file(
                "german",
                "latest-summary.md",
                "not allowed",
                "absent",
                data_root=self.data_root,
            )
        with self.assertRaises(ValueError):
            read_archive(
                "german",
                "03-vocabulary.md",
                "../outside.md",
                data_root=self.data_root,
            )

    def test_oversized_checkpoint_is_reported_without_recommending_vocabulary_compaction(self) -> None:
        self.initialize()
        checkpoint = self.data_root / "german" / "active-session.md"
        checkpoint.write_bytes(("# Checkpoint\n\n" + ("Resume the open exercise. " * 1500)).encode("utf-8"))

        status = context_status("german", data_root=self.data_root)

        self.assertEqual(status["files"]["active-session.md"]["action"], "shorten_volatile_file")
        self.assertFalse(status["files"]["active-session.md"]["compactable"])
        self.assertNotIn("03-vocabulary.md", status["files_recommended_for_compaction"])

    def test_server_config_defaults_to_stdio(self) -> None:
        config = parse_server_config([])

        self.assertEqual(config.transport, "stdio")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.path, "/mcp")
        self.assertTrue(config.allow_writes)
        self.assertTrue(config.audit_enabled)

    def test_server_config_supports_http_mode(self) -> None:
        config = parse_server_config(
            [
                "--http",
                "--allow-writes",
                "--host",
                "0.0.0.0",
                "--port",
                "8123",
                "--path",
                "/lingua",
                "--log-level",
                "debug",
            ]
        )

        self.assertEqual(config.transport, "http")
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 8123)
        self.assertEqual(config.path, "/lingua")
        self.assertEqual(config.log_level, "debug")
        self.assertTrue(config.allow_writes)

    def test_server_config_http_mode_is_read_only_by_default(self) -> None:
        config = parse_server_config(["--http"])

        self.assertEqual(config.transport, "http")
        self.assertFalse(config.allow_writes)

    def test_server_config_supports_oauth_mode(self) -> None:
        clients = self.data_root / "oauth-clients.json"
        config = parse_server_config([
            "--http", "--oauth", "--oauth-clients-file", str(clients),
            "--public-base-url", "https://tutor.example", "--oauth-state-path",
            str(self.data_root / ".oauth" / "state.sqlite3"), "--revoke-oauth-grants",
        ])

        self.assertTrue(config.oauth_enabled)
        self.assertEqual(config.oauth_password_env, "LINGUAMCP_OAUTH_PASSWORD")
        self.assertEqual(config.oauth_clients_file, clients)
        self.assertEqual(config.public_base_url, "https://tutor.example")
        self.assertEqual(config.oauth_state_path, self.data_root / ".oauth" / "state.sqlite3")
        self.assertTrue(config.revoke_oauth_grants)

    def test_server_config_rejects_invalid_http_options(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_server_config(["--http", "--port", "70000"])

        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_server_config(["--http", "--path", "mcp"])

    def test_runtime_security_blocks_write_tools_and_audits(self) -> None:
        audit_log = self.data_root / "audit-log.jsonl"
        configure_runtime_security(
            ServerConfig(allow_writes=False, audit_log=audit_log)
        )

        with self.assertRaises(PermissionError):
            _run_tool(
                "save_session_checkpoint",
                lambda: self.fail("Blocked write should not execute."),
                writes=True,
                language="german",
            )

        audit = audit_log.read_text("utf-8")
        self.assertIn('"tool": "save_session_checkpoint"', audit)
        self.assertIn('"status": "blocked_write"', audit)
        self.assertNotIn("# Checkpoint", audit)

    def test_oauth_metadata_excludes_unimplemented_dynamic_registration(self) -> None:
        request = SimpleNamespace(base_url="https://example.test/")

        metadata = _oauth_authorization_metadata(request)

        self.assertFalse(metadata["client_id_metadata_document_supported"])
        self.assertEqual(metadata["grant_types_supported"], ["authorization_code", "refresh_token"])
        self.assertEqual(metadata["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])


if __name__ == "__main__":
    unittest.main()

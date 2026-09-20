from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from server import (
    ALLOWED_FILES,
    MCP_SERVER_INSTRUCTIONS,
    MEMORY_PROTOCOL,
    mcp,
    ServerConfig,
    OAUTH_AUTH_CODES,
    OAUTH_ACCESS_TOKENS,
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
    read_archive,
    read_note,
    save_checkpoint,
    write_file,
    write_note,
    _oauth_authorize_submit,
    _oauth_token_response,
    _run_tool,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StorageVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary_directory.name) / "tutor_data"
        self.template_root = PROJECT_ROOT / "templates"

    def tearDown(self) -> None:
        configure_runtime_security(ServerConfig())
        OAUTH_AUTH_CODES.clear()
        OAUTH_ACCESS_TOKENS.clear()
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
        self.assertEqual(len(context), 11)
        self.assertNotIn("delivery/latest-email.md", context)

    def test_server_instructions_are_compact_and_cross_client(self) -> None:
        self.assertLessEqual(len(MCP_SERVER_INSTRUCTIONS), 512)
        self.assertIn("read_language_context", MCP_SERVER_INSTRUCTIONS)
        self.assertIn("03 vocabulary", MCP_SERVER_INSTRUCTIONS)
        self.assertIn("04 mistakes", MCP_SERVER_INSTRUCTIONS)

    def test_tool_descriptions_explain_memory_workflow(self) -> None:
        tools = asyncio.run(mcp.get_tools())
        self.assertEqual(len(tools), 14)
        descriptions = {
            name: tools[name].description or ""
            for name in (
                "read_language_context",
                "write_language_file",
                "list_language_notes",
                "read_language_note",
                "write_language_note",
                "append_session_log",
                "save_session_checkpoint",
                "finalize_lesson",
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
        self.assertIn("does not update profile", descriptions["append_session_log"])
        self.assertIn("not a final", descriptions["save_session_checkpoint"])
        self.assertIn(
            "enforce the cumulative-memory checklist",
            descriptions["finalize_lesson"],
        )
        self.assertEqual(
            set(tools["finalize_lesson"].parameters["required"]),
            {
                "language",
                "session_markdown",
                "homework_markdown",
                "updates",
                "unchanged_files",
            },
        )

    def test_write_allowed_file(self) -> None:
        self.initialize()
        write_file(
            "german",
            "02-progress.md",
            "# Fortschritt\n\nSchön!",
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

        created = write_note(
            "german", filename, first, data_root=self.data_root
        )
        self.assertTrue(created["created"])
        self.assertEqual(
            list_notes("german", data_root=self.data_root), [filename]
        )
        self.assertEqual(
            read_note("german", filename, data_root=self.data_root)["content"],
            first,
        )

        replaced = write_note(
            "german", filename, updated, data_root=self.data_root
        )
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
                write_note("german", filename, "bad", data_root=self.data_root)

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
                write_file("german", filename, "bad", data_root=self.data_root)

    def test_session_log_is_timestamped_and_updates_latest_summary(self) -> None:
        self.initialize()
        summary = "# Zusammenfassung\n\nHeute: Grüße und Café."
        fixed_time = datetime(2026, 6, 21, 10, 11, 12, 123456, tzinfo=timezone.utc)

        result = append_session(
            "german",
            summary,
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
        self.assertIn(
            "No active session checkpoint",
            (self.data_root / "german" / "active-session.md").read_text("utf-8"),
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
                data_root=self.data_root,
                template_root=self.template_root,
            )

    def test_checkpoint_is_bounded_and_delivery_drafts_are_writable(self) -> None:
        self.initialize()
        first = "# Checkpoint\n\nFirst state"
        second = "# Checkpoint\n\nCurrent state"
        save_checkpoint("german", first, data_root=self.data_root)
        save_checkpoint("german", second, data_root=self.data_root)
        write_file(
            "german",
            "delivery/latest-whatsapp.md",
            "Homework: review greetings.",
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
        write_file(
            "german", "03-vocabulary.md", original, data_root=self.data_root
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
        self.assertEqual(
            list_archives(
                "german", "03-vocabulary.md", data_root=self.data_root
            ),
            [archive_name],
        )
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
                data_root=self.data_root,
            )
        with self.assertRaises(ValueError):
            read_archive(
                "german",
                "03-vocabulary.md",
                "../outside.md",
                data_root=self.data_root,
            )

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
        config = parse_server_config(["--http", "--oauth"])

        self.assertTrue(config.oauth_enabled)
        self.assertEqual(config.oauth_password_env, "LINGUAMCP_OAUTH_PASSWORD")

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

    def test_oauth_token_exchange_validates_pkce_and_issues_bearer(self) -> None:
        verifier = "test-verifier"
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        OAUTH_AUTH_CODES["test-code"] = {
            "client_id": "chatgpt",
            "redirect_uri": "https://chatgpt.com/oauth/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "expires_at": 9_999_999_999,
        }

        response = _oauth_token_response(
            {
                "grant_type": "authorization_code",
                "code": "test-code",
                "redirect_uri": "https://chatgpt.com/oauth/callback",
                "client_id": "chatgpt",
                "code_verifier": verifier,
            }
        )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["token_type"], "Bearer")
        self.assertIn(body["access_token"], OAUTH_ACCESS_TOKENS)

    def test_oauth_authorization_redirect_preserves_callback_and_state(self) -> None:
        form = {
            "password": "approval-password",
            "response_type": "code",
            "client_id": "chatgpt",
            "redirect_uri": "https://chatgpt.com/connector/oauth/callback?existing=1",
            "state": "oauth_s_6aaf7d13d8b48191be85a86636052ad5",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }

        with patch("server.secrets.token_urlsafe", return_value="test-code"):
            response = _oauth_authorize_submit(None, form, "approval-password")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "https://chatgpt.com/connector/oauth/callback?existing=1"
            "&code=test-code"
            "&state=oauth_s_6aaf7d13d8b48191be85a86636052ad5",
        )
        self.assertEqual(
            OAUTH_AUTH_CODES["test-code"]["redirect_uri"],
            form["redirect_uri"],
        )

    def test_oauth_token_exchange_rejects_bad_pkce(self) -> None:
        OAUTH_AUTH_CODES["test-code"] = {
            "client_id": "chatgpt",
            "redirect_uri": "https://chatgpt.com/oauth/callback",
            "code_challenge": "expected",
            "code_challenge_method": "plain",
            "expires_at": 9_999_999_999,
        }

        response = _oauth_token_response(
            {
                "grant_type": "authorization_code",
                "code": "test-code",
                "redirect_uri": "https://chatgpt.com/oauth/callback",
                "client_id": "chatgpt",
                "code_verifier": "wrong",
            }
        )

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()

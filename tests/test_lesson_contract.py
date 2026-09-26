from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
import server

from lesson_contract import ContractValidationError
from lesson_contract import parse_contract_document
from memory_storage import VersionConflictError
from server import (
    ServerConfig,
    append_session,
    assess_lesson_contract_storage,
    create_lesson_contract_storage,
    configure_runtime_security,
    finalize_lesson_storage,
    initialize_profile,
    read_context,
    read_lesson_contract_storage,
    save_checkpoint,
    update_lesson_contract_state_storage,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_BODY = """# Lesson 6: Getting Around

## Lesson Purpose
Ask how to reach a destination.
## Required Vocabulary and Constructions
Use the taught destination question and one familiar place.
## Prior Material to Interleave
Retrieve greetings during warm-up.
## Suggested Teaching Progression
Model once, practise with support, then retrieve independently.
## Retrieval and Practice Requirements
Mix the target phrase with familiar conversation.
## Scope Boundaries
One destination question; not a full directions course.
## Deferred Topics
Detailed case theory.
## Optional Enrichment
Try a second destination if useful.
## Critical Gaps
Independent word order is uncertain.
## Completion Rule
Meet the criterion after delayed mixed retrieval with no critical gap.
"""
COMPETENCY = {
    "id": "ask_destination",
    "criterion": "Ask how to reach a destination after mixed retrieval.",
    "critical": True,
    "required_evidence": "delayed_or_mixed",
}


def passing_evidence() -> list[dict[str, object]]:
    return [{
        "competency_id": "ask_destination",
        "criterion_met": True,
        "evidence_level": "delayed_or_mixed",
        "evidence": "After intervening practice, independently asked Wie komme ich zum Bahnhof?",
    }]


class LessonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "tutor_data"
        self.language_dir = self.root / "german"
        initialize_profile(
            "german",
            "# Profile\n\nLearns Deutsch.",
            "# Plan\n\nPractise everyday questions.",
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
        )

    def tearDown(self) -> None:
        configure_runtime_security(ServerConfig())
        self.temporary_directory.cleanup()

    def create(self, expected_version: str = "") -> dict[str, object]:
        return create_lesson_contract_storage(
            "german",
            "6",
            "Getting Around",
            CONTRACT_BODY,
            [COMPETENCY],
            expected_version,
            data_root=self.root,
        )

    def test_legacy_context_and_contract_pages_are_compatible(self) -> None:
        context = read_context("german", data_root=self.root)
        self.assertEqual(
            context["current-lesson-contract.md"], ""
        )
        self.assertEqual(
            context["_meta"]["file_versions"]["current-lesson-contract.md"], "absent"
        )
        absent = read_lesson_contract_storage("german", data_root=self.root)
        self.assertFalse(absent["present"])
        self.assertTrue(absent["complete"])

        created = self.create()
        first = read_lesson_contract_storage("german", limit_chars=240, data_root=self.root)
        second = read_lesson_contract_storage(
            "german",
            offset_chars=first["next_offset_chars"],
            limit_chars=240,
            expected_file_version=first["file_version"],
            data_root=self.root,
        )
        self.assertEqual(first["file_version"], created["file_version"])
        self.assertEqual(first["contract_id"], created["contract_id"])
        self.assertEqual(first["content"] + second["content"], (self.language_dir / "current-lesson-contract.md").read_text("utf-8")[:480])
        self.assertFalse(first["complete"])

    def test_large_contract_counts_toward_context_but_never_triggers_compaction(self) -> None:
        long_body = CONTRACT_BODY.replace(
            "Try a second destination if useful.", "x" * 63_000
        )
        create_lesson_contract_storage(
            "german", "6", "Getting Around", long_body, [COMPETENCY], data_root=self.root
        )

        context = read_context("german", data_root=self.root)
        self.assertEqual(context["error"]["code"], "context_too_large")
        status = server.context_status(
            "german", data_root=self.root, threshold_chars=12_000, total_threshold_chars=1_000_000
        )
        contract_status = status["files"]["current-lesson-contract.md"]
        self.assertGreater(contract_status["characters"], 60_000)
        self.assertFalse(contract_status["compactable"])
        self.assertFalse(contract_status["compaction_recommended"])
        self.assertEqual(contract_status["action"], "deliberately_revise_contract")
        self.assertNotIn("03-vocabulary.md", status["files_recommended_for_compaction"])

        page = read_lesson_contract_storage("german", limit_chars=500, data_root=self.root)
        self.assertFalse(page["complete"])
        self.assertEqual(page["next_offset_chars"], 500)

    def test_updates_clear_assessment_and_replacement_resets_lesson_state(self) -> None:
        created = self.create()
        assessment = assess_lesson_contract_storage(
            "german",
            str(created["version_token"]),
            [{
                "competency_id": "ask_destination",
                "criterion_met": False,
                "evidence_level": "supported",
                "evidence": "Needed a model before answering.",
            }],
            ["ask_destination"],
            False,
            ["Retest word order independently."],
            data_root=self.root,
        )
        self.assertFalse(assessment["completion_ready"])

        updated = update_lesson_contract_state_storage(
            "german",
            str(assessment["version_token"]),
            [{
                "competency_id": "ask_destination",
                "status": "delayed_retest_required",
                "note": "Return to this after other practice.",
            }],
            data_root=self.root,
        )
        self.assertTrue(updated["assessment_cleared"])
        self.assertEqual(
            read_lesson_contract_storage("german", data_root=self.root)["status"],
            "active",
        )
        initialize_profile(
            "german",
            "# Updated profile",
            "# Updated plan",
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
        )
        self.assertEqual(
            read_lesson_contract_storage("german", data_root=self.root)["contract_id"],
            created["contract_id"],
        )
        with self.assertRaises(VersionConflictError):
            assess_lesson_contract_storage(
                "german",
                str(assessment["version_token"]),
                passing_evidence(),
                [],
                True,
                [],
                data_root=self.root,
            )

        replacement = self.create(str(updated["version_token"]))
        loaded = read_lesson_contract_storage("german", data_root=self.root)
        self.assertTrue(replacement["replaced"])
        self.assertNotEqual(replacement["contract_id"], created["contract_id"])
        self.assertEqual(loaded["status"], "active")
        self.assertEqual(loaded["version_token"], replacement["version_token"])
        metadata, body = parse_contract_document(
            (self.language_dir / "current-lesson-contract.md").read_text("utf-8")
        )
        self.assertEqual(body, CONTRACT_BODY)
        self.assertEqual(metadata["states"]["ask_destination"]["status"], "not_introduced")
        self.assertIsNone(metadata["assessment"])
        self.assertTrue((self.language_dir / ".backups" / "current-lesson-contract.md").is_file())

    def test_invalid_contract_is_rejected_and_read_only_mode_blocks_contract_writes(self) -> None:
        with self.assertRaises(ContractValidationError):
            create_lesson_contract_storage(
                "german",
                "6",
                "Getting Around",
                CONTRACT_BODY.replace("## Deferred Topics", "## Not Deferred Topics"),
                [COMPETENCY],
                data_root=self.root,
            )
        self.assertFalse((self.language_dir / "current-lesson-contract.md").exists())

        create_lesson_contract_storage(
            "german", "6", "Getting Around", CONTRACT_BODY, [COMPETENCY], data_root=self.root
        )
        contract = read_lesson_contract_storage("german", data_root=self.root)
        configure_runtime_security(ServerConfig(allow_writes=False, audit_enabled=False))
        def tool_function(name: str):
            tool = getattr(server, name)
            return getattr(tool, "fn", tool)

        with self.assertRaises(PermissionError):
            tool_function("create_lesson_contract")(
                "german", "7", "Next", CONTRACT_BODY, [COMPETENCY], str(contract["version_token"])
            )
        with self.assertRaises(PermissionError):
            tool_function("update_lesson_contract_state")(
                "german", str(contract["version_token"]),
                [{"competency_id": "ask_destination", "status": "introduced", "note": "Started."}],
            )
        with self.assertRaises(PermissionError):
            tool_function("assess_lesson_contract")(
                "german", str(contract["version_token"]), passing_evidence(), [], True, []
            )
        self.assertIsNone(parse_contract_document((self.language_dir / "current-lesson-contract.md").read_text("utf-8"))[0]["assessment"])

    def test_assessment_requires_every_competency_and_gates_finalization(self) -> None:
        created = self.create()
        with self.assertRaisesRegex(ContractValidationError, "Assess every"):
            assess_lesson_contract_storage(
                "german",
                str(created["version_token"]),
                [],
                [],
                True,
                [],
                data_root=self.root,
            )

        weak = assess_lesson_contract_storage(
            "german",
            str(created["version_token"]),
            [{
                "competency_id": "ask_destination",
                "criterion_met": True,
                "evidence_level": "supported",
                "evidence": "Could answer after a prompt.",
            }],
            [],
            True,
            [],
            data_root=self.root,
        )
        self.assertFalse(weak["completion_ready"])
        original_progress = (self.language_dir / "02-progress.md").read_bytes()
        current_versions = read_context("german", data_root=self.root)["_meta"]["file_versions"]
        with self.assertRaisesRegex(ContractValidationError, "assessment is not ready"):
            finalize_lesson_storage(
                "german",
                "# Summary\n\nPractised the phrase.",
                "# Homework\n\nReview the phrase.",
                {name: f"# {name}\n\nRecorded." for name in (
                    "00-profile.md", "01-lesson-plan.md", "02-progress.md",
                    "03-vocabulary.md", "04-mistakes.md", "05-scenarios.md",
                )},
                [],
                current_versions,
                str(uuid.uuid4()),
                str(weak["version_token"]),
                data_root=self.root,
                template_root=PROJECT_ROOT / "templates",
            )
        self.assertEqual((self.language_dir / "02-progress.md").read_bytes(), original_progress)
        self.assertEqual(list((self.language_dir / "sessions").glob("*.md")), [])

    def test_completion_is_recoverable_retryable_and_replaceable(self) -> None:
        created = self.create()
        starting_context = read_context("german", data_root=self.root)
        checkpoint = "# Checkpoint\n\nResume the delayed destination question."
        save_checkpoint(
            "german",
            checkpoint,
            starting_context["_meta"]["file_versions"]["active-session.md"],
            data_root=self.root,
        )
        paused = append_session(
            "german",
            "# Paused Session\n\nThe question still needs delayed practice.",
            {"latest-summary.md": starting_context["_meta"]["file_versions"]["latest-summary.md"]},
            str(uuid.uuid4()),
            data_root=self.root,
            template_root=PROJECT_ROOT / "templates",
        )
        self.assertFalse(paused["active_session_cleared"])
        self.assertEqual((self.language_dir / "active-session.md").read_text("utf-8"), checkpoint)

        weak = assess_lesson_contract_storage(
            "german",
            str(created["version_token"]),
            [{
                "competency_id": "ask_destination",
                "criterion_met": False,
                "evidence_level": "supported",
                "evidence": "Answered only after the tutor supplied the opening words.",
            }],
            ["ask_destination"],
            False,
            ["Retry the destination question after mixed practice."],
            data_root=self.root,
        )
        self.assertFalse(weak["completion_ready"])
        blocked_context = read_context("german", data_root=self.root)
        blocked_versions = dict(blocked_context["_meta"]["file_versions"])
        before_blocked_finalization = {
            name: (self.language_dir / name).read_bytes()
            for name in ("02-progress.md", "latest-homework.md", "latest-summary.md", "active-session.md")
        }
        incomplete_updates = {
            name: f"# {name}\n\nRecorded."
            for name in (
                "00-profile.md", "01-lesson-plan.md", "02-progress.md",
                "03-vocabulary.md", "04-mistakes.md", "05-scenarios.md",
            )
        }
        with self.assertRaisesRegex(ContractValidationError, "assessment is not ready"):
            finalize_lesson_storage(
                "german",
                "# Summary\n\nThe skill still needs practice.",
                "# Homework\n\nRetry the question.",
                incomplete_updates,
                [],
                blocked_versions,
                str(uuid.uuid4()),
                str(weak["version_token"]),
                data_root=self.root,
                template_root=PROJECT_ROOT / "templates",
            )
        self.assertEqual(
            {name: (self.language_dir / name).read_bytes() for name in before_blocked_finalization},
            before_blocked_finalization,
        )
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 1)
        self.assertEqual((self.language_dir / "active-session.md").read_text("utf-8"), checkpoint)

        assessment = assess_lesson_contract_storage(
            "german",
            str(weak["version_token"]),
            passing_evidence(),
            [],
            True,
            ["Review case endings in future mixed practice."],
            data_root=self.root,
        )
        self.assertTrue(assessment["completion_ready"])
        mastery = str(assessment["mastery_record_markdown"])
        self.assertIn("ask_destination", mastery)
        self.assertIn("delayed_or_mixed", mastery)

        context = read_context("german", data_root=self.root)
        versions = dict(context["_meta"]["file_versions"])
        updates = {
            "00-profile.md": "# Profile\n\nLearns Deutsch.",
            "01-lesson-plan.md": "# Plan\n\nContinue everyday questions.",
            "02-progress.md": "# Progress\n\n" + mastery,
            "03-vocabulary.md": "# Vocabulary\n\n- Wie komme ich ...? â€” How do I get ...?",
            "04-mistakes.md": "# Mistakes\n\n- Check question word order.",
            "05-scenarios.md": "# Scenarios\n\n- Ask for a train station.",
        }
        request = {
            "language": "german",
            "session_markdown": "# Summary\n\nIndependently asked how to reach the station.",
            "homework_markdown": "# Homework\n\nReview the destination question.",
            "updates": updates,
            "unchanged_files": [],
            "expected_versions": versions,
            "operation_id": str(uuid.uuid4()),
            "expected_contract_version": str(assessment["version_token"]),
            "data_root": self.root,
            "template_root": PROJECT_ROOT / "templates",
        }
        saved = finalize_lesson_storage(**request)
        self.assertTrue(saved["finalized"])
        self.assertEqual(saved["contract_status"], "completed")
        self.assertEqual(finalize_lesson_storage(**request), saved)
        self.assertEqual(len(list((self.language_dir / "sessions").glob("*.md"))), 2)
        self.assertIn(mastery, (self.language_dir / "02-progress.md").read_text("utf-8"))
        completed = read_lesson_contract_storage("german", data_root=self.root)
        self.assertEqual(completed["status"], "completed")
        self.assertNotEqual(completed["version_token"], assessment["version_token"])

        next_lesson = create_lesson_contract_storage(
            "german", "7", "At the Café", CONTRACT_BODY, [COMPETENCY],
            str(completed["version_token"]), data_root=self.root,
        )
        fresh = read_lesson_contract_storage("german", data_root=self.root)
        self.assertNotEqual(next_lesson["contract_id"], completed["contract_id"])
        self.assertEqual(fresh["status"], "active")
        next_metadata, _ = parse_contract_document(fresh["content"])
        self.assertEqual(next_metadata["states"]["ask_destination"]["status"], "not_introduced")
        self.assertIsNone(next_metadata["assessment"])
        self.assertIn(mastery, (self.language_dir / "02-progress.md").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()

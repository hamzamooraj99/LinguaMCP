"""Pure validation and rendering for the temporary lesson contract format."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any


CONTRACT_SCHEMA_VERSION = 1
CONTRACT_START = "<!-- linguamcp-contract:v1 -->"
CONTRACT_END = "<!-- /linguamcp-contract -->"
MASTERY_START = "<!-- lesson-mastery:{contract_id} -->"
MASTERY_END = "<!-- /lesson-mastery:{contract_id} -->"
CONTRACT_MAX_BYTES = 128 * 1024
MAX_COMPETENCIES = 30
COMPETENCY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EVIDENCE_LEVELS = (
    "not_observed",
    "introduced",
    "recognized",
    "supported",
    "independent",
    "delayed_or_mixed",
)
REQUIRED_EVIDENCE_LEVELS = frozenset(EVIDENCE_LEVELS[2:])
COMPETENCY_STATES = frozenset(
    {
        "not_introduced",
        "introduced",
        "practising",
        "demonstrated",
        "weak",
        "delayed_retest_required",
    }
)
REQUIRED_HEADINGS = (
    "Lesson Purpose",
    "Required Vocabulary and Constructions",
    "Prior Material to Interleave",
    "Suggested Teaching Progression",
    "Retrieval and Practice Requirements",
    "Scope Boundaries",
    "Deferred Topics",
    "Optional Enrichment",
    "Critical Gaps",
    "Completion Rule",
)
METADATA_KEYS = {
    "schema_version",
    "contract_id",
    "version_token",
    "lesson_id",
    "lesson_title",
    "status",
    "competencies",
    "states",
    "assessment",
}


class ContractValidationError(ValueError):
    """The lesson contract is malformed or does not meet its schema."""


def _bounded_string(value: Any, name: str, limit: int, *, nonblank: bool = True) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{name} must be a string.")
    if len(value) > limit:
        raise ContractValidationError(f"{name} exceeds {limit} characters.")
    if nonblank and not value.strip():
        raise ContractValidationError(f"{name} cannot be empty or whitespace.")
    if CONTRACT_START in value or CONTRACT_END in value:
        raise ContractValidationError(f"{name} contains a reserved contract marker.")
    return value


def _uuid(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{name} must be a UUID.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractValidationError(f"{name} must be a UUID.") from exc
    if str(parsed) != value:
        raise ContractValidationError(f"{name} must use canonical UUID form.")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"The stored contract contains duplicate JSON key {key}.")
        result[key] = value
    return result


def _validate_body(body: str) -> str:
    _bounded_string(body, "contract_markdown", 120_000)
    lines = body.splitlines()
    for heading in REQUIRED_HEADINGS:
        expected = f"## {heading}"
        matches = [index for index, line in enumerate(lines) if line == expected]
        if len(matches) != 1:
            raise ContractValidationError(f"contract_markdown must contain one '## {heading}' heading.")
        index = matches[0]
        end = next(
            (i for i in range(index + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        if not any(line.strip() for line in lines[index + 1 : end]):
            raise ContractValidationError(f"The '## {heading}' section needs nonblank content.")
    return body


def validate_competencies(competencies: Any) -> list[dict[str, Any]]:
    if not isinstance(competencies, list) or not 1 <= len(competencies) <= MAX_COMPETENCIES:
        raise ContractValidationError(f"competencies must contain 1 to {MAX_COMPETENCIES} required items.")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(competencies):
        if not isinstance(item, dict) or set(item) != {"id", "criterion", "critical", "required_evidence"}:
            raise ContractValidationError(f"competencies[{index}] must contain id, criterion, critical, and required_evidence only.")
        identifier = item["id"]
        if not isinstance(identifier, str) or not COMPETENCY_ID_PATTERN.fullmatch(identifier):
            raise ContractValidationError(f"competencies[{index}].id has an invalid format.")
        if identifier in seen:
            raise ContractValidationError(f"Duplicate competency ID: {identifier}.")
        seen.add(identifier)
        criterion = _bounded_string(item["criterion"], f"competencies[{index}].criterion", 1000)
        critical = item["critical"]
        if not isinstance(critical, bool):
            raise ContractValidationError(f"competencies[{index}].critical must be a boolean.")
        required = item["required_evidence"]
        if not isinstance(required, str) or required not in REQUIRED_EVIDENCE_LEVELS:
            raise ContractValidationError(f"competencies[{index}].required_evidence is unsupported.")
        result.append({
            "id": identifier,
            "criterion": criterion,
            "critical": critical,
            "required_evidence": required,
        })
    return result


def initial_states(competencies: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {item["id"]: {"status": "not_introduced", "note": ""} for item in competencies}


def validate_state_map(states: Any, competencies: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    expected = {item["id"] for item in competencies}
    if not isinstance(states, dict) or set(states) != expected:
        raise ContractValidationError("Stored competency states must match the contract competencies.")
    normalized: dict[str, dict[str, str]] = {}
    for definition in competencies:
        identifier = definition["id"]
        value = states[identifier]
        if not isinstance(value, dict) or set(value) != {"status", "note"}:
            raise ContractValidationError(f"Stored state for {identifier} is invalid.")
        status = value["status"]
        if not isinstance(status, str) or status not in COMPETENCY_STATES:
            raise ContractValidationError(f"Stored state for {identifier} has an unsupported status.")
        note = _bounded_string(value["note"], f"states.{identifier}.note", 2000, nonblank=False)
        normalized[identifier] = {"status": status, "note": note}
    return normalized


def validate_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        raise ContractValidationError("The stored contract metadata has an invalid shape.")
    if isinstance(metadata["schema_version"], bool) or metadata["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise ContractValidationError("The stored contract schema version is unsupported.")
    contract_id = _uuid(metadata["contract_id"], "contract_id")
    version_token = _uuid(metadata["version_token"], "version_token")
    lesson_id = _bounded_string(metadata["lesson_id"], "lesson_id", 120)
    lesson_title = _bounded_string(metadata["lesson_title"], "lesson_title", 200)
    status = metadata["status"]
    if not isinstance(status, str) or status not in {"active", "completed"}:
        raise ContractValidationError("The stored contract status is invalid.")
    competencies = validate_competencies(metadata["competencies"])
    states = validate_state_map(metadata["states"], competencies)
    assessment = metadata["assessment"]
    if assessment is not None:
        assessment = validate_assessment(assessment, competencies)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": contract_id,
        "version_token": version_token,
        "lesson_id": lesson_id,
        "lesson_title": lesson_title,
        "status": status,
        "competencies": competencies,
        "states": states,
        "assessment": assessment,
    }


def validate_assessment(assessment: Any, competencies: list[dict[str, Any]]) -> dict[str, Any]:
    keys = {"competencies", "unresolved_critical_gaps", "completion_rule_met", "carry_forward_weaknesses"}
    if not isinstance(assessment, dict) or set(assessment) != keys:
        raise ContractValidationError("The stored assessment has an invalid shape.")
    definitions = {item["id"]: item for item in competencies}
    items = assessment["competencies"]
    if not isinstance(items, list) or len(items) != len(definitions):
        raise ContractValidationError("Assess every required competency exactly once.")
    normalized_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        required = {"competency_id", "criterion_met", "evidence_level", "evidence"}
        if not isinstance(item, dict) or set(item) != required:
            raise ContractValidationError(f"assessment.competencies[{index}] has an invalid shape.")
        identifier = item["competency_id"]
        if not isinstance(identifier, str) or identifier not in definitions or identifier in seen:
            raise ContractValidationError(f"Assessment has a duplicate or unknown competency ID: {identifier}.")
        seen.add(identifier)
        criterion_met = item["criterion_met"]
        if not isinstance(criterion_met, bool):
            raise ContractValidationError(f"assessment.competencies[{index}].criterion_met must be a boolean.")
        level = item["evidence_level"]
        if not isinstance(level, str) or level not in EVIDENCE_LEVELS:
            raise ContractValidationError(f"assessment.competencies[{index}].evidence_level is unsupported.")
        evidence = _bounded_string(item["evidence"], f"assessment.competencies[{index}].evidence", 2000)
        normalized_items.append({
            "competency_id": identifier,
            "criterion_met": criterion_met,
            "evidence_level": level,
            "evidence": evidence,
        })
    if seen != set(definitions):
        raise ContractValidationError("Assess every required competency exactly once.")

    gaps = assessment["unresolved_critical_gaps"]
    if not isinstance(gaps, list) or len(gaps) > MAX_COMPETENCIES or any(
        not isinstance(value, str) or value not in definitions for value in gaps
    ):
        raise ContractValidationError("unresolved_critical_gaps must contain competency IDs from this contract.")
    if len(gaps) != len(set(gaps)):
        raise ContractValidationError("unresolved_critical_gaps must not contain duplicates.")
    completion_rule_met = assessment["completion_rule_met"]
    if not isinstance(completion_rule_met, bool):
        raise ContractValidationError("completion_rule_met must be a boolean.")
    weaknesses = assessment["carry_forward_weaknesses"]
    if not isinstance(weaknesses, list) or len(weaknesses) > MAX_COMPETENCIES:
        raise ContractValidationError("carry_forward_weaknesses must be a list of at most 30 entries.")
    normalized_weaknesses = [
        _bounded_string(value, "carry_forward_weaknesses entry", 2000)
        for value in weaknesses
    ]
    return {
        "competencies": normalized_items,
        "unresolved_critical_gaps": list(gaps),
        "completion_rule_met": completion_rule_met,
        "carry_forward_weaknesses": normalized_weaknesses,
    }


def readiness(contract: dict[str, Any]) -> tuple[bool, list[str]]:
    normalized = validate_metadata(contract)
    assessment = normalized["assessment"]
    if assessment is None:
        return False, ["No assessment has been recorded for the current contract."]
    definitions = {item["id"]: item for item in normalized["competencies"]}
    ranks = {value: index for index, value in enumerate(EVIDENCE_LEVELS)}
    reasons: list[str] = []
    for item in assessment["competencies"]:
        identifier = item["competency_id"]
        definition = definitions[identifier]
        if not item["criterion_met"]:
            reasons.append(f"{identifier}: the required criterion was not met.")
        if ranks[item["evidence_level"]] < ranks[definition["required_evidence"]]:
            reasons.append(
                f"{identifier}: evidence must reach {definition['required_evidence']}."
            )
    for identifier in assessment["unresolved_critical_gaps"]:
        reasons.append(f"{identifier}: an unresolved critical gap remains.")
    if not assessment["completion_rule_met"]:
        reasons.append("The contract completion rule was not met.")
    return not reasons, reasons


def serialize_contract_document(metadata: dict[str, Any], body: str) -> str:
    normalized = validate_metadata(metadata)
    body = _validate_body(body)
    if CONTRACT_START in body or CONTRACT_END in body:
        raise ContractValidationError("contract_markdown contains a reserved contract marker.")
    payload = json.dumps(normalized, ensure_ascii=False, indent=2)
    text = (
        "# Lesson Contract\n"
        + CONTRACT_START
        + "\n```json\n"
        + payload
        + "\n```\n"
        + CONTRACT_END
        + "\n\n"
        + body
    )
    if len(text.encode("utf-8")) > CONTRACT_MAX_BYTES:
        raise ContractValidationError("The serialized lesson contract exceeds 128 KiB.")
    return text


def parse_contract_document(text: str) -> tuple[dict[str, Any], str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > CONTRACT_MAX_BYTES:
        raise ContractValidationError("The stored lesson contract exceeds 128 KiB or is not text.")
    prefix = "# Lesson Contract\n" + CONTRACT_START + "\n```json\n"
    separator = "\n```\n" + CONTRACT_END + "\n\n"
    if not text.startswith(prefix) or text.count(CONTRACT_START) != 1 or text.count(CONTRACT_END) != 1:
        raise ContractValidationError("The lesson contract envelope is missing or duplicated.")
    split_at = text.find(separator, len(prefix))
    if split_at < 0:
        raise ContractValidationError("The lesson contract JSON fence is malformed.")
    json_text = text[len(prefix) : split_at]
    body = text[split_at + len(separator) :]
    try:
        raw_metadata = json.loads(json_text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ContractValidationError) as exc:
        raise ContractValidationError("The lesson contract JSON is malformed.") from exc
    metadata = validate_metadata(raw_metadata)
    _validate_body(body)
    if CONTRACT_START in body or CONTRACT_END in body:
        raise ContractValidationError("The lesson contract body contains a reserved marker.")
    return metadata, body


def create_metadata(
    lesson_id: str,
    lesson_title: str,
    competencies: Any,
) -> dict[str, Any]:
    lesson_id = _bounded_string(lesson_id, "lesson_id", 120)
    lesson_title = _bounded_string(lesson_title, "lesson_title", 200)
    normalized_competencies = validate_competencies(competencies)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": str(uuid.uuid4()),
        "version_token": str(uuid.uuid4()),
        "lesson_id": lesson_id,
        "lesson_title": lesson_title,
        "status": "active",
        "competencies": normalized_competencies,
        "states": initial_states(normalized_competencies),
        "assessment": None,
    }


def apply_state_updates(contract: dict[str, Any], updates: Any) -> dict[str, Any]:
    normalized = validate_metadata(contract)
    if normalized["status"] != "active":
        raise ContractValidationError("A completed contract cannot be updated.")
    if not isinstance(updates, list) or not updates or len(updates) > MAX_COMPETENCIES:
        raise ContractValidationError("competency_updates must be a nonempty list.")
    known = {item["id"] for item in normalized["competencies"]}
    seen: set[str] = set()
    for index, item in enumerate(updates):
        if not isinstance(item, dict) or set(item) != {"competency_id", "status", "note"}:
            raise ContractValidationError(f"competency_updates[{index}] has an invalid shape.")
        identifier = item["competency_id"]
        if not isinstance(identifier, str) or identifier not in known or identifier in seen:
            raise ContractValidationError(f"competency_updates has a duplicate or unknown ID: {identifier}.")
        seen.add(identifier)
        status = item["status"]
        if not isinstance(status, str) or status not in COMPETENCY_STATES:
            raise ContractValidationError(f"competency_updates[{index}].status is unsupported.")
        note = _bounded_string(item["note"], f"competency_updates[{index}].note", 2000, nonblank=False)
        normalized["states"][identifier] = {"status": status, "note": note}
    normalized["assessment"] = None
    normalized["version_token"] = str(uuid.uuid4())
    return normalized


def apply_assessment(
    contract: dict[str, Any],
    items: Any,
    unresolved_critical_gaps: Any,
    completion_rule_met: Any,
    carry_forward_weaknesses: Any,
) -> dict[str, Any]:
    normalized = validate_metadata(contract)
    if normalized["status"] != "active":
        raise ContractValidationError("A completed contract cannot be assessed.")
    assessment = validate_assessment(
        {
            "competencies": items,
            "unresolved_critical_gaps": unresolved_critical_gaps,
            "completion_rule_met": completion_rule_met,
            "carry_forward_weaknesses": carry_forward_weaknesses,
        },
        normalized["competencies"],
    )
    normalized["assessment"] = assessment
    normalized["version_token"] = str(uuid.uuid4())
    return normalized


def render_mastery_record(contract: dict[str, Any]) -> str:
    normalized = validate_metadata(contract)
    ready, reasons = readiness(normalized)
    if not ready:
        return ""
    assessment = normalized["assessment"]
    assert assessment is not None
    assessment_by_id = {item["competency_id"]: item for item in assessment["competencies"]}
    lines = [
        f"## Lesson {normalized['lesson_id']} — {normalized['lesson_title']}",
        "",
        "### Mastery Criteria Used",
    ]
    for definition in normalized["competencies"]:
        flag = "critical" if definition["critical"] else "required"
        criterion = " ".join(definition["criterion"].split())
        lines.append(
            f"- {definition['id']} [{flag}]: {criterion} Required evidence: {definition['required_evidence']}."
        )
    lines.extend(["", "### Demonstrated"])
    for definition in normalized["competencies"]:
        item = assessment_by_id[definition["id"]]
        evidence = " ".join(item["evidence"].split())
        lines.append(f"- {definition['id']} — {item['evidence_level']}: {evidence}")
    gaps = assessment["unresolved_critical_gaps"]
    lines.extend(["", "### Critical Gaps", "None." if not gaps else "\n".join(f"- {value}" for value in gaps)])
    weaknesses = assessment["carry_forward_weaknesses"]
    lines.extend(["", "### Carry-Forward Weaknesses", "None." if not weaknesses else "\n".join(f"- {' '.join(value.split())}" for value in weaknesses)])
    lines.extend(["", "### Completion Decision", "Lesson complete."])
    block = "\n".join(lines)
    return (
        MASTERY_START.format(contract_id=normalized["contract_id"])
        + "\n"
        + block
        + "\n"
        + MASTERY_END.format(contract_id=normalized["contract_id"])
    )


def mark_completed(contract: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_metadata(contract)
    ready, reasons = readiness(normalized)
    if not ready:
        raise ContractValidationError("The current assessment is not ready: " + " ".join(reasons))
    if normalized["status"] != "active":
        raise ContractValidationError("The lesson contract is already completed.")
    normalized["status"] = "completed"
    normalized["version_token"] = str(uuid.uuid4())
    return normalized

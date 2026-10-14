"""Observable response-shape checks shared by production and migration tests."""

from __future__ import annotations

import json
import re
from typing import Any


SCHEDULE_PATTERN = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:minutes?|mins?|hours?|days?|weeks?|words?|flashcards?|problems?|lessons?|terms?|definitions?|pages?|sentences?|times?)\b"
    r"|\b(?:daily|every day|each day|this week|next week|after each lesson|at the start|beginning tomorrow|starting tomorrow|before the next lesson|before next lesson|next lesson|next homework|on time|before the deadline)\b",
    re.IGNORECASE,
)
MATERIAL_PATTERN = re.compile(
    r"\b(?:class|math|mathematics|science|english|subject-specific|current|most recent)\s+(?:class\s+)?notes\b"
    r"|\b(?:flashcards?|worksheets?|workbooks?|practice sets?)\b"
    r"|\b(?:make|create|write|build)\s+(?:a|an|some)\s+(?:list|definitions?|flashcards?|practice set)\b"
    r"|\b(?:vocabulary words|fraction terms|key terms|definitions)\b",
    re.IGNORECASE,
)
BULLET_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
FOLLOW_UP_PATTERN = re.compile(
    r"\b(?:would you like me to|want me to|feel free to ask|let me know if|ask me if you need|i can help you with)\b",
    re.IGNORECASE,
)
LIST_REQUEST_PATTERN = re.compile(r"\b(?:bullet(?:s)?|list|numbered steps|checklist)\b", re.IGNORECASE)
LATEST_ASSESSMENT_PATTERN = re.compile(r"\b(?:last|latest|most recent)\s+(?:assessment|exam|test)\b", re.IGNORECASE)
HISTORY_REQUEST_PATTERN = re.compile(r"\b(?:history|all|every|assessments|exams|tests|compare)\b", re.IGNORECASE)
ONE_IMPROVEMENT_PATTERN = re.compile(
    r"\bone\s+(?:(?:direct|small|practical|clear|specific)\s+)*(?:area|improvement|suggestion|thing|next step)\b",
    re.IGNORECASE,
)


def _assessment_entries(value: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("assessment"), str) and (
            "assessed_on" in value or "date" in value
        ):
            entries.append(value)
        for nested in value.values():
            entries.extend(_assessment_entries(nested))
    elif isinstance(value, list):
        for nested in value:
            entries.extend(_assessment_entries(nested))
    return entries


def _improvement_phrases(records: list[dict[str, Any]]) -> set[str]:
    phrases: set[str] = set()
    for record in records:
        for key in ("areas_for_improvement", "improvement_areas", "could_work_on"):
            value = record.get(key)
            values = value if isinstance(value, list) else [value]
            phrases.update(item.strip().casefold() for item in values if isinstance(item, str) and item.strip())
    return phrases


def response_contract_issues(
    *, question: str, records: list[dict[str, Any]], answer: str, max_paragraphs: int = 2
) -> list[str]:
    """Return deterministic format and unsupported-advice violations."""
    paragraphs = [part for part in re.split(r"\n\s*\n", answer.strip()) if part.strip()]
    issues: list[str] = []
    if len(paragraphs) > max_paragraphs:
        issues.append(f"Use no more than {max_paragraphs} paragraphs (current: {len(paragraphs)}).")
    if not LIST_REQUEST_PATTERN.search(question) and BULLET_PATTERN.search(answer):
        issues.append("Use prose instead of bullets or numbered lists unless the student requests a list.")

    source = json.dumps({"question": question, "records": records}, ensure_ascii=False, default=str).casefold()
    unsupported_details = [
        match.group(0).strip()
        for match in SCHEDULE_PATTERN.finditer(answer)
        if match.group(0).casefold() not in source
    ]
    if unsupported_details:
        issues.append("Remove unrequested schedules or quantities: " + ", ".join(unsupported_details))
    unsupported_materials = [
        match.group(0).strip()
        for match in MATERIAL_PATTERN.finditer(answer)
        if match.group(0).casefold() not in source
    ]
    if unsupported_materials:
        issues.append("Remove unrecorded study examples or materials: " + ", ".join(unsupported_materials))
    if FOLLOW_UP_PATTERN.search(answer):
        issues.append("Remove the unsolicited follow-up offer or question.")

    if LATEST_ASSESSMENT_PATTERN.search(question) and not HISTORY_REQUEST_PATTERN.search(question):
        assessments = _assessment_entries(records)
        assessments.sort(key=lambda item: str(item.get("assessed_on", item.get("date", ""))), reverse=True)
        if len(assessments) > 1:
            older_mentions: list[str] = []
            for entry in assessments[1:]:
                title = entry.get("assessment")
                date = entry.get("assessed_on", entry.get("date"))
                for value in (title, date):
                    if isinstance(value, str) and value and value.casefold() in answer.casefold():
                        older_mentions.append(value)
            if older_mentions:
                issues.append("For a singular latest-assessment question, omit older assessments: " + ", ".join(dict.fromkeys(older_mentions)))

    if ONE_IMPROVEMENT_PATTERN.search(question):
        present = sorted(phrase for phrase in _improvement_phrases(records) if phrase in answer.casefold())
        if len(present) > 1:
            issues.append("The student requested one improvement; mention only one recorded improvement area.")
    return issues

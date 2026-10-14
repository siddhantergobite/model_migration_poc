"""Compatibility checks that gate model and parent-prompt releases."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from model_migration.azure_responses import AzureResponsesError, AzureResponsesClient
from model_migration.generation import generate_tutor_answer
from model_migration.response_contract import response_contract_issues


JUDGE_INSTRUCTIONS = """You are the behavior-compatibility evaluator for a client application.

Compare the candidate answer with the baseline answer using the supplied tutor persona, shared behavior contract, question, and synthetic MongoDB fixture. Do not reward wording similarity. Score each answer for factual accuracy, persona fidelity, response-contract compliance, and clarity/helpfulness from 1 (unacceptable) to 5 (excellent). A score of 4 means the behavior is solid with only a minor wording or emphasis difference. Give a score below 4 only for a clear, material failure such as wrong/missing student facts, absence or contradiction of the tutor's defining voice, an unsupported second improvement/action, an invented study schedule, or materially confusing output. A concise restatement or a few related details that make one recorded action understandable are still one action; do not count them as extra steps unless they are independent tasks. Penalize invented student facts, incorrect dates or marks, older assessments in a latest-only answer, more improvement areas than requested, unsupported grading claims, unrequested schedules or quantities, more than two paragraphs, missing requested next steps, or confusing formatting. A brief wording change is not a regression if the behavior remains equivalent.

Return only a JSON object with this shape:
{"baseline":{"factual_accuracy":1,"persona_fidelity":1,"contract_compliance":1,"clarity":1},"candidate":{"factual_accuracy":1,"persona_fidelity":1,"contract_compliance":1,"clarity":1},"issues":["short concrete issue"],"summary":"one sentence"}

Use integer scores from 1 to 5. Empty issues means no material behavior regression."""

DIMENSIONS = ("factual_accuracy", "persona_fidelity", "contract_compliance", "clarity")
SCORE_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*(?:/|out\s+of)\s*(\d{1,3})(?!\d)", re.IGNORECASE)
def _instructions(release: dict[str, Any], tutor_id: str) -> str:
    tutor = release["personas"][tutor_id]
    return (
        f"TUTOR PERSONA — {tutor['name']}\n"
        f"{tutor['system_prompt'].strip()}\n\n"
        f"{release['behavior_contract'].strip()}"
    )


def _case_input(case: dict[str, Any]) -> str:
    return (
        "Selected MongoDB records for this request (JSON data):\n"
        f"{json.dumps(case['student_records'], ensure_ascii=False, default=str)}\n\n"
        f"Student question:\n{case['question']}"
    )


def _deterministic_check(answer: str, case: dict[str, Any]) -> dict[str, Any]:
    normalized = " ".join(answer.casefold().split())
    missing_facts = [
        fact for fact in case.get("required_facts", [])
        if " ".join(fact.casefold().split()) not in normalized
    ]
    expected_pairs = {
        (int(pair[0]), int(pair[1])) for pair in case.get("expected_score_pairs", [])
    }
    actual_pairs = {
        (int(match.group(1)), int(match.group(2))) for match in SCORE_PATTERN.finditer(answer)
    }
    unexpected_pairs = sorted(actual_pairs - expected_pairs) if expected_pairs else []
    missing_pairs = sorted(expected_pairs - actual_pairs)
    word_count = len(re.findall(r"\b[\w'-]+\b", answer))
    contract_issues = response_contract_issues(
        question=case.get("question", ""),
        records=case.get("student_records", []),
        answer=answer,
        max_paragraphs=int(case.get("max_paragraphs", 2)),
    )
    paragraphs = [part for part in re.split(r"\n\s*\n", answer.strip()) if part.strip()]
    has_bullets = any(re.match(r"\s*(?:[-*•]|\d+[.)])\s+", line) for line in answer.splitlines())
    has_invented_schedule = any("schedules or quantities" in issue for issue in contract_issues)
    issues: list[str] = []
    if missing_facts:
        issues.append("Missing required facts: " + ", ".join(missing_facts))
    if missing_pairs:
        issues.append("Missing recorded score/denominator pairs: " + ", ".join(f"{score}/{out_of}" for score, out_of in missing_pairs))
    if unexpected_pairs:
        issues.append("Contains score/denominator pairs not in the fixture: " + ", ".join(f"{score}/{out_of}" for score, out_of in unexpected_pairs))
    min_words = int(case.get("min_words", 40))
    if word_count < min_words:
        issues.append(f"Answer is too short for this case ({word_count} words; minimum {min_words}).")
    if word_count > 240:
        issues.append(f"Answer is too long for this case ({word_count} words).")
    issues.extend(issue for issue in contract_issues if issue not in issues)
    return {
        "passed": not issues,
        "word_count": word_count,
        "paragraph_count": len(paragraphs),
        "has_bullets": has_bullets,
        "has_invented_schedule": has_invented_schedule,
        "missing_facts": missing_facts,
        "missing_score_pairs": [list(pair) for pair in missing_pairs],
        "unexpected_score_pairs": [list(pair) for pair in unexpected_pairs],
        "issues": issues,
    }


def _parse_judgment(raw: str) -> dict[str, Any]:
    try:
        judgment = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AzureResponsesError("The Azure compatibility judge did not return valid JSON.") from error
    for side in ("baseline", "candidate"):
        scores = judgment.get(side)
        if not isinstance(scores, dict):
            raise AzureResponsesError("The Azure compatibility judge returned an incomplete scorecard.")
        for dimension in DIMENSIONS:
            score = scores.get(dimension)
            if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
                raise AzureResponsesError("The Azure compatibility judge returned an invalid score.")
    issues = judgment.get("issues", [])
    if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues):
        raise AzureResponsesError("The Azure compatibility judge returned invalid issues.")
    if not isinstance(judgment.get("summary"), str):
        raise AzureResponsesError("The Azure compatibility judge returned no summary.")
    return judgment


def _judge_case(
    *,
    provider: AzureResponsesClient,
    judge_model: str,
    active_release: dict[str, Any],
    candidate: dict[str, Any],
    case: dict[str, Any],
    baseline_answer: str,
    candidate_answer: str,
) -> dict[str, Any]:
    tutor_id = case["tutor_id"]
    judge_input = {
        "persona": candidate["personas"][tutor_id]["system_prompt"],
        "shared_contract": candidate["behavior_contract"],
        "persona_expectation": case.get("persona_expectation", ""),
        "question": case["question"],
        "synthetic_mongodb_records": case["student_records"],
        "baseline_answer": baseline_answer,
        "candidate_answer": candidate_answer,
    }
    raw = provider.complete(
        model=judge_model,
        instructions=JUDGE_INSTRUCTIONS,
        input_text=json.dumps(judge_input, ensure_ascii=False, default=str),
        max_output_tokens=900,
        json_mode=True,
    )
    return _parse_judgment(raw)


def run_compatibility_check(
    *,
    active_release: dict[str, Any],
    candidate: dict[str, Any],
    provider: AzureResponsesClient,
    cases_path: Path,
) -> dict[str, Any]:
    cases_data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = cases_data.get("cases", [])
    if not cases:
        return {
            "evaluation_id": str(uuid.uuid4()),
            "status": "blocked",
            "summary": "No compatibility cases are configured.",
            "active_model": active_release["model"],
            "candidate_model": candidate["model"],
            "cases": [],
        }

    registry_path = cases_path.parent / "applications.json"
    if registry_path.is_file():
        registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
        for application in registry_data.get("applications", []):
            persona_id = application.get("persona_id") or application.get("application_id")
            if not persona_id:
                continue
            if not any(
                case.get("tutor_id") == persona_id or case.get("application_id") == application.get("application_id")
                for case in cases
            ):
                return {
                    "evaluation_id": str(uuid.uuid4()),
                    "status": "blocked",
                    "summary": f"No regression cases are configured for client application '{application.get('application_id', persona_id)}'.",
                    "active_model": active_release["model"],
                    "candidate_model": candidate["model"],
                    "cases": [],
                }

    results: list[dict[str, Any]] = []
    for case in cases:
        tutor_id = case.get("tutor_id")
        if tutor_id not in active_release["personas"] or tutor_id not in candidate["personas"]:
            return {
                "evaluation_id": str(uuid.uuid4()),
                "status": "blocked",
                "summary": f"Benchmark '{case.get('id', 'unknown')}' references an unregistered tutor.",
                "active_model": active_release["model"],
                "candidate_model": candidate["model"],
                "cases": results,
            }
        try:
            baseline_answer = generate_tutor_answer(
                provider=provider,
                model=active_release["model"],
                instructions=_instructions(active_release, tutor_id),
                question=case["question"],
                records=case["student_records"],
                max_output_tokens=2200,
            )
            candidate_answer = generate_tutor_answer(
                provider=provider,
                model=candidate["model"],
                instructions=_instructions(candidate, tutor_id),
                question=case["question"],
                records=case["student_records"],
                max_output_tokens=2200,
            )
            baseline_check = _deterministic_check(baseline_answer, case)
            candidate_check = _deterministic_check(candidate_answer, case)
            judgment = _judge_case(
                provider=provider,
                judge_model=active_release["model"],
                active_release=active_release,
                candidate=candidate,
                case=case,
                baseline_answer=baseline_answer,
                candidate_answer=candidate_answer,
            )
        except AzureResponsesError as error:
            results.append({
                "case_id": case.get("id", "unknown"),
                "tutor_id": tutor_id,
                "status": "blocked",
                "error": str(error),
            })
            return {
                "evaluation_id": str(uuid.uuid4()),
                "status": "blocked",
                "summary": f"Compatibility check stopped at '{case.get('id', 'unknown')}': {error}",
                "active_model": active_release["model"],
                "candidate_model": candidate["model"],
                "cases": results,
            }

        baseline_scores = judgment["baseline"]
        candidate_scores = judgment["candidate"]
        score_drops = {
            dimension: baseline_scores[dimension] - candidate_scores[dimension]
            for dimension in DIMENSIONS
            if baseline_scores[dimension] - candidate_scores[dimension] > 1
        }
        minimum_scores = {
            "factual_accuracy": 5,
            "persona_fidelity": 4,
            "contract_compliance": 4,
            "clarity": 4,
        }
        low_scores = {
            dimension: score for dimension, score in candidate_scores.items()
            if score < minimum_scores[dimension]
        }
        case_passed = baseline_check["passed"] and candidate_check["passed"] and not score_drops and not low_scores
        results.append({
            "case_id": case["id"],
            "tutor_id": tutor_id,
            "application_id": case.get("application_id", tutor_id),
            "status": "passed" if case_passed else "failed",
            "baseline_check": baseline_check,
            "candidate_check": candidate_check,
            "baseline_scores": baseline_scores,
            "candidate_scores": candidate_scores,
            "score_drops": score_drops,
            "low_scores": low_scores,
            "judge_issues": judgment.get("issues", []),
            "judge_summary": judgment["summary"],
            "baseline_answer": baseline_answer,
            "candidate_answer": candidate_answer,
        })

    passed_count = sum(result.get("status") == "passed" for result in results)
    all_passed = passed_count == len(cases)
    return {
        "evaluation_id": str(uuid.uuid4()),
        "status": "passed" if all_passed else "failed",
        "summary": (
            f"All {len(cases)} compatibility cases passed."
            if all_passed
            else f"{passed_count} of {len(cases)} compatibility cases passed. Promotion is blocked."
        ),
        "active_model": active_release["model"],
        "candidate_model": candidate["model"],
        "judge_model": active_release["model"],
        "cases": results,
    }


"""Generate tutor answers and repair contract violations with the same model."""

from __future__ import annotations

import json
from typing import Any

from model_migration.azure_responses import AzureResponsesClient, AzureResponsesError
from model_migration.response_contract import response_contract_issues


def generate_tutor_answer(
    *,
    provider: AzureResponsesClient,
    model: str,
    instructions: str,
    question: str,
    records: list[dict[str, Any]],
    max_output_tokens: int = 3000,
    max_paragraphs: int = 3,
) -> str:
    """Ask once, then permit up to two same-model repair attempts if rules fail."""
    result = generate_tutor_answer_diagnostic(
        provider=provider,
        model=model,
        instructions=instructions,
        question=question,
        records=records,
        max_output_tokens=max_output_tokens,
        max_paragraphs=max_paragraphs,
    )
    if result["issues"]:
        raise AzureResponsesError(
            "The tutor response did not meet the saved behavior contract after two repair attempts. "
            "No answer was returned. Violations: " + "; ".join(result["issues"])
        )
    return result["answer"]


def generate_tutor_answer_diagnostic(
    *,
    provider: AzureResponsesClient,
    model: str,
    instructions: str,
    question: str,
    records: list[dict[str, Any]],
    max_output_tokens: int = 3000,
    max_paragraphs: int = 3,
) -> dict[str, Any]:
    """Run the live response and repair path, returning the final draft and remaining issues.

    The parent compare screen uses this to show a failed candidate draft for review. The live
    tutor route continues to use generate_tutor_answer, which withholds drafts that still fail.
    """
    context = (
        "Selected MongoDB records for this question (JSON data):\n"
        f"{json.dumps(records, ensure_ascii=False, default=str)}\n\n"
        f"Student question:\n{question}"
    )
    answer = provider.complete(
        model=model,
        instructions=instructions,
        input_text=context,
        max_output_tokens=max_output_tokens,
    ).strip()
    issues = response_contract_issues(
        question=question, records=records, answer=answer, max_paragraphs=max_paragraphs
    )
    first_answer = answer
    first_issues = list(issues)
    repair_history: list[dict[str, Any]] = []
    if not issues:
        return {
            "answer": answer,
            "issues": [],
            "repair_attempts": 0,
            "first_answer": first_answer,
            "first_issues": first_issues,
            "first_passed": True,
            "repair_history": repair_history,
        }

    for attempt in range(2):
        repair_input = (
            f"{context}\n\n"
            "Revise your previous draft to satisfy the saved response contract. Keep the same tutor persona "
            "and every correct MongoDB fact. Do not add facts. If the student asks to list recorded improvement "
            "areas, include every recorded area in that list. If advice is requested, choose one recorded area, "
            "preserve its key wording, and give exactly one concise recommendation. A request for one suggestion "
            "limits the recommendation count; it does not limit a separately requested list of recorded areas. "
            "Do not expand it into additional steps, add a schedule, duration, quantity, study material, a "
            "second recommendation, or an offer for further help. Preserve empathy or humor required by the "
            f"persona. Return only the corrected answer in no more than {max_paragraphs} prose paragraphs.\n\n"
            f"Issues to correct:\n- " + "\n- ".join(issues) +
            f"\n\nPrevious draft:\n{answer}"
        )
        answer = provider.complete(
            model=model,
            instructions=instructions,
            input_text=repair_input,
            max_output_tokens=max_output_tokens,
        ).strip()
        issues = response_contract_issues(
            question=question, records=records, answer=answer, max_paragraphs=max_paragraphs
        )
        repair_history.append({
            "attempt": attempt + 1,
            "answer": answer,
            "issues": list(issues),
            "passed": not issues,
        })
        if not issues:
            return {
                "answer": answer,
                "issues": [],
                "repair_attempts": attempt + 1,
                "first_answer": first_answer,
                "first_issues": first_issues,
                "first_passed": not first_issues,
                "repair_history": repair_history,
            }

    return {
        "answer": answer,
        "issues": issues,
        "repair_attempts": 2,
        "first_answer": first_answer,
        "first_issues": first_issues,
        "first_passed": not first_issues,
        "repair_history": repair_history,
    }

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
) -> str:
    """Ask once, then permit one same-model repair pass if deterministic rules fail."""
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
    issues = response_contract_issues(question=question, records=records, answer=answer)
    if not issues:
        return answer

    for attempt in range(2):
        repair_input = (
            f"{context}\n\n"
            "Revise your previous draft to satisfy the saved response contract. Keep the same tutor persona "
            "and every correct MongoDB fact. Do not add facts. If one improvement was requested, mention only "
            "one exact recorded area, give one concise action, and add no schedule, duration, quantity, study "
            "material, or follow-up offer. Preserve empathy or humor required by the persona. Return only the "
            "corrected answer in no more than two prose paragraphs.\n\n"
            f"Issues to correct:\n- " + "\n- ".join(issues) +
            f"\n\nPrevious draft:\n{answer}"
        )
        answer = provider.complete(
            model=model,
            instructions=instructions,
            input_text=repair_input,
            max_output_tokens=max_output_tokens,
        ).strip()
        issues = response_contract_issues(question=question, records=records, answer=answer)
        if not issues:
            return answer

    raise AzureResponsesError(
        "The tutor response did not meet the saved behavior contract after two repair attempts. "
        "No answer was returned. Violations: " + "; ".join(issues)
    )

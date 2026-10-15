"""Compatibility checks that gate model and parent-prompt releases."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import statistics
import uuid
from pathlib import Path
from typing import Any

from model_migration.azure_responses import AzureResponsesError, AzureResponsesClient
from model_migration.generation import generate_tutor_answer_diagnostic
from model_migration.response_contract import response_contract_issues
from model_migration.release_prompts import release_instructions


JUDGE_INSTRUCTIONS = """You are a blind behavior-compatibility evaluator for a client application.

The input contains Answer A and Answer B for the same request. Their model identities and which one is the active reference are hidden. Evaluate each answer independently against the supplied records, unchanged client prompt, shared response contract, and app-specific behavior expectation. Do not reward wording similarity or infer model identity. Use integer scores from 1 (unacceptable) to 5 (excellent) for factual accuracy, persona fidelity, contract compliance, and clarity. A score of 4 means solid behavior with only a minor wording or emphasis difference. Treat any recorded improvement as allowed unless the case supplies an explicit priority. Do not flag a different recorded choice by itself when multiple areas are valid; flag invented or unrecorded recommendations. Do flag repeated or unsupported extra actions, multi-step expansion beyond the recorded action, unsolicited follow-up offers, invented facts, wrong dates or marks, unrequested schedules, materially softened or contradicted persona, or missing requested content. A concise explanation of the same recorded action is still one action. Attribute each material issue to Answer A or Answer B; do not report normal wording variation.

Return only a JSON object with one scorecard per answer, preserving input order:
{"answer_a":[{"factual_accuracy":5,"persona_fidelity":5,"contract_compliance":5,"clarity":5}],"answer_b":[{"factual_accuracy":5,"persona_fidelity":5,"contract_compliance":5,"clarity":5}],"issues_a":["short concrete issue"],"issues_b":["short concrete issue"],"summary":"one sentence"}

Each score list must have exactly as many entries as the answer list. Empty issue lists mean no material issue for that answer."""

DIMENSIONS = ("factual_accuracy", "persona_fidelity", "contract_compliance", "clarity")
SCORE_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*(?:/|out\s+of)\s*(\d{1,3})(?!\d)", re.IGNORECASE)
EVALUATION_PIPELINE_REVISION = "deterministic-core-blind-final-v2"


def compatibility_suite_fingerprint(
    cases_path: Path,
    applications_path: Path | None = None,
    holdout_path: Path | None = None,
) -> str:
    """Hash the core and reserved validation fixtures plus registry."""
    registry_path = applications_path or cases_path.parent / "applications.json"
    validation_path = holdout_path or cases_path.with_name("behavior_holdout_cases.json")
    digest = hashlib.sha256()
    digest.update(EVALUATION_PIPELINE_REVISION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(JUDGE_INSTRUCTIONS.encode("utf-8"))
    digest.update(b"\0")
    for path in (cases_path, registry_path, validation_path):
        resolved = path.resolve()
        digest.update(str(resolved.name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes() if resolved.is_file() else b"<not-configured>")
        digest.update(b"\0")
    return digest.hexdigest()
def _instructions(release: dict[str, Any], tutor_id: str) -> str:
    return release_instructions(release, tutor_id)


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
        max_paragraphs=int(case.get("max_paragraphs", 3)),
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


def _recorded_improvement_options(records: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("areas_for_improvement", "improvement_areas", "could_work_on"):
                candidate = value.get(key)
                candidates = candidate if isinstance(candidate, list) else [candidate]
                values.extend(item.strip() for item in candidates if isinstance(item, str) and item.strip())
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(records)
    return list(dict.fromkeys(values))


def _run_first_pass_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(bool(run.get("first_pass_check", {}).get("passed")) for run in runs)
    return {
        "passed_runs": passed,
        "total_runs": len(runs),
        "pass_rate": round(passed / len(runs), 3) if runs else 0.0,
        "repair_attempts": sum(int(run.get("repair_attempts", 0)) for run in runs),
    }


def _parse_blind_judgment(raw: str, repetitions: int) -> dict[str, Any]:
    try:
        judgment = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AzureResponsesError("The Azure compatibility judge did not return valid JSON.") from error
    if not isinstance(judgment, dict):
        raise AzureResponsesError("The Azure compatibility judge did not return a JSON object.")
    for side in ("answer_a", "answer_b"):
        score_runs = judgment.get(side)
        if not isinstance(score_runs, list) or len(score_runs) != repetitions:
            raise AzureResponsesError("The Azure compatibility judge returned the wrong number of run scorecards.")
        for scores in score_runs:
            if not isinstance(scores, dict):
                raise AzureResponsesError("The Azure compatibility judge returned an incomplete scorecard.")
            for dimension in DIMENSIONS:
                score = scores.get(dimension)
                if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
                    raise AzureResponsesError("The Azure compatibility judge returned an invalid score.")
    for side in ("issues_a", "issues_b"):
        issues = judgment.get(side, [])
        if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues):
            raise AzureResponsesError("The Azure compatibility judge returned invalid answer-specific issues.")
    if not isinstance(judgment.get("summary"), str):
        raise AzureResponsesError("The Azure compatibility judge returned no summary.")
    return judgment


def validate_judge_models(judge_models: list[str] | None, candidate_model: str) -> str | None:
    """Require one to three distinct judge deployments, excluding the candidate."""
    if not isinstance(judge_models, list) or not 1 <= len(judge_models) <= 3:
        return (
            "Configure MODEL_EVAL_JUDGE_MODELS with one to three distinct Azure deployment names. "
            "Candidate promotion is blocked until the judge panel is configured."
        )
    cleaned = [item.strip() for item in judge_models if isinstance(item, str) and item.strip()]
    if len(cleaned) != len(judge_models) or len({item.casefold() for item in cleaned}) != len(cleaned):
        return "The judge panel must contain one to three non-empty, distinct deployment names."
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) for item in cleaned):
        return "Judge deployment names may contain only letters, numbers, dots, underscores, and hyphens."
    if candidate_model.casefold() in {item.casefold() for item in cleaned}:
        return "The candidate deployment cannot judge its own responses. Remove it from MODEL_EVAL_JUDGE_MODELS."
    return None


def _judge_score_summary(judgment: dict[str, Any]) -> dict[str, Any]:
    baseline_scores = _mean_scores(judgment["baseline"])
    candidate_scores = _mean_scores(judgment["candidate"])
    baseline_noise = _score_spread(judgment["baseline"])
    score_drops = {
        dimension: {
            "baseline_mean": baseline_scores[dimension],
            "candidate_mean": candidate_scores[dimension],
            "baseline_stddev": baseline_noise[dimension],
        }
        for dimension in DIMENSIONS
        if candidate_scores[dimension] < baseline_scores[dimension] - min(1.0, max(0.5, baseline_noise[dimension]))
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
    issues = list(judgment.get("issues", []))
    issues.extend(
        f"Candidate {dimension.replace('_', ' ')} score {score}/5 is below the required {minimum_scores[dimension]}/5."
        for dimension, score in low_scores.items()
    )
    issues.extend(
        f"Candidate {dimension.replace('_', ' ')} score dropped from {values['baseline_mean']}/5 "
        f"to {values['candidate_mean']}/5 beyond baseline variation."
        for dimension, values in score_drops.items()
    )
    return {
        "baseline_scores": baseline_scores,
        "candidate_scores": candidate_scores,
        "baseline_score_noise": baseline_noise,
        "score_drops": score_drops,
        "low_scores": low_scores,
        "issues": list(dict.fromkeys(issues)),
    }


def _average_score_runs(judgments: list[dict[str, Any]], side: str, repetitions: int) -> list[dict[str, float]]:
    """Average judge scores per response run while retaining per-judge votes separately."""
    return [
        {
            dimension: round(statistics.mean(
                judgment[side][run_index][dimension] for judgment in judgments
            ), 2)
            for dimension in DIMENSIONS
        }
        for run_index in range(repetitions)
    ]


def _judge_case(
    *,
    provider: AzureResponsesClient,
    judge_models: list[str],
    active_release: dict[str, Any],
    case: dict[str, Any],
    baseline_answers: list[str],
    candidate_answers: list[str],
    evaluation_id: str,
    judge_warnings: list[str],
) -> dict[str, Any]:
    tutor_id = case["tutor_id"]
    shared_judge_input = {
        "persona": active_release["personas"][tutor_id]["system_prompt"],
        "shared_contract": active_release["behavior_contract"],
        "persona_expectation": case.get("persona_expectation", ""),
        "question": case["question"],
        "student_records": case["student_records"],
        "allowed_recorded_improvement_areas": _recorded_improvement_options(case["student_records"]),
        "recommendation_policy": case.get("recommendation_policy", "any_recorded_improvement"),
        "recommendation_priority": case.get("recommendation_priority"),
    }
    seed_material = f"{evaluation_id}\0{case.get('id', 'unknown')}"
    seed = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    baseline_first = int(seed[:8], 16) % 2 == 0
    order_specs = [
        {
            "name": "seeded_first_order",
            "answer_a_source": "baseline" if baseline_first else "candidate",
            "answer_b_source": "candidate" if baseline_first else "baseline",
        },
        {
            "name": "opposite_order",
            "answer_a_source": "candidate" if baseline_first else "baseline",
            "answer_b_source": "baseline" if baseline_first else "candidate",
        },
    ]
    judgments: list[dict[str, Any]] = []
    judge_summaries: list[dict[str, Any]] = []
    judge_order_records: list[dict[str, Any]] = []
    for judge_model in judge_models:
        order_summaries: list[dict[str, Any]] = []
        mapped_judgments: list[dict[str, Any]] = []
        for order in order_specs:
            source_answers = {
                "baseline": baseline_answers,
                "candidate": candidate_answers,
            }
            judge_input = {
                **shared_judge_input,
                "answer_a": source_answers[order["answer_a_source"]],
                "answer_b": source_answers[order["answer_b_source"]],
            }
            raw = provider.complete(
                model=judge_model,
                instructions=JUDGE_INSTRUCTIONS,
                input_text=json.dumps(judge_input, ensure_ascii=False, default=str),
                max_output_tokens=900,
                json_mode=True,
            )
            blind_judgment = _parse_blind_judgment(raw, len(baseline_answers))
            answer_a_source = order["answer_a_source"]
            answer_b_source = order["answer_b_source"]
            mapped = {
                "baseline": blind_judgment[f"answer_{'a' if answer_a_source == 'baseline' else 'b'}"],
                "candidate": blind_judgment[f"answer_{'a' if answer_a_source == 'candidate' else 'b'}"],
                "issues": blind_judgment[f"issues_{'a' if answer_a_source == 'candidate' else 'b'}"],
                "answer_issues": {
                    "baseline": blind_judgment[f"issues_{'a' if answer_a_source == 'baseline' else 'b'}"],
                    "candidate": blind_judgment[f"issues_{'a' if answer_a_source == 'candidate' else 'b'}"],
                },
                "summary": blind_judgment["summary"],
            }
            score_summary = _judge_score_summary(mapped)
            order_passed = not score_summary["issues"]
            mapped_judgments.append(mapped)
            order_summaries.append({
                "order": order["name"],
                "answer_a_source": answer_a_source,
                "answer_b_source": answer_b_source,
                "passed": order_passed,
                "baseline_scores": score_summary["baseline_scores"],
                "candidate_scores": score_summary["candidate_scores"],
                "score_drops": score_summary["score_drops"],
                "low_scores": score_summary["low_scores"],
                "issues": score_summary["issues"],
                "summary": blind_judgment["summary"],
            })

        order_pass_states = [bool(item["passed"]) for item in order_summaries]
        unstable = len(set(order_pass_states)) > 1
        combined = {
            "baseline": [
                {
                    dimension: round(statistics.mean(
                        judgment["baseline"][run_index][dimension]
                        for judgment in mapped_judgments
                    ), 2)
                    for dimension in DIMENSIONS
                }
                for run_index in range(len(baseline_answers))
            ],
            "candidate": [
                {
                    dimension: round(statistics.mean(
                        judgment["candidate"][run_index][dimension]
                        for judgment in mapped_judgments
                    ), 2)
                    for dimension in DIMENSIONS
                }
                for run_index in range(len(candidate_answers))
            ],
            "issues": list(dict.fromkeys(
                issue for judgment in mapped_judgments for issue in judgment["issues"]
            )),
            "summary": " / ".join(judgment["summary"] for judgment in mapped_judgments),
        }
        score_summary = _judge_score_summary(combined)
        order_issues = list(dict.fromkeys(
            issue for item in order_summaries for issue in item["issues"]
        ))
        if unstable:
            order_issues.append("Judge pass/fail changed when answer order was reversed; result is unstable.")
        judge_issues = list(dict.fromkeys([*score_summary["issues"], *order_issues]))
        judge_passed = all(order_pass_states) and not unstable and not judge_issues
        combined["issues"] = judge_issues
        judgments.append(combined)
        judge_order_records.append({"model": judge_model, "orders": order_summaries, "unstable": unstable})
        judge_summaries.append({
            "model": judge_model,
            "passed": judge_passed,
            "baseline_scores": score_summary["baseline_scores"],
            "candidate_scores": score_summary["candidate_scores"],
            "score_drops": score_summary["score_drops"],
            "low_scores": score_summary["low_scores"],
            "issues": judge_issues,
            "summary": (
                "Candidate passed both answer orders."
                if judge_passed
                else "Candidate failed at least one answer order or the orders produced an unstable decision."
            ),
            "order_checks": order_summaries,
            "unstable": unstable,
        })

    passed_judges = sum(bool(item["passed"]) for item in judge_summaries)
    all_judges_passed = passed_judges == len(judge_summaries)
    issues = [
        f"{judge['model']}: {issue}"
        for judge in judge_summaries
        for issue in judge["issues"]
    ]
    summary = (
        f"All {len(judge_summaries)} configured judges passed in both answer orders."
        if all_judges_passed
        else f"Only {passed_judges} of {len(judge_summaries)} configured judges passed both answer orders; every judge must pass."
    )
    return {
        "baseline": _average_score_runs(judgments, "baseline", len(baseline_answers)),
        "candidate": _average_score_runs(judgments, "candidate", len(candidate_answers)),
        "issues": issues,
        "summary": summary,
        "judges": judge_summaries,
        "judge_models": judge_models,
        "all_judges_passed": all_judges_passed,
        "judge_order_seed": seed,
        "judge_order_records": judge_order_records,
        "judge_warnings": judge_warnings,
    }


def _aggregate_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    issues = list(dict.fromkeys(
        f"Run {index}: {issue}"
        for index, check in enumerate(checks, start=1)
        for issue in check.get("issues", [])
    ))
    return {
        "passed": bool(checks) and all(check.get("passed") for check in checks),
        "run_count": len(checks),
        "issues": issues,
        "runs": checks,
    }


def _mean_scores(score_runs: list[dict[str, int]]) -> dict[str, float]:
    return {
        dimension: round(statistics.mean(run[dimension] for run in score_runs), 2)
        for dimension in DIMENSIONS
    }


def _score_spread(score_runs: list[dict[str, int]]) -> dict[str, float]:
    return {
        dimension: round(statistics.pstdev(run[dimension] for run in score_runs), 2)
        for dimension in DIMENSIONS
    }


def run_compatibility_check(
    *,
    active_release: dict[str, Any],
    candidate: dict[str, Any],
    provider: AzureResponsesClient,
    cases_path: Path,
    judge_models: list[str] | None = None,
    repetitions: int = 3,
    suite_stage: str = "core",
) -> dict[str, Any]:
    """Compare repeated responses in a tuning (core) or promotion (final) run."""
    evaluation_id = str(uuid.uuid4())
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or not 2 <= repetitions <= 5:
        raise ValueError("Evaluation repetitions must be an integer from 2 to 5.")
    if not isinstance(suite_stage, str) or suite_stage not in {"core", "final"}:
        raise ValueError("Evaluation suite stage must be 'core' or 'final'.")
    effective_judge_models = list(judge_models or []) if suite_stage == "final" else []
    judge_configuration_error = (
        validate_judge_models(effective_judge_models, candidate.get("model", ""))
        if suite_stage == "final"
        else None
    )
    if judge_configuration_error:
        return {
            "evaluation_id": evaluation_id,
            "status": "blocked",
            "summary": judge_configuration_error,
            "active_model": active_release["model"],
            "candidate_model": candidate["model"],
            "judge_models": effective_judge_models,
            "judge_panel_configured": False,
            "repetitions": repetitions,
            "suite_stage": suite_stage,
            "cases": [],
        }
    judge_warnings = [
        f"Judge deployment '{judge_model}' is also the active baseline model; its decision is not independent."
        for judge_model in effective_judge_models
        if judge_model.casefold() == active_release["model"].casefold()
    ]
    for warning in judge_warnings:
        logging.getLogger(__name__).warning(warning)
    cases_data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = [
        {**case, "case_group": "core"}
        for case in cases_data.get("cases", [])
    ]
    holdout_path = cases_path.with_name("behavior_holdout_cases.json")
    if suite_stage == "final" and holdout_path.is_file():
        holdout_data = json.loads(holdout_path.read_text(encoding="utf-8"))
        holdout_cases = holdout_data.get("cases", [])
        if not isinstance(holdout_cases, list):
            raise ValueError("Reserved validation cases must be a JSON array.")
        cases.extend({**case, "case_group": "holdout"} for case in holdout_cases)
    if suite_stage == "final" and not any(case.get("case_group") == "holdout" for case in cases):
        return {
            "evaluation_id": evaluation_id,
            "status": "blocked",
            "summary": "Final validation requires at least one reserved case. Add reserved cases before attempting promotion.",
            "active_model": active_release["model"],
            "candidate_model": candidate["model"],
            "repetitions": repetitions,
            "suite_stage": suite_stage,
            "judge_warnings": judge_warnings,
            "cases": [],
        }
    if not cases:
        return {
            "evaluation_id": evaluation_id,
            "status": "blocked",
            "summary": "No compatibility cases are configured.",
            "active_model": active_release["model"],
            "candidate_model": candidate["model"],
            "repetitions": repetitions,
            "suite_stage": suite_stage,
            "judge_warnings": judge_warnings,
            "cases": [],
        }

    registry_path = cases_path.parent / "applications.json"
    if registry_path.is_file():
        registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
        core_cases = [case for case in cases if case.get("case_group") == "core"]
        holdout_cases = [case for case in cases if case.get("case_group") == "holdout"]
        for application in registry_data.get("applications", []):
            persona_id = application.get("persona_id") or application.get("application_id")
            if not persona_id:
                continue
            if not any(
                case.get("tutor_id") == persona_id or case.get("application_id") == application.get("application_id")
                for case in core_cases
            ):
                return {
                    "evaluation_id": evaluation_id,
                    "status": "blocked",
                    "summary": f"No regression cases are configured for client application '{application.get('application_id', persona_id)}'.",
                    "active_model": active_release["model"],
                    "candidate_model": candidate["model"],
                    "repetitions": repetitions,
                    "suite_stage": suite_stage,
                    "judge_warnings": judge_warnings,
                    "cases": [],
                }
            if suite_stage == "final" and application.get("holdout_required") and not any(
                case.get("tutor_id") == persona_id or case.get("application_id") == application.get("application_id")
                for case in holdout_cases
            ):
                return {
                    "evaluation_id": evaluation_id,
                    "status": "blocked",
                    "summary": f"Reserved validation cases are missing for client application '{application.get('application_id', persona_id)}'.",
                    "active_model": active_release["model"],
                    "candidate_model": candidate["model"],
                    "repetitions": repetitions,
                    "suite_stage": suite_stage,
                    "judge_warnings": judge_warnings,
                    "cases": [],
                }

    results: list[dict[str, Any]] = []
    for case in cases:
        tutor_id = case.get("tutor_id")
        if tutor_id not in active_release["personas"] or tutor_id not in candidate["personas"]:
            return {
                "evaluation_id": evaluation_id,
                "status": "blocked",
                "summary": f"Benchmark '{case.get('id', 'unknown')}' references an unregistered tutor.",
                "active_model": active_release["model"],
                "candidate_model": candidate["model"],
                "repetitions": repetitions,
                "suite_stage": suite_stage,
                "judge_warnings": judge_warnings,
                "cases": results,
            }
        baseline_answers: list[str] = []
        candidate_answers: list[str] = []
        baseline_checks: list[dict[str, Any]] = []
        candidate_checks: list[dict[str, Any]] = []
        baseline_runs: list[dict[str, Any]] = []
        candidate_runs: list[dict[str, Any]] = []
        try:
            for run_index in range(1, repetitions + 1):
                baseline_result = generate_tutor_answer_diagnostic(
                    provider=provider,
                    model=active_release["model"],
                    instructions=_instructions(active_release, tutor_id),
                    question=case["question"],
                    records=case["student_records"],
                    max_output_tokens=2200,
                )
                candidate_result = generate_tutor_answer_diagnostic(
                    provider=provider,
                    model=candidate["model"],
                    instructions=_instructions(candidate, tutor_id),
                    question=case["question"],
                    records=case["student_records"],
                    max_output_tokens=2200,
                )
                baseline_answers.append(baseline_result["answer"])
                candidate_answers.append(candidate_result["answer"])
                baseline_first_check = _deterministic_check(baseline_result["first_answer"], case)
                candidate_first_check = _deterministic_check(candidate_result["first_answer"], case)
                baseline_first_check["issues"].extend(
                    "Response contract: " + issue for issue in baseline_result["first_issues"]
                    if "Response contract: " + issue not in baseline_first_check["issues"]
                )
                candidate_first_check["issues"].extend(
                    "Response contract: " + issue for issue in candidate_result["first_issues"]
                    if "Response contract: " + issue not in candidate_first_check["issues"]
                )
                baseline_first_check["passed"] = not baseline_first_check["issues"]
                candidate_first_check["passed"] = not candidate_first_check["issues"]
                baseline_check = _deterministic_check(baseline_result["answer"], case)
                candidate_check = _deterministic_check(candidate_result["answer"], case)
                baseline_check["issues"].extend(
                    "Response contract: " + issue for issue in baseline_result["issues"]
                    if "Response contract: " + issue not in baseline_check["issues"]
                )
                candidate_check["issues"].extend(
                    "Response contract: " + issue for issue in candidate_result["issues"]
                    if "Response contract: " + issue not in candidate_check["issues"]
                )
                baseline_check["passed"] = not baseline_check["issues"]
                candidate_check["passed"] = not candidate_check["issues"]
                baseline_checks.append(baseline_check)
                candidate_checks.append(candidate_check)
                baseline_runs.append({
                    "first_answer": baseline_result["first_answer"],
                    "first_issues": baseline_result["first_issues"],
                    "first_pass_check": baseline_first_check,
                    "repair_history": baseline_result["repair_history"],
                    "repair_attempts": baseline_result["repair_attempts"],
                    "final_answer": baseline_result["answer"],
                    "final_check": baseline_check,
                })
                candidate_runs.append({
                    "first_answer": candidate_result["first_answer"],
                    "first_issues": candidate_result["first_issues"],
                    "first_pass_check": candidate_first_check,
                    "repair_history": candidate_result["repair_history"],
                    "repair_attempts": candidate_result["repair_attempts"],
                    "final_answer": candidate_result["answer"],
                    "final_check": candidate_check,
                })

            if suite_stage == "final":
                judgment = _judge_case(
                    provider=provider,
                    judge_models=effective_judge_models,
                    active_release=active_release,
                    case=case,
                    baseline_answers=baseline_answers,
                    candidate_answers=candidate_answers,
                    evaluation_id=evaluation_id,
                    judge_warnings=judge_warnings,
                )
            else:
                judgment = {
                    "baseline": [],
                    "candidate": [],
                    "issues": [],
                    "summary": "Deterministic checks only; LLM judges run during final validation.",
                    "judges": [],
                    "judge_models": [],
                    "all_judges_passed": None,
                    "judge_order_seed": None,
                    "judge_order_records": [],
                    "judge_warnings": [],
                }
        except AzureResponsesError as error:
            results.append({
                "case_id": case.get("id", "unknown"),
                "tutor_id": tutor_id,
                "case_group": case.get("case_group", "core"),
                "status": "blocked",
                "error": str(error),
                "baseline_runs": len(baseline_answers),
                "candidate_runs": len(candidate_answers),
            })
            return {
                "evaluation_id": evaluation_id,
                "status": "blocked",
                "summary": f"Compatibility check stopped at '{case.get('id', 'unknown')}': {error}",
                "active_model": active_release["model"],
                "candidate_model": candidate["model"],
                "repetitions": repetitions,
                "suite_stage": suite_stage,
                "judge_warnings": judge_warnings,
                "cases": results,
            }

        baseline_score_runs = judgment["baseline"]
        candidate_score_runs = judgment["candidate"]
        baseline_check = _aggregate_checks(baseline_checks)
        candidate_check = _aggregate_checks(candidate_checks)
        if suite_stage == "final":
            baseline_scores = _mean_scores(baseline_score_runs)
            candidate_scores = _mean_scores(candidate_score_runs)
            baseline_noise = _score_spread(baseline_score_runs)
            score_drops = {
                dimension: {
                    "baseline_mean": baseline_scores[dimension],
                    "candidate_mean": candidate_scores[dimension],
                    "baseline_stddev": baseline_noise[dimension],
                }
                for dimension in DIMENSIONS
                if candidate_scores[dimension] < baseline_scores[dimension] - min(1.0, max(0.5, baseline_noise[dimension]))
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
        else:
            baseline_scores = {}
            candidate_scores = {}
            baseline_noise = {}
            score_drops = {}
            low_scores = {}
        judge_issues = judgment.get("issues", [])
        case_passed = baseline_check["passed"] and candidate_check["passed"]
        if suite_stage == "final":
            case_passed = (
                case_passed
                and not score_drops
                and not low_scores
                and not judge_issues
                and judgment.get("all_judges_passed", False)
            )
        results.append({
            "case_id": case["id"],
            "tutor_id": tutor_id,
            "application_id": case.get("application_id", tutor_id),
            "case_group": case.get("case_group", "core"),
            "status": "passed" if case_passed else "failed",
            "baseline_check": baseline_check,
            "candidate_check": candidate_check,
            "baseline_scores": baseline_scores,
            "candidate_scores": candidate_scores,
            "baseline_score_runs": baseline_score_runs,
            "candidate_score_runs": candidate_score_runs,
            "baseline_score_noise": baseline_noise,
            "score_drops": score_drops,
            "low_scores": low_scores,
            "judge_issues": judge_issues,
            "judge_summary": judgment["summary"],
            "judge_models": judgment["judge_models"],
            "judges": judgment["judges"],
            "all_judges_passed": judgment["all_judges_passed"],
            "judge_order_seed": judgment["judge_order_seed"],
            "judge_order_records": judgment["judge_order_records"],
            "judge_warnings": judgment["judge_warnings"],
            "baseline_answer": baseline_answers[0],
            "candidate_answer": candidate_answers[0],
            "baseline_answers": baseline_answers,
            "candidate_answers": candidate_answers,
            "baseline_runs": baseline_runs,
            "candidate_runs": candidate_runs,
            "first_pass_summary": {
                "active": _run_first_pass_summary(baseline_runs),
                "candidate": _run_first_pass_summary(candidate_runs),
            },
        })

    passed_count = sum(result.get("status") == "passed" for result in results)
    all_passed = passed_count == len(cases)
    return {
        "evaluation_id": evaluation_id,
        "evaluation_pipeline_revision": EVALUATION_PIPELINE_REVISION,
        "status": "passed" if all_passed else "failed",
        "summary": (
            (
                f"All {len(cases)} core behavior cases passed. Run final validation before promotion."
                if suite_stage == "core"
                else f"All {len(cases)} core and reserved validation cases passed."
            )
            if all_passed
            else (
                f"{passed_count} of {len(cases)} core cases passed."
                if suite_stage == "core"
                else f"{passed_count} of {len(cases)} core and reserved validation cases passed. Promotion is blocked."
            )
        ),
        "active_model": active_release["model"],
        "candidate_model": candidate["model"],
        "judge_models": effective_judge_models,
        "judge_panel_configured": bool(effective_judge_models),
        "judge_warnings": judge_warnings,
        "repetitions": repetitions,
        "suite_stage": suite_stage,
        "case_counts": {
            "core": sum(result.get("case_group") == "core" for result in results),
            "holdout": sum(result.get("case_group") == "holdout" for result in results),
        },
        "first_pass_summary": {
            "active": _run_first_pass_summary([run for result in results for run in result.get("baseline_runs", [])]),
            "candidate": _run_first_pass_summary([run for result in results for run in result.get("candidate_runs", [])]),
        },
        "cases": results,
    }


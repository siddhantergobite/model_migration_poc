"""Parent model control plane and Azure-backed response gateway for V2."""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from flask import Flask, current_app, jsonify, render_template, request
from pymongo.errors import PyMongoError

from model_migration.azure_responses import AzureResponsesClient, AzureResponsesError
from model_migration.application_registry import ApplicationRegistry, ApplicationRegistryError
from model_migration.control_store import ControlStore, ControlStoreError
from model_migration.evaluation import (
    _deterministic_check,
    compatibility_suite_fingerprint,
    run_compatibility_check,
    validate_judge_models,
)
from model_migration.generation import generate_tutor_answer, generate_tutor_answer_diagnostic
from model_migration.langfuse_tracing import (
    check_langfuse_connection,
    langfuse_status,
    observation_scope,
    safe_observation_update,
)
from model_migration.response_contract import response_contract_issues
from model_migration.release_prompts import release_instructions
from shared.config import load_env_file
from shared.database import get_collection
from shared.web_factory import student_context_for_question


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_env_file(PROJECT_ROOT / ".env")


def _configured_judge_models(value: str | None = None) -> list[str]:
    raw = os.getenv("MODEL_EVAL_JUDGE_MODELS", "") if value is None else value
    return [item.strip() for item in re.split(r"[,;\r\n]", raw) if item.strip()]


def _bearer_matches(expected: str) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    return bool(expected and scheme.casefold() == "bearer" and hmac.compare_digest(supplied.strip(), expected))


def _supplied_bearer() -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    return supplied.strip() if scheme.casefold() == "bearer" else ""


def _application_token_matches(application: dict[str, Any], token: str) -> bool:
    if not token:
        return False
    stored_hash = application.get("gateway_token_sha256", "")
    if stored_hash:
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(supplied_hash, stored_hash)
    token_env = application.get("gateway_token_env", "")
    expected = os.getenv(token_env, "").strip() if token_env else ""
    if expected:
        if hmac.compare_digest(token, expected):
            return True
        return bool(current_app.config.get("ALLOW_SHARED_GATEWAY_OVERRIDE")) and _bearer_matches(
            current_app.config.get("MODEL_GATEWAY_TOKEN", "")
        )
    # Backward compatibility for the three local sample tutors. Newly registered
    # applications have a stored credential hash and never use this fallback.
    return bool(application.get("legacy_shared_token_fallback", True)) and hmac.compare_digest(
        token, current_app.config.get("MODEL_GATEWAY_TOKEN", "")
    )


def _authenticated_application(application_id: str | None = None) -> dict[str, Any] | None:
    registry: ApplicationRegistry = current_app.config["APPLICATION_REGISTRY"]
    token = _supplied_bearer()
    if application_id:
        application = registry.get_application(application_id)
        return application if application and _application_token_matches(application, token) else None
    for application in registry.list_applications():
        if _application_token_matches(application, token):
            return application
    return None


def _response_instructions(release: dict[str, Any], tutor_id: str) -> str:
    return release_instructions(release, tutor_id)


def _suite_fingerprint(project_root: Path) -> str:
    config_dir = project_root / "config"
    return compatibility_suite_fingerprint(
        config_dir / "behavior_regression_cases.json",
        config_dir / "applications.json",
        config_dir / "behavior_holdout_cases.json",
    )


def _apply_suite_freshness(
    public_state: dict[str, Any],
    project_root: Path,
    judge_models: list[str] | None = None,
) -> dict[str, Any]:
    """Require saved approval to match the cases, registry, and judge panel."""
    evaluation = public_state.get("last_evaluation")
    if not evaluation:
        return public_state
    if judge_models is not None and evaluation.get("judge_models") != judge_models:
        evaluation["promotion_allowed"] = False
        if evaluation.get("status") == "passed":
            evaluation["status"] = "stale"
            evaluation["summary"] = "The configured judge panel changed after this evaluation. Run the checks again."
    try:
        current_fingerprint = _suite_fingerprint(project_root)
    except (OSError, ValueError, json.JSONDecodeError):
        current_fingerprint = ""
    if not current_fingerprint or evaluation.get("suite_fingerprint") != current_fingerprint:
        evaluation["promotion_allowed"] = False
        if evaluation.get("status") == "passed":
            evaluation["status"] = "stale"
            evaluation["summary"] = "The regression cases, application registry, or evaluation rules changed after this evaluation. Run the checks again."
    return public_state


def _response_input(question: str, records: list[dict[str, Any]]) -> str:
    return (
        "Selected MongoDB records for this question (JSON data):\n"
        f"{json.dumps(records, ensure_ascii=False, default=str)}\n\n"
        f"Student question:\n{question}"
    )


def _append_regression_cases(project_root: Path, new_cases: list[dict[str, Any]]) -> None:
    path = project_root / "config" / "behavior_regression_cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.setdefault("cases", [])
    existing_ids = {item.get("id") for item in cases}
    if any(item.get("id") in existing_ids for item in new_cases):
        raise ValueError("One or more regression case IDs are already in use.")
    data["cases"] = [*cases, *new_cases]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f"{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _public_application(application: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in application.items() if key not in {"gateway_token_sha256"}}


def _record_values(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for record in records:
        for key in keys:
            raw = record.get(key)
            items = raw if isinstance(raw, list) else [raw]
            values.extend(item.strip() for item in items if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(values))


def _identity_options(records: list[dict[str, Any]]) -> list[str]:
    """Accept a full name, first name, or student ID as a clear identity reference."""
    options: list[str] = []
    for record in records:
        name = record.get("name")
        if isinstance(name, str) and name.strip():
            options.append(name.strip())
            first_name = name.strip().split()[0]
            if first_name:
                options.append(first_name)
        student_id = record.get("student_id")
        if isinstance(student_id, str) and student_id.strip():
            options.append(student_id.strip())
    return list(dict.fromkeys(options))


def _find_comparison_students(question: str) -> list[dict[str, Any]]:
    """Resolve only an exact full name or student ID mentioned in the question.

    This development setup shares MongoDB with the three tutor apps. It reads the small
    identity projection first, then loads full data only for an exact match. External
    clients send authorized request context or use an approved data connector.
    """
    normalize = lambda value: " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))
    normalized_question = f" {normalize(question)} "
    collection = get_collection()
    matches: dict[str, dict[str, Any]] = {}
    for identity in collection.find({}, {"_id": 0, "student_id": 1, "name": 1}):
        student_id = str(identity.get("student_id") or "").strip()
        name = str(identity.get("name") or "").strip()
        identities = [value for value in (student_id, name) if value]
        for value in identities:
            normalized_identity = normalize(value)
            if normalized_identity and f" {normalized_identity} " in normalized_question:
                key = student_id or normalized_identity
                matches[key] = identity
                break
    if not matches:
        return []

    student_ids = [str(row["student_id"]) for row in matches.values() if row.get("student_id")]
    if student_ids:
        return list(collection.find({"student_id": {"$in": student_ids}}, {"_id": 0}))

    names = [str(row["name"]) for row in matches.values() if row.get("name")]
    return list(collection.find({"name": {"$in": names}}, {"_id": 0}))


def _comparison_requirements(question: str, records: list[dict[str, Any]]) -> tuple[list[str], list[list[int]], list[str], str]:
    """Build focused factual checks from the same student context sent to both models."""
    lowered = question.casefold()
    required_facts: list[str] = []
    expected_scores: list[list[int]] = []
    assessment_entries: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("assessment"), str) and any(key in value for key in ("assessed_on", "date", "score")):
                assessment_entries.append(value)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for record in records:
        collect(record)

    assessment_requested = bool(re.search(r"\b(assessment|assessments|exam|exams|test|tests|result|results|score|scores|history)\b", lowered))
    asks_history = bool(re.search(r"\b(history|all|every)\b", lowered))
    asks_latest_only = bool(re.search(r"\b(latest|last|most recent)\b", lowered)) and not asks_history
    if assessment_requested and assessment_entries:
        assessment_entries.sort(
            key=lambda item: str(item.get("assessed_on", item.get("date", ""))),
            reverse=True,
        )
        selected_entries = assessment_entries[:1] if asks_latest_only else assessment_entries
        for entry in selected_entries:
            for key in ("subject", "assessment"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    required_facts.append(value.strip())
            date_value = entry.get("assessed_on", entry.get("date"))
            if date_value is not None:
                if hasattr(date_value, "date") and callable(date_value.date):
                    date_value = date_value.date()
                if hasattr(date_value, "isoformat") and callable(date_value.isoformat):
                    date_value = date_value.isoformat()
                date_text = str(date_value)
                if re.match(r"^\d{4}-\d{2}-\d{2}", date_text):
                    date_text = date_text[:10]
                if date_text:
                    required_facts.append(date_text)
            score = entry.get("score")
            out_of = entry.get("out_of")
            if (
                isinstance(score, (int, float)) and not isinstance(score, bool)
                and isinstance(out_of, (int, float)) and not isinstance(out_of, bool)
                and float(score).is_integer() and float(out_of).is_integer()
            ):
                expected_scores.append([int(score), int(out_of)])

    improvement_values = _record_values(records, ("areas_for_improvement", "improvement_areas", "could_work_on"))
    asks_for_advice = bool(re.search(r"\b(improv\w*|suggest\w*|recommend\w*|advice|feedback|next step|work on)\b", lowered))
    asks_for_all_improvements = bool(re.search(
        r"\bareas\s+(?:of|for|to)\s+improvement\b"
        r"|\ball\s+(?:the\s+)?(?:recorded\s+)?improvement\s+areas\b"
        r"|\bevery\s+recorded\s+improvement\s+area\b",
        lowered,
    ))
    asks_for_one_improvement = bool(re.search(
        r"\bone\s+(?:(?:direct|small|practical|clear|specific|recorded)\s+)*(?:"
        r"area(?:\s+(?:for|of)\s+improvement)?|improvement(?:\s+area)?|thing)\b",
        lowered,
    ))
    improvement_mode = "all" if asks_for_all_improvements else "any" if asks_for_advice or asks_for_one_improvement else "none"
    if improvement_mode == "all":
        required_facts.extend(improvement_values)

    return list(dict.fromkeys(required_facts)), expected_scores, improvement_values if improvement_mode == "any" else [], improvement_mode


def _comparison_check(answer: str, case: dict[str, Any], generation_issues: list[str] | None = None) -> dict[str, Any]:
    result = _deterministic_check(answer, case)
    normalized_answer = " ".join(re.findall(r"[a-z0-9]+", answer.casefold()))
    identity_options = [
        " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
        for value in case.get("required_identity_any", [])
    ]
    if identity_options and not any(
        f" {identity} " in f" {normalized_answer} "
        for identity in identity_options if identity
    ):
        result["issues"].append("Identify the student by name or student ID.")
    for issue in generation_issues or []:
        if issue not in result["issues"]:
            result["issues"].append("Response contract: " + issue)
    required_improvements = case.get("required_improvements_any", [])
    if required_improvements and not any(value.casefold() in answer.casefold() for value in required_improvements):
        result["issues"].append("Mention at least one recorded improvement area: " + "; ".join(required_improvements))
    result["passed"] = not result["issues"]
    return result


def _registered_applications(project_root: Path, active: dict[str, Any], candidate: dict[str, Any], evaluation: dict[str, Any] | None) -> list[dict[str, Any]]:
    registry_path = project_root / "config" / "applications.json"
    cases_path = project_root / "config" / "behavior_regression_cases.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    cases_data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = list(cases_data.get("cases", []))
    holdout_path = project_root / "config" / "behavior_holdout_cases.json"
    if holdout_path.is_file():
        holdout_data = json.loads(holdout_path.read_text(encoding="utf-8"))
        cases.extend(holdout_data.get("cases", []))
    applications: list[dict[str, Any]] = []

    for configured in registry.get("applications", []):
        app = dict(configured)
        persona_id = app.get("persona_id")
        persona = active.get("personas", {}).get(persona_id, {})
        prompt = persona.get("system_prompt", "")
        expected_prompt_hash = app.get("prompt_sha256")
        computed_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else None
        app["prompt_sha256"] = computed_prompt_hash
        app["prompt_revision"] = app.get("prompt_revision", active.get("prompt_revision"))
        app["active_release"] = active.get("release_id")
        app["active_model"] = active.get("model")
        app["benchmark_cases"] = sum(
            case.get("tutor_id") == persona_id or case.get("application_id") == app.get("application_id")
            for case in cases
        )
        app["prompt_loaded"] = bool(prompt)
        app["prompt_matches_snapshot"] = not expected_prompt_hash or expected_prompt_hash == computed_prompt_hash
        has_app_credential = bool(
            app.get("gateway_token_sha256")
            or (app.get("gateway_token_env") and os.getenv(app.get("gateway_token_env"), "").strip())
        )
        app["auth_mode"] = "Per-application bearer token" if has_app_credential else "Legacy shared gateway token"
        app.pop("gateway_token_sha256", None)
        app["candidate_model"] = candidate.get("model") or None
        case_results = [
            case for case in (evaluation or {}).get("cases", [])
            if case.get("tutor_id") == persona_id or case.get("application_id") == app.get("application_id")
        ]
        same_candidate = evaluation and evaluation.get("candidate_model") == candidate.get("model")
        all_app_cases_passed = bool(case_results) and all(item.get("status") == "passed" for item in case_results)
        app_has_failed_case = any(item.get("status") == "failed" for item in case_results)
        if not same_candidate:
            app["candidate_status"] = "not_evaluated"
        elif evaluation.get("status") in {"stale", "blocked", "running"}:
            app["candidate_status"] = evaluation["status"]
        elif evaluation.get("suite_stage") != "final" and all_app_cases_passed:
            app["candidate_status"] = "core_passed"
        elif evaluation.get("suite_stage") == "final" and all_app_cases_passed:
            app["candidate_status"] = "passed"
        elif app_has_failed_case:
            app["candidate_status"] = "failed"
        else:
            app["candidate_status"] = evaluation.get("status", "not_evaluated")
        app["latest_evaluation_at"] = (evaluation or {}).get("evaluated_at")
        app["latest_evaluation_status"] = app["candidate_status"]

        health_env = app.get("health_url_env", "")
        health_url = (os.getenv(health_env, "").strip() if health_env else "") or app.get("health_url", "").strip()
        app["health_url"] = health_url
        app["status"] = "offline"
        app["database"] = None
        app["collection"] = None
        app["student_count"] = None
        app["observed_model"] = None
        try:
            with urlopen(health_url, timeout=2.5) as response:
                health = json.loads(response.read().decode("utf-8"))
            app["status"] = "online" if health.get("ok") and (health.get("model") or {}).get("configured") else "degraded"
            app["database"] = health.get("database_name")
            app["collection"] = health.get("collection_name")
            app["student_count"] = health.get("student_count")
            model_status = health.get("model") or {}
            app["observed_model"] = model_status.get("model")
            if app["observed_model"] and app["observed_model"] != active.get("model"):
                app["status"] = "degraded"
            child_prompt_hash = health.get("prompt_sha256")
            app["observed_prompt_sha256"] = child_prompt_hash
            if child_prompt_hash and child_prompt_hash != app.get("prompt_sha256"):
                app["status"] = "degraded"
                app["prompt_matches_snapshot"] = False
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        app["active_release"] = active.get("release_id")
        integration = app.get("data_integration")
        app["application_data_scope"] = (
            integration.get("scope", "Application credential scoped")
            if isinstance(integration, dict)
            else str(integration or "Application credential scoped")
        )
        applications.append(app)
    return applications


def create_parent_app(
    *,
    project_root: Path = PROJECT_ROOT,
    store: ControlStore | None = None,
    provider: AzureResponsesClient | None = None,
    admin_token: str | None = None,
    gateway_token: str | None = None,
    judge_models: list[str] | None = None,
) -> Flask:
    project_root = project_root.resolve()
    state_path = Path(os.getenv("MODEL_CONTROL_STATE_PATH", project_root / "config" / "model_control.json"))
    if not state_path.is_absolute():
        state_path = project_root / state_path
    control_store = store or ControlStore(
        state_path,
        project_root,
        initial_model=os.getenv("TUTOR_MODEL", "gpt-5-mini"),
    )
    control_store.recover_interrupted_evaluation()
    application_registry = ApplicationRegistry(project_root / "config" / "applications.json")
    azure = provider or AzureResponsesClient()
    try:
        evaluation_repetitions = int(os.getenv("MODEL_EVAL_REPETITIONS", "3"))
    except ValueError as error:
        raise ValueError("MODEL_EVAL_REPETITIONS must be an integer from 2 to 5.") from error
    if not 2 <= evaluation_repetitions <= 5:
        raise ValueError("MODEL_EVAL_REPETITIONS must be an integer from 2 to 5.")
    configured_judges = list(judge_models) if judge_models is not None else _configured_judge_models()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
        static_url_path="/static",
    )
    app.config.update(
        CONTROL_STORE=control_store,
        APPLICATION_REGISTRY=application_registry,
        AZURE_PROVIDER=azure,
        PROJECT_ROOT=project_root,
        MODEL_ADMIN_TOKEN=admin_token if admin_token is not None else os.getenv("MODEL_ADMIN_TOKEN", ""),
        MODEL_GATEWAY_TOKEN=gateway_token if gateway_token is not None else os.getenv("MODEL_GATEWAY_TOKEN", ""),
        ALLOW_SHARED_GATEWAY_OVERRIDE=gateway_token is not None,
        EVALUATION_REPETITIONS=evaluation_repetitions,
        EVALUATION_JUDGE_MODELS=configured_judges,
        EVALUATION_LOCK=threading.Lock(),
    )

    def store_from_app() -> ControlStore:
        return current_app.config["CONTROL_STORE"]

    def provider_from_app() -> AzureResponsesClient:
        return current_app.config["AZURE_PROVIDER"]

    def require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            if not _bearer_matches(current_app.config["MODEL_ADMIN_TOKEN"]):
                return jsonify({"error": "Enter the parent control token to manage releases."}), 401
            return view(*args, **kwargs)
        return wrapped

    @app.get("/")
    def home():
        return render_template("parent.html")

    @app.get("/api/health")
    def health():
        active = store_from_app().get_active_release()
        candidate = store_from_app().get_candidate()
        evaluation = _apply_suite_freshness(
            store_from_app().get_public_state(), project_root, current_app.config["EVALUATION_JUDGE_MODELS"]
        ).get("last_evaluation")
        if candidate["model"]:
            candidate_status = evaluation.get("status") if evaluation and evaluation.get("candidate_model") == candidate["model"] else "not_evaluated"
            if candidate_status == "passed" and evaluation.get("suite_stage") != "final":
                candidate_status = "core_passed"
        elif evaluation and active.get("evaluation_id") == evaluation.get("evaluation_id"):
            candidate_status = "promoted"
        else:
            candidate_status = "not_staged"
        configured = provider_from_app().is_configured()
        return jsonify({
            "ok": configured,
            "provider": "Azure OpenAI",
            "model": active["model"],
            "active_release": active["release_id"],
            "candidate_model": candidate["model"] or None,
            "candidate_status": candidate_status,
            "judge_models": current_app.config["EVALUATION_JUDGE_MODELS"],
            "judge_panel_configured": validate_judge_models(
                current_app.config["EVALUATION_JUDGE_MODELS"], candidate.get("model", "")
            ) is None,
            "azure_configured": configured,
            "admin_auth_configured": bool(current_app.config["MODEL_ADMIN_TOKEN"]),
            "gateway_auth_configured": bool(current_app.config["MODEL_GATEWAY_TOKEN"])
            or any(item.get("gateway_token_sha256") or os.getenv(item.get("gateway_token_env", ""), "") for item in application_registry.list_applications()),
            "observability": {"langfuse": langfuse_status()},
        }), (200 if configured else 503)

    @app.get("/api/observability/langfuse")
    @require_admin
    def langfuse_health():
        status = langfuse_status()
        status["connected"] = check_langfuse_connection() if status["enabled"] else False
        return jsonify(status), (200 if status["connected"] else 503)

    @app.get("/api/v1/status")
    def gateway_status():
        application_id = request.headers.get("X-Application-ID", "").strip()
        if not _authenticated_application(application_id or None):
            return jsonify({"error": "The client application is not authorized to use this parent gateway."}), 401
        active = store_from_app().get_active_release()
        return jsonify({
            "provider": "Azure OpenAI",
            "model": active["model"],
            "release_id": active["release_id"],
            "contract_revision": active["contract_revision"],
            "prompt_revision": active["prompt_revision"],
        })

    @app.post("/api/v1/respond")
    def gateway_respond():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with an application ID, question, and selected records."}), 400
        requested_tutor_id = payload.get("tutor_id")
        application_id = payload.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            if isinstance(requested_tutor_id, str):
                application_id = next(
                    (item.get("application_id") for item in application_registry.list_applications()
                     if item.get("persona_id") == requested_tutor_id),
                    None,
                )
        if not isinstance(application_id, str) or not application_id:
            return jsonify({"error": "An application ID is required."}), 400
        registered_app = application_registry.get_application(application_id)
        if not registered_app:
            return jsonify({"error": "That client application is not registered."}), 404
        if not _authenticated_application(application_id):
            return jsonify({"error": "This credential is not authorized for the requested application."}), 401
        tutor_id = registered_app.get("persona_id") or registered_app.get("application_id")
        if requested_tutor_id and requested_tutor_id != tutor_id:
            return jsonify({"error": "The requested tutor does not belong to this application credential."}), 403
        question = payload.get("question")
        records = payload.get("student_records")
        if not isinstance(tutor_id, str) or not isinstance(question, str) or not question.strip():
            return jsonify({"error": "A registered application and non-empty question are required."}), 400
        if len(question) > 4000:
            return jsonify({"error": "Questions must be 4,000 characters or fewer."}), 400
        if not isinstance(records, list) or not records or len(records) > 5 or not all(isinstance(item, dict) for item in records):
            return jsonify({"error": "Send between one and five selected MongoDB records."}), 400
        if len(json.dumps(records, ensure_ascii=False, default=str)) > 100_000:
            return jsonify({"error": "The selected MongoDB context is too large."}), 413

        active = store_from_app().get_active_release()
        if tutor_id not in active["personas"]:
            return jsonify({"error": "This application's prompt snapshot is not present in the active release."}), 409
        with observation_scope(
            name="parent.tutor_response",
            metadata={
                "application_id": application_id,
                "tutor_id": tutor_id,
                "model": active["model"],
                "release_id": active["release_id"],
                "contract_revision": active["contract_revision"],
                "request_kind": "live_tutor_response",
                "tags": ["gateway", "live"],
            },
        ) as trace:
            try:
                answer = generate_tutor_answer(
                    provider=provider_from_app(),
                    model=active["model"],
                    instructions=_response_instructions(active, tutor_id),
                    question=question.strip(),
                    records=records,
                    max_output_tokens=3000,
                )
            except AzureResponsesError as error:
                safe_observation_update(
                    trace,
                    output={"status": "error", "error_type": "AzureResponsesError"},
                )
                return jsonify({"error": str(error)}), 502
            safe_observation_update(
                trace,
                output={"status": "success", "answer": answer.strip()},
            )
        return jsonify({
            "answer": answer.strip(),
            "mode": f"Azure OpenAI | {active['model']}",
            "release_id": active["release_id"],
            "contract_revision": active["contract_revision"],
            "prompt_revision": active["prompt_revision"],
        })

    @app.get("/api/control/state")
    @require_admin
    def control_state():
        return jsonify(_apply_suite_freshness(
            store_from_app().get_public_state(), project_root, current_app.config["EVALUATION_JUDGE_MODELS"]
        ))

    @app.get("/api/control/applications")
    @require_admin
    def control_applications():
        public_state = _apply_suite_freshness(
            store_from_app().get_public_state(), project_root, current_app.config["EVALUATION_JUDGE_MODELS"]
        )
        try:
            applications = _registered_applications(
                project_root,
                public_state["active"],
                public_state["candidate"],
                public_state.get("last_evaluation"),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return jsonify({"error": f"Could not load the application registry: {error}"}), 503
        return jsonify({"applications": applications})

    @app.get("/api/control/applications/summary")
    @app.get("/api/control/poc-applications")
    @require_admin
    def control_application_summary():
        """Return tutor choices and saved evaluation case counts for the dashboard."""
        try:
            public_state = store_from_app().get_public_state()
            applications = []
            for item in application_registry.list_applications():
                persona_id = item.get("persona_id") or item.get("application_id")
                persona = public_state["active"].get("personas", {}).get(persona_id, {})
                applications.append({
                    "application_id": item.get("application_id"),
                    "persona_id": persona_id,
                    "display_name": item.get("display_name") or persona.get("name") or item.get("application_id"),
                })
            cases_data = json.loads((project_root / "config" / "behavior_regression_cases.json").read_text(encoding="utf-8"))
            holdout_path = project_root / "config" / "behavior_holdout_cases.json"
            holdout_data = json.loads(holdout_path.read_text(encoding="utf-8")) if holdout_path.is_file() else {"cases": []}
        except (ApplicationRegistryError, OSError, ValueError, json.JSONDecodeError) as error:
            return jsonify({"error": f"Could not load the application registry and saved checks: {error}"}), 503
        return jsonify({
            "applications": applications,
            "case_count": len(cases_data.get("cases", [])) + len(holdout_data.get("cases", [])),
            "core_case_count": len(cases_data.get("cases", [])),
            "holdout_case_count": len(holdout_data.get("cases", [])),
            "repetitions": current_app.config["EVALUATION_REPETITIONS"],
            "judge_models": current_app.config["EVALUATION_JUDGE_MODELS"],
            "judge_panel_configured": validate_judge_models(
                current_app.config["EVALUATION_JUDGE_MODELS"], store_from_app().get_candidate().get("model", "")
            ) is None,
        })

    @app.post("/api/control/applications")
    @require_admin
    def register_application():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send the application details as a JSON object."}), 400
        application_id = payload.get("application_id")
        display_name = payload.get("display_name")
        system_prompt = payload.get("system_prompt")
        role = payload.get("role", "Registered client application")
        style = payload.get("style", "custom")
        regression_cases = payload.get("regression_cases")
        if not all(isinstance(value, str) and value.strip() for value in (application_id, display_name, system_prompt)):
            return jsonify({"error": "Application ID, display name, and the existing system prompt are required."}), 400
        if len(system_prompt) > 16000:
            return jsonify({"error": "Application prompts must be 16,000 characters or fewer."}), 400
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", application_id.strip()):
            return jsonify({"error": "Application IDs must be 2-64 letters, numbers, underscores, or hyphens."}), 400
        application_id = application_id.strip()
        if not isinstance(role, str) or not role.strip() or not isinstance(style, str) or not style.strip():
            return jsonify({"error": "Application persona role and style must be non-empty text."}), 400
        if not isinstance(regression_cases, list) or not regression_cases:
            return jsonify({"error": "Add at least one representative regression case for this application."}), 400

        normalized_cases: list[dict[str, Any]] = []
        for index, case in enumerate(regression_cases, start=1):
            if not isinstance(case, dict) or not isinstance(case.get("question"), str) or not case["question"].strip():
                return jsonify({"error": f"Regression case {index} needs a non-empty question."}), 400
            records = case.get("student_records")
            if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
                return jsonify({"error": f"Regression case {index} needs one or more JSON records."}), 400
            required_facts = case.get("required_facts", [])
            score_pairs = case.get("expected_score_pairs", [])
            if not isinstance(required_facts, list) or not all(isinstance(item, str) and item.strip() for item in required_facts):
                return jsonify({"error": f"Regression case {index} required_facts must be a list of non-empty strings."}), 400
            if not isinstance(score_pairs, list) or any(
                not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) for value in pair)
                for pair in score_pairs
            ):
                return jsonify({"error": f"Regression case {index} expected_score_pairs must be a list of integer pairs."}), 400
            if "persona_expectation" in case and not isinstance(case["persona_expectation"], str):
                return jsonify({"error": f"Regression case {index} persona_expectation must be text."}), 400
            case_name = case.get("id") or f"case-{index}"
            if not isinstance(case_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", case_name):
                return jsonify({"error": f"Regression case {index} has an invalid ID."}), 400
            normalized = dict(case)
            normalized["id"] = f"{application_id}-{case_name}"
            normalized["application_id"] = application_id
            normalized["tutor_id"] = application_id
            normalized["persona_expectation"] = str(case.get("persona_expectation", role))
            normalized.setdefault("required_facts", [])
            normalized.setdefault("expected_score_pairs", [])
            normalized.setdefault("min_words", 0)
            normalized.setdefault("max_paragraphs", 3)
            normalized.setdefault("forbid_bullets", True)
            normalized.setdefault("forbid_invented_schedule", True)
            normalized_cases.append(normalized)

        health_url = payload.get("health_url", "")
        if not isinstance(health_url, str):
            return jsonify({"error": "Health URL must be text."}), 400
        health_url = health_url.strip()
        if health_url:
            parsed = urlsplit(health_url)
            local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "https" and not local_http:
                return jsonify({"error": "Use an HTTPS health URL, or localhost HTTP for a development server."}), 400

        api_schema = payload.get("api_schema") or {
            "version": "v1",
            "request": ["application_id", "question", "student_records"],
            "response": ["answer", "mode", "release_id", "contract_revision", "prompt_revision"],
        }
        tool_schema = payload.get("tool_schema", [])
        data_integration = payload.get("data_integration") or {
            "mode": "client_selected_context",
            "description": "The client selects and sends the relevant records to the parent gateway.",
            "scope": "The application credential is bound to this registered application ID.",
        }
        if not isinstance(api_schema, (dict, list)) or not isinstance(tool_schema, (dict, list)) or not isinstance(data_integration, (dict, str)):
            return jsonify({"error": "API schema, tool schema, and data integration must be JSON values."}), 400

        existing_cases_path = project_root / "config" / "behavior_regression_cases.json"
        try:
            existing_case_ids = {
                item.get("id")
                for item in json.loads(existing_cases_path.read_text(encoding="utf-8")).get("cases", [])
            }
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return jsonify({"error": f"Could not read the regression suite: {error}"}), 503
        if any(case["id"] in existing_case_ids for case in normalized_cases):
            return jsonify({"error": "A regression case with that application ID already exists."}), 409
        if application_registry.get_application(application_id) or application_id in store_from_app().get_active_release().get("personas", {}):
            return jsonify({"error": "That application ID is already registered."}), 409

        credential = secrets.token_urlsafe(32)
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        persona = {
            "id": application_id,
            "name": display_name,
            "short_name": payload.get("short_name") or display_name,
            "role": role,
            "style": style,
            "system_prompt": system_prompt,
        }
        application = {
            "application_id": application_id,
            "display_name": display_name,
            "persona_id": application_id,
            "health_url_env": "",
            "health_url": health_url,
            "gateway_token_env": "",
            "gateway_token_sha256": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
            "legacy_shared_token_fallback": False,
            "prompt_revision": 1,
            "prompt_sha256": prompt_hash,
            "data_source": str(payload.get("data_source") or "Client-supplied selected records"),
            "data_integration": data_integration,
            "api_schema": api_schema,
            "tool_schema": tool_schema,
            "request_contract": "POST /api/v1/respond: application_id, question, student_records; response: answer, mode, release_id, contract_revision, prompt_revision",
            "auth_mode": "Per-application bearer token",
            "regression_case_ids": [case["id"] for case in normalized_cases],
        }
        try:
            application_registry.register(application)
            store_from_app().register_application_persona(persona)
            _append_regression_cases(project_root, normalized_cases)
        except (ApplicationRegistryError, ControlStoreError, OSError, ValueError, json.JSONDecodeError) as error:
            return jsonify({"error": f"The application could not be registered: {error}"}), 400
        return jsonify({"application": _public_application(application), "gateway_token": credential}), 201

    @app.post("/api/control/compare")
    @require_admin
    def compare_active_and_candidate():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send the comparison request as a JSON object."}), 400
        application_id = payload.get("application_id")
        question = payload.get("question")
        records = payload.get("student_records")
        if not isinstance(application_id, str) or not isinstance(question, str) or not question.strip():
            return jsonify({"error": "Choose a registered application and enter a question."}), 400
        if len(question) > 4000:
            return jsonify({"error": "Questions must be 4,000 characters or fewer."}), 400
        application = application_registry.get_application(application_id)
        if not application:
            return jsonify({"error": "That client application is not registered."}), 404
        context_source = "Manual JSON context"
        if records is None:
            integration = application.get("data_integration") or {}
            if isinstance(integration, dict) and integration.get("mode") == "local_shared_mongodb_question_lookup":
                try:
                    matches = _find_comparison_students(question)
                except PyMongoError:
                    return jsonify({"error": "The local student database is unavailable for this comparison."}), 503
                if not matches:
                    return jsonify({"error": "Include the student's full name or student ID in the question."}), 400
                if len(matches) > 1:
                    return jsonify({"error": "The question matches multiple students. Include one student's full name or exact ID."}), 409
                student = matches[0]
                records = [student_context_for_question(student, question.casefold())]
                student_name = str(student.get("name") or "Student")
                student_id = str(student.get("student_id") or "")
                context_source = f"MongoDB record: {student_name}" + (f" ({student_id})" if student_id else "")
            else:
                return jsonify({"error": "This tutor has no local student lookup configured."}), 400
        if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
            return jsonify({"error": "Student context must contain one or more records."}), 400
        if len(records) > 5 or len(json.dumps(records, ensure_ascii=False, default=str)) > 100_000:
            return jsonify({"error": "Comparison context must contain at most five records and 100,000 characters."}), 413
        required_facts = payload.get("required_facts", [])
        score_pairs = payload.get("expected_score_pairs", [])
        max_paragraphs = payload.get("max_paragraphs", 3)
        if not isinstance(required_facts, list) or not all(isinstance(item, str) and item.strip() for item in required_facts):
            return jsonify({"error": "required_facts must be a list of non-empty strings."}), 400
        if not isinstance(score_pairs, list) or any(
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) for value in pair)
            for pair in score_pairs
        ):
            return jsonify({"error": "expected_score_pairs must be a list of integer pairs."}), 400
        if not isinstance(max_paragraphs, int) or isinstance(max_paragraphs, bool) or not 1 <= max_paragraphs <= 10:
            return jsonify({"error": "max_paragraphs must be an integer from 1 to 10."}), 400
        active = store_from_app().get_active_release()
        candidate = store_from_app().get_candidate()
        if not candidate.get("model"):
            return jsonify({"error": "Stage a candidate model before running a side-by-side comparison."}), 400
        if candidate["model"] == active["model"]:
            return jsonify({"error": "Choose a candidate deployment different from the active model."}), 400
        persona_id = application.get("persona_id") or application_id
        if persona_id not in active["personas"] or persona_id not in candidate["personas"]:
            return jsonify({"error": "The registered prompt is missing from one of the releases."}), 409
        derived_facts, derived_scores, required_improvements_any, improvement_mode = _comparison_requirements(question, records)
        case = {
            "question": question.strip(),
            "student_records": records,
            "required_facts": list(dict.fromkeys([*derived_facts, *required_facts])),
            "required_identity_any": _identity_options(records),
            "expected_score_pairs": [*derived_scores, *score_pairs],
            "required_improvements_any": required_improvements_any,
            "min_words": 0,
            "max_paragraphs": max_paragraphs,
        }
        try:
            baseline_result = generate_tutor_answer_diagnostic(
                provider=provider_from_app(), model=active["model"],
                instructions=_response_instructions(active, persona_id),
                question=case["question"], records=records, max_output_tokens=2200,
                max_paragraphs=max_paragraphs,
            )
            candidate_result = generate_tutor_answer_diagnostic(
                provider=provider_from_app(), model=candidate["model"],
                instructions=_response_instructions(candidate, persona_id),
                question=case["question"], records=records, max_output_tokens=2200,
                max_paragraphs=max_paragraphs,
            )
        except AzureResponsesError as error:
            return jsonify({"error": str(error)}), 502
        baseline_answer = baseline_result["answer"]
        candidate_answer = candidate_result["answer"]
        baseline_first_pass_check = _comparison_check(
            baseline_result["first_answer"], case, baseline_result["first_issues"]
        )
        candidate_first_pass_check = _comparison_check(
            candidate_result["first_answer"], case, candidate_result["first_issues"]
        )
        review = {
            "status": "human_review_only",
            "summary": (
                "This manual comparison did not send request context to an LLM judge. "
                "Review persona and tone here; automated judge checks use the saved synthetic regression cases."
            ),
            "issues": [],
            "score_drops": {},
            "judge_models": [],
            "judges": [],
            "all_judges_passed": None,
        }
        return jsonify({
            "application_id": application_id,
            "active_model": active["model"],
            "candidate_model": candidate["model"],
            "baseline_answer": baseline_answer,
            "candidate_answer": candidate_answer,
            "baseline_first_answer": baseline_result["first_answer"],
            "candidate_first_answer": candidate_result["first_answer"],
            "baseline_first_pass_check": baseline_first_pass_check,
            "candidate_first_pass_check": candidate_first_pass_check,
            "baseline_repair_history": baseline_result["repair_history"],
            "candidate_repair_history": candidate_result["repair_history"],
            "baseline_check": _comparison_check(baseline_answer, case, baseline_result["issues"]),
            "candidate_check": _comparison_check(candidate_answer, case, candidate_result["issues"]),
            "behavior_review": review,
            "prompt_sha256": hashlib.sha256(active["personas"][persona_id]["system_prompt"].encode("utf-8")).hexdigest(),
            "context_source": context_source,
            "baseline_repair_attempts": baseline_result["repair_attempts"],
            "candidate_repair_attempts": candidate_result["repair_attempts"],
            "check_scope": {
                "facts": case["required_facts"],
                "score_pairs": case["expected_score_pairs"],
                "improvement_mode": improvement_mode,
            },
        })

    @app.put("/api/control/candidate/model")
    @require_admin
    def candidate_model():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return jsonify({"error": "Provide an Azure deployment name."}), 400
        requested_model = payload["model"].strip()
        if requested_model and requested_model == store_from_app().get_active_release()["model"]:
            return jsonify({"error": "The candidate deployment must differ from the active model."}), 400
        try:
            candidate = store_from_app().update_candidate_model(requested_model)
            return jsonify({"candidate": candidate})
        except ControlStoreError as error:
            return jsonify({"error": str(error)}), 400

    @app.put("/api/control/candidate/contract")
    @require_admin
    def candidate_contract():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("behavior_contract"), str):
            return jsonify({"error": "Provide the shared response contract."}), 400
        try:
            candidate = store_from_app().update_candidate_contract(payload["behavior_contract"])
            return jsonify({"candidate": candidate})
        except ControlStoreError as error:
            return jsonify({"error": str(error)}), 400

    @app.put("/api/control/candidate/applications/<application_id>/compatibility")
    @require_admin
    def candidate_application_compatibility(application_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("guidance"), str):
            return jsonify({"error": "Provide parent-owned compatibility guidance as text. Use an empty string to remove it."}), 400
        if len(payload["guidance"]) > 8000:
            return jsonify({"error": "Compatibility guidance must be 8,000 characters or fewer."}), 400
        application = application_registry.get_application(application_id)
        if not application:
            return jsonify({"error": "That client application is not registered."}), 404
        persona_id = application.get("persona_id") or application_id
        try:
            candidate = store_from_app().update_candidate_model_adapter(persona_id, payload["guidance"])
            return jsonify({"candidate": candidate, "application_id": application_id})
        except ControlStoreError as error:
            return jsonify({"error": str(error)}), 400

    @app.put("/api/control/candidate/tutors/<tutor_id>/prompt")
    @require_admin
    def candidate_prompt(tutor_id: str):
        return jsonify({
            "error": "Registered tutor persona prompts are locked for model upgrades. Stage changes only to the parent-owned behavior contract."
        }), 409

    @app.post("/api/control/evaluate")
    @require_admin
    def evaluate_candidate():
        store = store_from_app()
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "Send the evaluation suite stage as a JSON object."}), 400
        suite_stage = payload.get("suite_stage", "core")
        if not isinstance(suite_stage, str) or suite_stage not in {"core", "final"}:
            return jsonify({"error": "suite_stage must be 'core' or 'final'."}), 400
        active = store.get_active_release()
        candidate = store.get_candidate()
        if not candidate["model"]:
            return jsonify({"error": "Stage a candidate Azure deployment before running the checks."}), 400
        if candidate["model"] == active["model"]:
            return jsonify({"error": "The candidate deployment must differ from the active model."}), 400
        try:
            suite_fingerprint = _suite_fingerprint(project_root)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "The saved compatibility cases or application registry could not be read."}), 503
        evaluation_lock: threading.Lock = current_app.config["EVALUATION_LOCK"]
        if not evaluation_lock.acquire(blocking=False):
            return jsonify({"error": "A compatibility check is already running."}), 409

        candidate_fingerprint = store.candidate_fingerprint()
        active_release_id = active["release_id"]
        cases_path = project_root / "config" / "behavior_regression_cases.json"
        running = {
            "status": "running",
            "summary": (
                "Comparing repeated active and candidate responses against core cases. This review cannot promote the candidate."
                if suite_stage == "core"
                else "Running final validation against core and reserved cases. The active release continues serving requests."
            ),
            "active_model": active["model"],
            "candidate_model": candidate["model"],
            "repetitions": current_app.config["EVALUATION_REPETITIONS"],
            "judge_models": current_app.config["EVALUATION_JUDGE_MODELS"],
            "judge_panel_configured": validate_judge_models(
                current_app.config["EVALUATION_JUDGE_MODELS"], candidate["model"]
            ) is None,
            "suite_stage": suite_stage,
            "cases": [],
            "suite_fingerprint": suite_fingerprint,
        }
        try:
            store.save_evaluation(
                running,
                evaluated_fingerprint=candidate_fingerprint,
                evaluated_active_release_id=active_release_id,
            )
        except Exception:
            evaluation_lock.release()
            raise

        def evaluate_in_background() -> None:
            try:
                with app.app_context():
                    try:
                        evaluation = run_compatibility_check(
                            active_release=active,
                            candidate=candidate,
                            provider=azure,
                            cases_path=cases_path,
                            judge_models=app.config["EVALUATION_JUDGE_MODELS"],
                            repetitions=app.config["EVALUATION_REPETITIONS"],
                            suite_stage=suite_stage,
                        )
                    except Exception as error:
                        message = str(error) if isinstance(error, AzureResponsesError) else type(error).__name__
                        evaluation = {
                            "status": "blocked",
                            "summary": f"Compatibility check could not complete: {message}",
                            "active_model": active["model"],
                            "candidate_model": candidate["model"],
                            "suite_stage": suite_stage,
                            "cases": [],
                        }
                    evaluation["suite_fingerprint"] = suite_fingerprint
                    store.save_evaluation(
                        evaluation,
                        evaluated_fingerprint=candidate_fingerprint,
                        evaluated_active_release_id=active_release_id,
                    )
            except Exception as error:
                try:
                    store.save_evaluation(
                        {
                            "status": "blocked",
                            "summary": f"Compatibility worker failed ({type(error).__name__}). The active release remains in service.",
                            "active_model": active["model"],
                            "candidate_model": candidate["model"],
                            "suite_stage": suite_stage,
                            "suite_fingerprint": suite_fingerprint,
                            "cases": [],
                        },
                        evaluated_fingerprint=candidate_fingerprint,
                        evaluated_active_release_id=active_release_id,
                    )
                except Exception:
                    pass
                print(f"Compatibility worker failed ({type(error).__name__}); the saved active release remains in service.")
            finally:
                evaluation_lock.release()

        worker = threading.Thread(
            target=evaluate_in_background,
            name="manual-model-compatibility",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            evaluation_lock.release()
            store.save_evaluation(
                {
                    "status": "blocked",
                    "summary": "The compatibility worker could not be started. The active release remains in service.",
                    "active_model": active["model"],
                    "candidate_model": candidate["model"],
                    "judge_models": current_app.config["EVALUATION_JUDGE_MODELS"],
                    "suite_stage": suite_stage,
                    "cases": [],
                    "suite_fingerprint": suite_fingerprint,
                },
                evaluated_fingerprint=candidate_fingerprint,
                evaluated_active_release_id=active_release_id,
            )
            return jsonify({"error": "The compatibility worker could not be started."}), 503
        return jsonify(running), 202

    @app.post("/api/control/promote")
    @require_admin
    def promote_candidate():
        try:
            current_fingerprint = _suite_fingerprint(project_root)
            evaluation = store_from_app().get_public_state().get("last_evaluation") or {}
            if evaluation.get("suite_fingerprint") != current_fingerprint:
                return jsonify({"error": "Promotion is blocked because the compatibility suite changed or has not been checked. Run the checks again."}), 409
            if evaluation.get("judge_models") != current_app.config["EVALUATION_JUDGE_MODELS"]:
                return jsonify({"error": "Promotion is blocked because this candidate was not checked with the currently configured judge panel. Run final validation again."}), 409
            release = store_from_app().promote_candidate()
            return jsonify({"release": release, "state": _apply_suite_freshness(
                store_from_app().get_public_state(), project_root, current_app.config["EVALUATION_JUDGE_MODELS"]
            )})
        except ControlStoreError as error:
            return jsonify({"error": str(error)}), 409

    @app.post("/api/control/rollback")
    @require_admin
    def rollback_release():
        try:
            release = store_from_app().rollback()
            return jsonify({"release": release, "state": store_from_app().get_public_state()})
        except ControlStoreError as error:
            return jsonify({"error": str(error)}), 409

    return app


def reconcile_startup_model(app: Flask) -> dict[str, Any]:
    """Treat a changed TUTOR_MODEL as a candidate and require admin promotion."""
    requested_model = os.getenv("TUTOR_MODEL", "").strip()
    if not requested_model:
        return {"status": "unchanged", "summary": "TUTOR_MODEL is empty; the saved active release remains in service."}

    store: ControlStore = app.config["CONTROL_STORE"]
    provider: AzureResponsesClient = app.config["AZURE_PROVIDER"]
    active = store.get_active_release()
    if requested_model == active["model"]:
        return {
            "status": "unchanged",
            "active_model": active["model"],
            "summary": "TUTOR_MODEL matches the saved active release.",
        }

    try:
        candidate = store.stage_model_candidate(requested_model)
    except ControlStoreError as error:
        return {"status": "blocked", "active_model": active["model"], "summary": str(error)}

    fingerprint = store.candidate_fingerprint()
    suite_fingerprint = _suite_fingerprint(app.config["PROJECT_ROOT"])
    store.save_evaluation(
        {
            "status": "running",
            "summary": f"Checking {requested_model} against the active {active['model']} release. The active model continues serving until all checks pass.",
            "active_model": active["model"],
            "candidate_model": requested_model,
            "repetitions": app.config["EVALUATION_REPETITIONS"],
            "judge_models": app.config["EVALUATION_JUDGE_MODELS"],
            "judge_panel_configured": validate_judge_models(
                app.config["EVALUATION_JUDGE_MODELS"], requested_model
            ) is None,
            "suite_stage": "core",
            "suite_fingerprint": suite_fingerprint,
            "cases": [],
        },
        evaluated_fingerprint=fingerprint,
        evaluated_active_release_id=active["release_id"],
    )
    try:
        evaluation = run_compatibility_check(
            active_release=active,
            candidate=candidate,
            provider=provider,
            cases_path=app.config["PROJECT_ROOT"] / "config" / "behavior_regression_cases.json",
            judge_models=app.config["EVALUATION_JUDGE_MODELS"],
            repetitions=app.config["EVALUATION_REPETITIONS"],
            suite_stage="core",
        )
    except Exception as error:
        evaluation = {
            "status": "blocked",
            "summary": f"Startup compatibility check could not complete ({type(error).__name__}). The saved active release remains in service.",
            "active_model": active["model"],
            "candidate_model": requested_model,
            "suite_stage": "core",
            "cases": [],
        }

    evaluation["suite_fingerprint"] = suite_fingerprint
    store.save_evaluation(
        evaluation,
        evaluated_fingerprint=fingerprint,
        evaluated_active_release_id=active["release_id"],
    )
    public_state = _apply_suite_freshness(
        store.get_public_state(), app.config["PROJECT_ROOT"], app.config["EVALUATION_JUDGE_MODELS"]
    )
    saved_evaluation = public_state.get("last_evaluation") or {}
    if saved_evaluation.get("promotion_allowed"):
        return {
            "status": "passed",
            "active_model": active["model"],
            "candidate_model": requested_model,
            "summary": "All compatibility checks passed. Review the results and promote the candidate from the parent dashboard.",
        }

    return {
        "status": saved_evaluation.get("status", "blocked"),
        "active_model": active["model"],
        "candidate_model": requested_model,
        "summary": saved_evaluation.get("summary", "Candidate was not promoted; the saved active release remains in service."),
    }


def start_startup_model_reconciliation(app: Flask) -> threading.Thread | None:
    """Run a requested model upgrade in the background while the active model serves."""
    requested_model = os.getenv("TUTOR_MODEL", "").strip()
    active = app.config["CONTROL_STORE"].get_active_release()
    if not requested_model or requested_model == active["model"]:
        return None

    def reconcile() -> None:
        evaluation_lock: threading.Lock = app.config["EVALUATION_LOCK"]
        if not evaluation_lock.acquire(blocking=False):
            print("Startup model check skipped: another compatibility evaluation is already running.")
            return
        try:
            with app.app_context():
                try:
                    result = reconcile_startup_model(app)
                    print(f"Startup model check: {result['status']} - {result['summary']}")
                except Exception as error:
                    print(f"Startup model check blocked ({type(error).__name__}); the saved active release remains in service.")
        finally:
            evaluation_lock.release()

    worker = threading.Thread(target=reconcile, name="startup-model-compatibility", daemon=True)
    worker.start()
    return worker


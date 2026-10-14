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

from model_migration.azure_responses import AzureResponsesClient, AzureResponsesError
from model_migration.application_registry import ApplicationRegistry, ApplicationRegistryError
from model_migration.control_store import ControlStore, ControlStoreError
from model_migration.evaluation import _deterministic_check, run_compatibility_check
from model_migration.generation import generate_tutor_answer
from model_migration.response_contract import response_contract_issues
from shared.config import load_env_file


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_env_file(PROJECT_ROOT / ".env")


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
    tutor = release["personas"][tutor_id]
    return (
        f"TUTOR PERSONA — {tutor['name']}\n"
        f"{tutor['system_prompt'].strip()}\n\n"
        f"{release['behavior_contract'].strip()}"
    )


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


def _registered_applications(project_root: Path, active: dict[str, Any], candidate: dict[str, Any], evaluation: dict[str, Any] | None) -> list[dict[str, Any]]:
    registry_path = project_root / "config" / "applications.json"
    cases_path = project_root / "config" / "behavior_regression_cases.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    cases_data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = cases_data.get("cases", [])
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
        app["candidate_status"] = (
            "passed" if case_results and all(item.get("status") == "passed" for item in case_results)
            else "failed" if any(item.get("status") == "failed" for item in case_results)
            else evaluation.get("status") if evaluation and evaluation.get("candidate_model") == candidate.get("model")
            else "not_evaluated"
        )
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
    application_registry = ApplicationRegistry(project_root / "config" / "applications.json")
    azure = provider or AzureResponsesClient()
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
        evaluation = store_from_app().get_public_state().get("last_evaluation")
        if candidate["model"]:
            candidate_status = evaluation.get("status") if evaluation and evaluation.get("candidate_model") == candidate["model"] else "not_evaluated"
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
            "azure_configured": configured,
            "admin_auth_configured": bool(current_app.config["MODEL_ADMIN_TOKEN"]),
            "gateway_auth_configured": bool(current_app.config["MODEL_GATEWAY_TOKEN"])
            or any(item.get("gateway_token_sha256") or os.getenv(item.get("gateway_token_env", ""), "") for item in application_registry.list_applications()),
        }), (200 if configured else 503)

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
            return jsonify({"error": str(error)}), 502
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
        return jsonify(store_from_app().get_public_state())

    @app.get("/api/control/applications")
    @require_admin
    def control_applications():
        public_state = store_from_app().get_public_state()
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
            normalized.setdefault("max_paragraphs", 2)
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
                return jsonify({"error": "Use an HTTPS health URL, or localhost HTTP for this local POC."}), 400

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
        if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
            return jsonify({"error": "Provide one or more JSON records for the comparison."}), 400
        if len(records) > 5 or len(json.dumps(records, ensure_ascii=False, default=str)) > 100_000:
            return jsonify({"error": "Comparison context must contain at most five records and 100,000 characters."}), 413
        required_facts = payload.get("required_facts", [])
        score_pairs = payload.get("expected_score_pairs", [])
        max_paragraphs = payload.get("max_paragraphs", 2)
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
        application = application_registry.get_application(application_id)
        if not application:
            return jsonify({"error": "That client application is not registered."}), 404
        persona_id = application.get("persona_id") or application_id
        if persona_id not in active["personas"] or persona_id not in candidate["personas"]:
            return jsonify({"error": "The registered prompt is missing from one of the releases."}), 409
        case = {
            "question": question.strip(),
            "student_records": records,
            "required_facts": required_facts,
            "expected_score_pairs": score_pairs,
            "min_words": 0,
            "max_paragraphs": max_paragraphs,
        }
        try:
            baseline_answer = generate_tutor_answer(
                provider=provider_from_app(), model=active["model"],
                instructions=_response_instructions(active, persona_id),
                question=case["question"], records=records, max_output_tokens=2200,
            )
            candidate_answer = generate_tutor_answer(
                provider=provider_from_app(), model=candidate["model"],
                instructions=_response_instructions(candidate, persona_id),
                question=case["question"], records=records, max_output_tokens=2200,
            )
        except AzureResponsesError as error:
            return jsonify({"error": str(error)}), 502
        return jsonify({
            "application_id": application_id,
            "active_model": active["model"],
            "candidate_model": candidate["model"],
            "baseline_answer": baseline_answer,
            "candidate_answer": candidate_answer,
            "baseline_check": _deterministic_check(baseline_answer, case),
            "candidate_check": _deterministic_check(candidate_answer, case),
            "prompt_sha256": hashlib.sha256(active["personas"][persona_id]["system_prompt"].encode("utf-8")).hexdigest(),
        })

    @app.put("/api/control/candidate/model")
    @require_admin
    def candidate_model():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            return jsonify({"error": "Provide an Azure deployment name."}), 400
        try:
            candidate = store_from_app().update_candidate_model(payload["model"])
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

    @app.put("/api/control/candidate/tutors/<tutor_id>/prompt")
    @require_admin
    def candidate_prompt(tutor_id: str):
        return jsonify({
            "error": "Registered tutor persona prompts are locked for model upgrades. Stage changes only to the parent-owned behavior contract."
        }), 409

    @app.post("/api/control/evaluate")
    @require_admin
    def evaluate_candidate():
        active = store_from_app().get_active_release()
        candidate = store_from_app().get_candidate()
        if not candidate["model"]:
            return jsonify({"error": "Enter a candidate Azure deployment name or set TUTOR_MODEL and restart the parent."}), 400
        fingerprint = store_from_app().candidate_fingerprint()
        try:
            evaluation = run_compatibility_check(
                active_release=active,
                candidate=candidate,
                provider=provider_from_app(),
                cases_path=project_root / "config" / "behavior_regression_cases.json",
            )
        except (AzureResponsesError, OSError, ValueError, json.JSONDecodeError) as error:
            evaluation = {
                "status": "blocked",
                "summary": f"Compatibility check could not complete: {error}",
                "candidate_model": candidate["model"],
                "cases": [],
            }
        store_from_app().save_evaluation(
            evaluation,
            evaluated_fingerprint=fingerprint,
            evaluated_active_release_id=active["release_id"],
        )
        return jsonify(store_from_app().get_public_state()["last_evaluation"])

    @app.post("/api/control/promote")
    @require_admin
    def promote_candidate():
        try:
            release = store_from_app().promote_candidate()
            return jsonify({"release": release, "state": store_from_app().get_public_state()})
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
    store.save_evaluation(
        {
            "status": "running",
            "summary": f"Checking {requested_model} against the active {active['model']} release. The active model continues serving until all checks pass.",
            "active_model": active["model"],
            "candidate_model": requested_model,
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
        )
    except Exception as error:
        evaluation = {
            "status": "blocked",
            "summary": f"Startup compatibility check could not complete ({type(error).__name__}). The saved active release remains in service.",
            "active_model": active["model"],
            "candidate_model": requested_model,
            "cases": [],
        }

    store.save_evaluation(
        evaluation,
        evaluated_fingerprint=fingerprint,
        evaluated_active_release_id=active["release_id"],
    )
    public_state = store.get_public_state()
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
        try:
            result = reconcile_startup_model(app)
            print(f"Startup model check: {result['status']} — {result['summary']}")
        except Exception as error:
            print(f"Startup model check blocked ({type(error).__name__}); the saved active release remains in service.")

    worker = threading.Thread(target=reconcile, name="startup-model-compatibility", daemon=True)
    worker.start()
    return worker


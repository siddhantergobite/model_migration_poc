from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

from model_migration.azure_responses import AzureResponsesError
from model_migration.application_registry import ApplicationRegistry
from model_migration.control_store import ControlStore, ControlStoreError
from model_migration.evaluation import (
    _deterministic_check,
    compatibility_suite_fingerprint,
    run_compatibility_check,
)
from model_migration.generation import generate_tutor_answer
from model_migration.parent_app import _apply_suite_freshness, create_parent_app
from model_migration.response_contract import response_contract_issues
from model_migration.release_prompts import release_instructions
from shared.tutor_runtime import TutorProviderError, answer_question


ROOT = Path(__file__).resolve().parents[1]


class FakeAzureProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def complete(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "Grounded tutor response from the selected student record."


class ControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ControlStore(
            Path(self.temp_dir.name) / "state.json",
            ROOT,
            initial_model="gpt-5-mini",
            candidate_model="gpt-6-luna",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_candidate_is_staged_and_cannot_promote_without_current_pass(self) -> None:
        self.assertEqual(self.store.get_active_release()["model"], "gpt-5-mini")
        self.assertEqual(self.store.get_candidate()["model"], "gpt-6-luna")
        with self.assertRaises(ControlStoreError):
            self.store.promote_candidate()

    def test_stale_evaluation_is_rejected_and_successful_release_rolls_back(self) -> None:
        active_id = self.store.get_active_release()["release_id"]
        fingerprint = self.store.candidate_fingerprint()
        self.store.update_candidate_prompt("tutor_abhishek", "Updated strict prompt for a candidate only.")
        self.store.save_evaluation(
            {"status": "passed", "suite_stage": "final", "case_counts": {"holdout": 1}, "summary": "old evaluation", "cases": []},
            evaluated_fingerprint=fingerprint,
            evaluated_active_release_id=active_id,
        )
        with self.assertRaises(ControlStoreError):
            self.store.promote_candidate()

        fingerprint = self.store.candidate_fingerprint()
        self.store.save_evaluation(
            {"status": "passed", "suite_stage": "final", "case_counts": {"holdout": 1}, "summary": "all checks passed", "cases": []},
            evaluated_fingerprint=fingerprint,
            evaluated_active_release_id=active_id,
        )
        promoted = self.store.promote_candidate()
        self.assertEqual(promoted["model"], "gpt-6-luna")
        self.assertEqual(self.store.get_active_release()["release_id"], promoted["release_id"])
        rolled_back = self.store.rollback()
        self.assertEqual(rolled_back["model"], "gpt-5-mini")
        self.assertEqual(self.store.get_active_release()["release_id"], "release-001")

    def test_model_deployment_name_is_validated(self) -> None:
        with self.assertRaises(ControlStoreError):
            self.store.update_candidate_model("not a deployment name!")

    def test_candidate_guidance_is_isolated_and_versioned_with_the_promoted_model(self) -> None:
        active = self.store.get_active_release()
        guidance = "Keep Abhishek's response blunt and choose one exact recorded improvement."
        before = self.store.candidate_fingerprint()
        candidate = self.store.update_candidate_model_adapter("tutor_abhishek", guidance)
        self.assertNotEqual(before, self.store.candidate_fingerprint())
        self.assertNotIn(guidance, release_instructions(active, "tutor_abhishek"))
        self.assertIn(guidance, release_instructions(candidate, "tutor_abhishek"))

        fingerprint = self.store.candidate_fingerprint()
        self.store.save_evaluation(
            {"status": "passed", "suite_stage": "final", "case_counts": {"holdout": 1}, "summary": "checks passed", "cases": []},
            evaluated_fingerprint=fingerprint,
            evaluated_active_release_id=active["release_id"],
        )
        promoted = self.store.promote_candidate()
        self.assertIn(guidance, release_instructions(promoted, "tutor_abhishek"))

    def test_interrupted_evaluation_is_recovered_as_blocked(self) -> None:
        self.store.save_evaluation({"status": "running", "summary": "in progress", "cases": []})
        self.assertTrue(self.store.recover_interrupted_evaluation())
        self.assertEqual(self.store.get_public_state()["last_evaluation"]["status"], "blocked")
        with self.assertRaises(ControlStoreError):
            self.store.promote_candidate()

    def test_restarting_with_the_same_candidate_preserves_parent_guidance(self) -> None:
        guidance = "Keep the strict tutor's recommendation focus consistent with the active release."
        self.store.update_candidate_model_adapter("tutor_abhishek", guidance)
        restaged = self.store.stage_model_candidate("gpt-6-luna")
        self.assertEqual(
            restaged["model_adapters"]["gpt-6-luna"]["tutor_abhishek"],
            guidance,
        )


class ParentGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ControlStore(Path(self.temp_dir.name) / "state.json", ROOT)
        self.provider = FakeAzureProvider()
        app = create_parent_app(
            project_root=ROOT,
            store=self.store,
            provider=self.provider,
            admin_token="admin-test-token",
            gateway_token="gateway-test-token",
            judge_models=["judge-stable-a", "judge-stable-b"],
        )
        # Keep the gateway tests independent of local development credential
        # hashes in config/applications.json. The test app supplies its own
        # explicit shared gateway token above.
        registry_data = json.loads((ROOT / "config" / "applications.json").read_text(encoding="utf-8"))
        for application in registry_data["applications"]:
            application["gateway_token_sha256"] = ""
        registry_path = Path(self.temp_dir.name) / "applications.json"
        registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
        app.config["APPLICATION_REGISTRY"] = ApplicationRegistry(registry_path)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_gateway_requires_auth_and_reports_active_model(self) -> None:
        self.assertEqual(self.client.get("/api/v1/status").status_code, 401)
        response = self.client.get(
            "/api/v1/status", headers={"Authorization": "Bearer gateway-test-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["model"], "gpt-5-mini")
        self.assertEqual(response.json["provider"], "Azure OpenAI")

    def test_manual_comparison_uses_same_context_without_sending_it_to_judge(self) -> None:
        self.store.update_candidate_model("gpt-5-nano")
        records = [{
            "student_id": "IND-035",
            "name": "Yash Thakur",
            "assessment_history": [{
                "subject": "Mathematics",
                "assessment": "Fractions Quiz",
                "score": 58,
                "out_of": 100,
                "assessed_on": "2026-10-10",
            }],
            "areas_for_improvement": ["submit homework consistently", "check answers before submitting"],
        }]
        generated = [
            {
                "answer": "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Next step: check answers before submitting.",
                "first_answer": "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Next step: check answers before submitting.",
                "first_issues": [], "issues": [], "repair_attempts": 0, "repair_history": [],
            },
            {
                "answer": "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Next step: submit homework consistently.",
                "first_answer": "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Next step: submit homework consistently.",
                "first_issues": [], "issues": [], "repair_attempts": 0, "repair_history": [],
            },
        ]
        with patch("model_migration.parent_app.generate_tutor_answer_diagnostic", side_effect=generated) as generate:
            response = self.client.post(
                "/api/control/compare",
                headers={"Authorization": "Bearer admin-test-token"},
                json={
                    "application_id": "tutor_abhishek",
                    "question": "I am Yash Thakur (IND-035). Explain my assessment history and give one next step.",
                    "student_records": records,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["behavior_review"]["status"], "human_review_only")
        self.assertIn("did not send request context to an LLM judge", response.json["behavior_review"]["summary"])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(generate.call_args_list[0].kwargs["records"], generate.call_args_list[1].kwargs["records"])
        active_instructions = generate.call_args_list[0].kwargs["instructions"]
        candidate_instructions = generate.call_args_list[1].kwargs["instructions"]
        self.assertIn("CLIENT APPLICATION PROMPT", active_instructions)
        self.assertIn("CLIENT APPLICATION PROMPT", candidate_instructions)
        self.assertIn("PARENT RESPONSE CONTRACT", active_instructions)
        self.assertIn("PARENT RESPONSE CONTRACT", candidate_instructions)
        self.assertNotIn("PARENT-OWNED COMPATIBILITY GUIDANCE", active_instructions)
        self.assertIn("PARENT-OWNED COMPATIBILITY GUIDANCE", candidate_instructions)

    def test_tutor_request_uses_active_azure_release_and_selected_mongo_data(self) -> None:
        headers = {"Authorization": "Bearer gateway-test-token"}
        response = self.client.post(
            "/api/v1/respond",
            headers=headers,
            json={
                "tutor_id": "tutor_vicky",
                "question": "Tell me about QA student QA-303.",
                "student_records": [{"student_id": "QA-303", "name": "Rohan Iyer", "average_marks": 82}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["mode"], "Azure OpenAI | gpt-5-mini")
        self.assertEqual(response.json["answer"], "Grounded tutor response from the selected student record.")
        self.assertEqual(self.provider.calls[0]["model"], "gpt-5-mini")
        self.assertIn("Tutor Vicky", self.provider.calls[0]["instructions"])
        self.assertIn("Keep every score tied", self.provider.calls[0]["instructions"])
        self.assertIn("Rohan Iyer", self.provider.calls[0]["input_text"])

    def test_parent_outage_does_not_fall_back_to_local_model(self) -> None:
        persona = {"id": "tutor_vicky", "system_prompt": "Tutor Vicky persona."}
        with patch.dict(os.environ, {
            "MODEL_PARENT_URL": "http://parent.invalid",
            "MODEL_GATEWAY_TOKEN": "gateway-test-token",
        }), patch("shared.tutor_runtime.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")), \
                patch("shared.tutor_runtime.model_configuration", create=True) as local_provider:
            with self.assertRaises(TutorProviderError):
                answer_question(persona=persona, question="Check this record.", student_records=[{"name": "Test"}])
            local_provider.assert_not_called()

    def test_missing_parent_gateway_does_not_use_azure_credentials_directly(self) -> None:
        persona = {"id": "tutor_vicky", "system_prompt": "Tutor Vicky persona."}
        with patch.dict(os.environ, {"MODEL_PARENT_URL": ""}, clear=True):
            with patch("shared.tutor_runtime.urllib.request.urlopen") as network_call:
                with self.assertRaisesRegex(TutorProviderError, "MODEL_PARENT_URL is required"):
                    answer_question(persona=persona, question="Check this record.", student_records=[{"name": "Test"}])
                network_call.assert_not_called()

    def test_admin_can_read_state_but_unauthorized_cannot(self) -> None:
        self.assertEqual(self.client.get("/api/control/state").status_code, 401)
        response = self.client.get(
            "/api/control/state", headers={"Authorization": "Bearer admin-test-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("tutor_siddhant", response.json["candidate"]["personas"])

    def test_compatibility_evaluation_runs_in_background_and_requires_current_suite(self) -> None:
        self.store.update_candidate_model("gpt-5-nano")
        headers = {"Authorization": "Bearer admin-test-token"}
        def fake_check(**kwargs: Any) -> dict[str, Any]:
            return {
                "status": "passed",
                "summary": "All cases passed.",
                "cases": [],
                "judge_models": kwargs["judge_models"],
                "suite_stage": kwargs["suite_stage"],
                "case_counts": {"core": 6, "holdout": 6 if kwargs["suite_stage"] == "final" else 0},
            }
        with patch(
            "model_migration.parent_app.run_compatibility_check",
            side_effect=fake_check,
        ):
            response = self.client.post("/api/control/evaluate", headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["status"], "running")
            result = None
            for _ in range(100):
                result = self.client.get("/api/control/state", headers=headers).json["last_evaluation"]
                if result["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(result["status"], "passed")
            self.assertFalse(result["promotion_allowed"])

            response = self.client.post(
                "/api/control/evaluate", headers=headers, json={"suite_stage": "final"}
            )
            self.assertEqual(response.status_code, 202)
            for _ in range(100):
                result = self.client.get("/api/control/state", headers=headers).json["last_evaluation"]
                if result["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["promotion_allowed"])

    def test_promote_api_rejects_a_pass_from_another_suite(self) -> None:
        self.store.update_candidate_model("gpt-5-nano")
        self.store.save_evaluation(
            {"status": "passed", "summary": "old pass", "cases": [], "suite_fingerprint": "old-suite"},
            evaluated_fingerprint=self.store.candidate_fingerprint(),
            evaluated_active_release_id=self.store.get_active_release()["release_id"],
        )
        response = self.client.post(
            "/api/control/promote",
            headers={"Authorization": "Bearer admin-test-token"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get_active_release()["model"], "gpt-5-mini")


class SuiteFreshnessTests(unittest.TestCase):
    def test_suite_edits_make_saved_pass_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases_path = root / "behavior_regression_cases.json"
            apps_path = root / "applications.json"
            cases_path.write_text('{"cases":[1]}', encoding="utf-8")
            apps_path.write_text('{"applications":[1]}', encoding="utf-8")
            state = {"last_evaluation": {
                "status": "passed",
                "promotion_allowed": True,
                "suite_fingerprint": compatibility_suite_fingerprint(cases_path, apps_path),
            }}
            project_root = root / "project"
            config_dir = project_root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "behavior_regression_cases.json").write_text('{"cases":[1]}', encoding="utf-8")
            (config_dir / "applications.json").write_text('{"applications":[1]}', encoding="utf-8")
            self.assertTrue(_apply_suite_freshness(state, project_root)["last_evaluation"]["promotion_allowed"])
            (config_dir / "behavior_regression_cases.json").write_text('{"cases":[2]}', encoding="utf-8")
            stale = _apply_suite_freshness(state, project_root)["last_evaluation"]
            self.assertEqual(stale["status"], "stale")
            self.assertFalse(stale["promotion_allowed"])


class DeterministicGateTests(unittest.TestCase):
    def test_missing_recorded_fact_or_score_fails_gate(self) -> None:
        case = {
            "required_facts": ["QA student", "2026-09-18"],
            "expected_score_pairs": [[81, 100]],
        }
        result = _deterministic_check(
            "QA student scored 81/100 on 2026-09-18. " + "Clear explanation. " * 22,
            case,
        )
        self.assertTrue(result["passed"])
        failed = _deterministic_check("QA student scored well.", case)
        self.assertFalse(failed["passed"])
        self.assertTrue(failed["missing_score_pairs"])

    def test_response_format_and_invented_schedule_are_gated(self) -> None:
        case = {
            "required_facts": [],
            "expected_score_pairs": [],
            "max_paragraphs": 2,
            "forbid_bullets": True,
            "forbid_invented_schedule": True,
        }
        answer = (
            "Here is a factual summary with enough clear words to exceed the minimum. " * 3
            + "\n\nDo one practical action.\n\nA third paragraph should fail this contract.\n"
            "- Spend 10 minutes each day making flashcards."
        )
        result = _deterministic_check(answer, case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["paragraph_count"], 3)
        self.assertTrue(result["has_bullets"])
        self.assertTrue(result["has_invented_schedule"])

    def test_latest_only_and_single_improvement_rules_are_detected(self) -> None:
        question = "Tell me my latest assessment and one area I should improve."
        records = [{
            "assessment_history": [
                {"assessment": "Fractions Quiz", "assessed_on": "2026-10-10", "score": 78, "out_of": 100},
                {"assessment": "Reading Check", "assessed_on": "2026-10-01", "score": 75, "out_of": 100},
            ],
            "areas_for_improvement": ["review key vocabulary", "submit homework consistently"],
        }]
        answer = (
            "The latest assessment was Fractions Quiz on 2026-10-10, with 78/100.\n\n"
            "Your previous result was Reading Check on 2026-10-01, 75/100. "
            "Recorded areas: review key vocabulary and submit homework consistently."
        )
        issues = response_contract_issues(question=question, records=records, answer=answer)
        self.assertTrue(any("omit older assessments" in issue for issue in issues))
        self.assertTrue(any("only one recorded improvement" in issue for issue in issues))


class RepeatedEvaluationTests(unittest.TestCase):
    class PairwiseProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def complete(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            if kwargs.get("json_mode"):
                baseline_score = {"factual_accuracy": 5, "persona_fidelity": 5, "contract_compliance": 5, "clarity": 5}
                candidate_score = {"factual_accuracy": 5, "persona_fidelity": 4, "contract_compliance": 4, "clarity": 5}
                answer_a = kwargs["input_text"]
                payload = json.loads(answer_a)
                candidate_in_a = "submit homework consistently" in payload["answer_a"][0]
                return json.dumps({
                    "answer_a": [candidate_score if candidate_in_a else baseline_score] * 3,
                    "answer_b": [baseline_score if candidate_in_a else candidate_score] * 3,
                    "issues_a": ["The selected recommendation changed."] if candidate_in_a else [],
                    "issues_b": [] if candidate_in_a else ["The selected recommendation changed."],
                    "summary": "The candidate changed the next step.",
                })
            if kwargs["model"] == "gpt-5-mini":
                return "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Recorded area: check answers before submitting. Recommendation: check answers before submitting."
            return "Yash Thakur: Mathematics, Fractions Quiz, 2026-10-10, 58/100. Recorded area: submit homework consistently. Recommendation: submit homework consistently."

    def test_repeated_suite_blocks_consistent_recommendation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cases_path = Path(temp_dir) / "behavior_regression_cases.json"
            cases_path.write_text(json.dumps({"cases": [{
                "id": "recommendation-choice",
                "tutor_id": "tutor_abhishek",
                "question": "I am Yash Thakur. Explain my assessment and give one next step.",
                "student_records": [{
                    "student_id": "IND-035",
                    "name": "Yash Thakur",
                    "assessment_history": [{
                        "subject": "Mathematics",
                        "assessment": "Fractions Quiz",
                        "score": 58,
                        "out_of": 100,
                        "assessed_on": "2026-10-10",
                    }],
                    "areas_for_improvement": ["check answers before submitting", "submit homework consistently"],
                }],
                "required_facts": ["Fractions Quiz", "2026-10-10"],
                "expected_score_pairs": [[58, 100]],
                "min_words": 0,
                "max_paragraphs": 2,
            }]}), encoding="utf-8")
            active = ControlStore(Path(temp_dir) / "state.json", ROOT).get_active_release()
            candidate = {**active, "model": "gpt-5-nano"}
            candidate["model_adapters"] = {"gpt-5-nano": {
                "tutor_abhishek": "Choose one exact recorded improvement, preserving the active model's selection behavior."
            }}
            provider = self.PairwiseProvider()
            result = run_compatibility_check(
                active_release=active,
                candidate=candidate,
                provider=provider,  # type: ignore[arg-type]
                cases_path=cases_path,
                judge_models=["judge-stable-a", "judge-stable-b"],
                repetitions=3,
            )
        self.assertEqual(result["status"], "failed")
        case_result = result["cases"][0]
        self.assertEqual(case_result["baseline_check"]["run_count"], 3)
        self.assertEqual(case_result["candidate_check"]["run_count"], 3)
        self.assertEqual(len(provider.calls), 10)
        self.assertEqual(
            [call["model"] for call in provider.calls if call.get("json_mode")],
            ["judge-stable-a", "judge-stable-a", "judge-stable-b", "judge-stable-b"],
        )
        judge_inputs = [json.loads(call["input_text"]) for call in provider.calls if call.get("json_mode")]
        self.assertEqual(judge_inputs[0]["answer_a"], judge_inputs[1]["answer_b"])
        self.assertEqual(judge_inputs[0]["answer_b"], judge_inputs[1]["answer_a"])
        self.assertNotIn("baseline_answers", judge_inputs[0])
        self.assertNotIn("candidate_answers", judge_inputs[0])
        self.assertTrue(any("The selected recommendation changed" in issue for issue in case_result["judge_issues"]))
        self.assertEqual(len(case_result["judges"]), 2)
        self.assertEqual(len(case_result["judge_order_records"][0]["orders"]), 2)
        self.assertFalse(case_result["all_judges_passed"])

    def test_compatibility_evaluation_blocks_without_a_judge(self) -> None:
        provider = self.PairwiseProvider()
        with tempfile.TemporaryDirectory() as temp_dir:
            active = ControlStore(Path(temp_dir) / "state.json", ROOT).get_active_release()
            candidate = {**active, "model": "gpt-5-nano"}
            result = run_compatibility_check(
                active_release=active,
                candidate=candidate,
                provider=provider,  # type: ignore[arg-type]
                cases_path=ROOT / "config" / "behavior_regression_cases.json",
                judge_models=[],
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("one to three", result["summary"])
        self.assertEqual(provider.calls, [])


class ResponseRepairTests(unittest.TestCase):
    class SequenceProvider:
        def __init__(self, answers: list[str]) -> None:
            self.answers = iter(answers)
            self.calls: list[dict[str, Any]] = []

        def complete(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            return next(self.answers)

    def test_same_model_repairs_format_and_unsupported_advice_once(self) -> None:
        provider = self.SequenceProvider([
            "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n- Study for 10 minutes each day.",
            "Your recorded improvement is review vocabulary.\n\nRecommendation: review vocabulary.",
        ])
        answer = generate_tutor_answer(
            provider=provider,  # type: ignore[arg-type]
            model="gpt-5-mini",
            instructions="Stable tutor and shared behavior instructions.",
            question="Give me one next step.",
            records=[{"areas_for_improvement": ["review vocabulary"]}],
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([call["model"] for call in provider.calls], ["gpt-5-mini", "gpt-5-mini"])
        self.assertEqual(answer, "Your recorded improvement is review vocabulary.\n\nRecommendation: review vocabulary.")

    def test_invalid_answer_is_not_returned_after_two_repairs(self) -> None:
        invalid = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n- Study for 10 minutes each day."
        provider = self.SequenceProvider([invalid, invalid, invalid])
        with self.assertRaises(AzureResponsesError):
            generate_tutor_answer(
                provider=provider,  # type: ignore[arg-type]
                model="gpt-5-mini",
                instructions="Stable tutor and shared behavior instructions.",
                question="Give me one next step.",
                records=[{"areas_for_improvement": ["review vocabulary"]}],
            )
        self.assertEqual(len(provider.calls), 3)


if __name__ == "__main__":
    unittest.main()

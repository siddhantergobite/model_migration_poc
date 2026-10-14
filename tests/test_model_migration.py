from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

from model_migration.azure_responses import AzureResponsesError
from model_migration.control_store import ControlStore, ControlStoreError
from model_migration.evaluation import _deterministic_check
from model_migration.generation import generate_tutor_answer
from model_migration.parent_app import create_parent_app
from model_migration.response_contract import response_contract_issues
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
            {"status": "passed", "summary": "old evaluation", "cases": []},
            evaluated_fingerprint=fingerprint,
            evaluated_active_release_id=active_id,
        )
        with self.assertRaises(ControlStoreError):
            self.store.promote_candidate()

        fingerprint = self.store.candidate_fingerprint()
        self.store.save_evaluation(
            {"status": "passed", "summary": "all checks passed", "cases": []},
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
        )
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
            "Your recorded improvement is key vocabulary.\n\nRecommendation: review the vocabulary area recorded for you.",
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
        self.assertEqual(answer, "Your recorded improvement is key vocabulary.\n\nRecommendation: review the vocabulary area recorded for you.")

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

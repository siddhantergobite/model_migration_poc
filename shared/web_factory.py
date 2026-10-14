"""Create one independently configured Flask app for each tutor persona."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request
from pymongo.errors import PyMongoError

from shared.config import load_env_file
from shared.database import (
    class_report,
    collection_count,
    find_students_mentioned,
    get_collection,
    ping_database,
)
from shared.tutor_runtime import (
    TutorProviderError,
    active_model_status,
    answer_question,
)


ROOT = Path(__file__).resolve().parent.parent
load_env_file(ROOT / ".env")


def read_persona(tutor_dir: Path) -> dict[str, Any]:
    config = json.loads((tutor_dir / "tutor.json").read_text(encoding="utf-8"))
    config["system_prompt"] = (tutor_dir / "system_prompt.txt").read_text(encoding="utf-8").strip()
    config["id"] = tutor_dir.name
    config["port"] = int(config.get("port", 5001))
    return config


def _class_question(question: str, has_student_match: bool) -> bool:
    if has_student_match:
        return False
    phrases = (
        "class-wide", "class wide", "whole class", "whole group", "class average",
        "class overall", "class snapshot", "for the class", "all students",
        "student list", "roster", "compare students",
    )
    words = set(re.findall(r"[a-z]+", question.casefold()))
    return (
        any(phrase in question for phrase in phrases)
        or "who has" in question
        or {"class", "average"}.issubset(words)
        or bool(words & {"best", "highest", "lowest", "most", "top"})
    )


def _rank_request(question: str) -> tuple[str | None, str | None]:
    words = set(re.findall(r"[a-z]+", question.casefold()))
    direction = None
    if words & {"highest", "top", "best", "most"} or "who has" in question:
        direction = "highest"
    elif words & {"lowest", "least"}:
        direction = "lowest"
    if direction is None:
        return None, None
    if "attendance" in question or "attend" in words:
        return "attendance_percentage", direction
    if "homework" in words:
        return "homework_completion_percentage", direction
    return "average_marks", direction


def _student_context(student: dict[str, Any], question: str) -> dict[str, Any]:
    """Send the model only MongoDB fields relevant to the student's question."""
    words = set(re.findall(r"[a-z]+", question.casefold()))
    selected: set[str] = set()

    if words & {"assessment", "assessments", "exam", "exams", "test", "tests", "history", "result", "results"}:
        selected.add("assessment_history")
    if words & {"improve", "improvement", "improvements", "better", "weakness", "weaknesses", "suggest", "suggestion", "recommend", "recommendation", "practice", "next"}:
        selected.add("areas_for_improvement")
    if "average" in words:
        selected.add("average_marks")
    if words & {"attendance", "attend"}:
        selected.add("attendance_percentage")
    if "homework" in words:
        selected.add("homework_completion_percentage")
    if words & {"conduct", "behavior", "behaviour", "participation"}:
        selected.update({"class_conduct", "participation_level"})
    if words & {"strength", "strengths"}:
        selected.add("strengths")
    if words & {"learning", "style"}:
        selected.add("learning_style")
    if words & {"city", "state", "location", "where"}:
        selected.update({"city", "state"})
    if words & {"grade", "section", "school", "term"}:
        selected.update({"grade", "section", "school_name", "term"})
    if words & {"note", "notes", "teacher"}:
        selected.add("teacher_notes")

    if not selected:
        return student

    context = {
        field: student[field]
        for field in ("name", "student_id", *sorted(selected))
        if field in student
    }
    return context


def create_tutor_app(tutor_dir: Path) -> tuple[Flask, dict[str, Any]]:
    tutor_dir = tutor_dir.resolve()
    persona = read_persona(tutor_dir)
    app = Flask(
        __name__,
        template_folder=str(tutor_dir / "templates"),
        static_folder=str(tutor_dir / "static"),
        static_url_path="/static",
    )

    @app.get("/")
    def home():
        return render_template("index.html", tutor=persona)

    @app.get("/api/config")
    def api_config():
        public_fields = ("name", "short_name", "symbol", "role", "style", "theme", "tagline", "port")
        return jsonify(
            {
                "tutor": {field: persona[field] for field in public_fields},
            }
        )

    @app.get("/api/health")
    def api_health():
        try:
            ping_database()
            count = collection_count()
            collection = get_collection()
            try:
                model_status = active_model_status(persona["id"])
            except TutorProviderError as error:
                model_status = {"configured": False, "error": str(error)}
            return jsonify(
                {
                    "ok": True,
                    "database": "connected",
                    "database_name": collection.database.name,
                    "collection_name": collection.name,
                    "student_count": count,
                    "model": model_status,
                    "prompt_sha256": hashlib.sha256(persona["system_prompt"].encode("utf-8")).hexdigest(),
                }
            )
        except PyMongoError:
            return jsonify(
                {
                    "ok": False,
                    "database": "unavailable",
                    "error": "MongoDB is unavailable. Check the MongoDB service and MONGODB_URI.",
                }
            ), 503

    @app.post("/api/ask")
    def api_ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Send a JSON object with a question."}), 400
        question = payload.get("question", "")
        if not isinstance(question, str) or not question.strip():
            return jsonify({"error": "Write a question first."}), 400
        question = question.strip()
        if len(question) > 4000:
            return jsonify({"error": "Questions must be 4,000 characters or fewer."}), 400

        try:
            lowered_question = question.casefold()
            matched_students = find_students_mentioned(question)
            class_question = _class_question(lowered_question, bool(matched_students))
            if class_question:
                metric, direction = _rank_request(lowered_question)
                report = class_report(metric=metric, direction=direction)
                records = [{"record_type": "class_report", **report}]
            else:
                if len(matched_students) > 1:
                    matching_names = ", ".join(item["name"] for item in matched_students)
                    return jsonify(
                        {"error": f"More than one student matched ({matching_names}). Include a full name or student ID."}
                    ), 409
                if not matched_students:
                    return jsonify(
                        {"error": "I couldn't match a student. Include their full name or student ID."}
                    ), 400
                records = [_student_context(matched_students[0], lowered_question)]
            if not records:
                return jsonify(
                    {"error": "Include a student's full name or student ID."}
                ), 400
            answer, mode = answer_question(
                persona=persona,
                question=question,
                student_records=records,
            )
            return jsonify({"answer": answer, "mode": mode, "record_count": len(records)})
        except TutorProviderError as error:
            return jsonify({"error": str(error)}), 502
        except PyMongoError:
            return jsonify(
                {"error": "MongoDB is unavailable. Check the MongoDB service and MONGODB_URI."}
            ), 503

    return app, persona

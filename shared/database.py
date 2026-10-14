"""MongoDB access for the shared student roster."""

from __future__ import annotations

import os
import re
from typing import Any

from pymongo import MongoClient


_client: MongoClient | None = None


def get_collection():
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
        _client = MongoClient(uri, serverSelectionTimeoutMS=2500, connectTimeoutMS=2500)
    database_name = os.getenv("TUTOR_DB_NAME", "local")
    collection_name = os.getenv("TUTOR_COLLECTION", "Student_Data")
    return _client[database_name][collection_name]


def ping_database() -> bool:
    get_collection().database.client.admin.command("ping")
    return True


def list_students(search: str = "") -> list[dict[str, Any]]:
    collection = get_collection()
    query: dict[str, Any] = {}
    needle = search.strip()
    if needle:
        safe_needle = re.escape(needle)
        query = {
            "$or": [
                {"name": {"$regex": safe_needle, "$options": "i"}},
                {"student_id": {"$regex": safe_needle, "$options": "i"}},
                {"city": {"$regex": safe_needle, "$options": "i"}},
                {"state": {"$regex": safe_needle, "$options": "i"}},
            ]
        }
    projection = {"_id": 0}
    return list(collection.find(query, projection).sort("student_id", 1))


def get_student(student_id: str) -> dict[str, Any] | None:
    return get_collection().find_one({"student_id": student_id}, {"_id": 0})


def collection_count() -> int:
    return get_collection().count_documents({})


def find_students_mentioned(question: str, limit: int = 3) -> list[dict[str, Any]]:
    """Find student names or IDs inside a question using MongoDB text fields."""
    collection = get_collection()
    tokens = re.findall(r"[A-Za-z0-9-]+", question)
    ignored = {
        "about", "academic", "am", "and", "are", "assessment", "assessments", "attendance",
        "average", "class", "conduct", "could", "did", "doing", "exam", "exams", "give",
        "grade", "has", "have", "help", "highest", "homework", "how", "improve", "in", "is",
        "last", "least", "let", "list", "looking", "lowest", "mark", "marks", "me", "most",
        "of", "overall", "participation", "perform", "performed", "please", "progress", "record",
        "report", "result", "results", "recent", "scores", "show", "sir", "student", "students",
        "strength", "strengths", "summary", "tell", "the", "their", "this", "top", "what", "which",
        "who", "with",
    }
    found: dict[str, dict[str, Any]] = {}
    for token in tokens:
        if len(token) < 3 or token.casefold() in ignored:
            continue
        pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
        query = {
            "$or": [
                {"name": {"$regex": pattern, "$options": "i"}},
                {"student_id": {"$regex": pattern, "$options": "i"}},
            ]
        }
        for student in collection.find(query, {"_id": 0}).limit(limit):
            found[student["student_id"]] = student
            if len(found) >= limit:
                return list(found.values())
    return list(found.values())


def class_report(
    *, metric: str | None = None, direction: str | None = None
) -> dict[str, Any]:
    """Calculate class metrics in MongoDB and optionally attach a ranked student."""
    collection = get_collection()
    pipeline = [
        {
            "$group": {
                "_id": None,
                "student_count": {"$sum": 1},
                "average_marks": {"$avg": "$average_marks"},
                "average_attendance": {"$avg": "$attendance_percentage"},
                "average_homework_completion": {"$avg": "$homework_completion_percentage"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "student_count": 1,
                "average_marks": {"$round": ["$average_marks", 1]},
                "average_attendance": {"$round": ["$average_attendance", 1]},
                "average_homework_completion": {"$round": ["$average_homework_completion", 1]},
            }
        },
    ]
    report = next(collection.aggregate(pipeline), {"student_count": 0})
    if metric in {"average_marks", "attendance_percentage", "homework_completion_percentage"}:
        sort_direction = -1 if direction == "highest" else 1
        projection = {
            "_id": 0,
            "student_id": 1,
            "name": 1,
            "average_marks": 1,
            "attendance_percentage": 1,
            "homework_completion_percentage": 1,
        }
        ranked = collection.find({}, projection).sort(metric, sort_direction).limit(1)
        report["ranked_student"] = next(ranked, None)
        report["ranking_metric"] = metric
        report["ranking_direction"] = direction
    return report

"""Create 50 fictional Indian student records in the V1 MongoDB collection."""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, UpdateOne

from shared.config import load_env_file


ROOT = __import__("pathlib").Path(__file__).resolve().parent
load_env_file(ROOT / ".env")

NAMES = [
    "Aarav Sharma", "Aanya Iyer", "Aditya Patel", "Ananya Reddy", "Arjun Nair",
    "Diya Mehta", "Ishaan Gupta", "Kavya Menon", "Krishna Rao", "Meera Joshi",
    "Neha Kulkarni", "Rohan Das", "Saanvi Kapoor", "Siddharth Verma", "Tanvi Shah",
    "Vihaan Singh", "Anika Bose", "Dev Malhotra", "Esha Banerjee", "Farhan Khan",
    "Gauri Deshpande", "Harsh Venkatesh", "Isha Chawla", "Kabir Sethi", "Lakshmi Krishnan",
    "Manav Bhatia", "Nandini Pillai", "Om Prakash", "Parth Trivedi", "Riya Mukherjee",
    "Samar Chatterjee", "Tara Shetty", "Uday Yadav", "Veda Srinivasan", "Yash Thakur",
    "Zoya Qureshi", "Akash Ghosh", "Bhavna Patil", "Chirag Jain", "Divya Sinha",
    "Eshan Aggarwal", "Fatima Ansari", "Ganesh Naidu", "Hema Ranganathan", "Jatin Arora",
    "Karthik Subramanian", "Leela Naik", "Mohit Saxena", "Pooja Nambiar", "Rahul Choudhary",
]

LOCATIONS = [
    ("Mumbai", "Maharashtra"), ("Chennai", "Tamil Nadu"), ("Ahmedabad", "Gujarat"),
    ("Hyderabad", "Telangana"), ("Kochi", "Kerala"), ("Pune", "Maharashtra"),
    ("New Delhi", "Delhi"), ("Kolkata", "West Bengal"), ("Bengaluru", "Karnataka"),
    ("Lucknow", "Uttar Pradesh"), ("Jaipur", "Rajasthan"), ("Bhopal", "Madhya Pradesh"),
    ("Patna", "Bihar"), ("Indore", "Madhya Pradesh"), ("Surat", "Gujarat"),
    ("Visakhapatnam", "Andhra Pradesh"), ("Mysuru", "Karnataka"), ("Coimbatore", "Tamil Nadu"),
    ("Nagpur", "Maharashtra"), ("Chandigarh", "Chandigarh"), ("Guwahati", "Assam"),
    ("Dehradun", "Uttarakhand"), ("Ranchi", "Jharkhand"), ("Raipur", "Chhattisgarh"),
    ("Thiruvananthapuram", "Kerala"),
]

CONDUCT_NOTES = [
    "Respectful with classmates and follows classroom instructions.",
    "Participates willingly and listens carefully when others speak.",
    "Works well with peers and is learning to stay focused during independent work.",
    "Usually attentive; benefits from occasional reminders to wait for a turn.",
    "Shows a positive attitude and takes care of shared classroom materials.",
    "Collaborates kindly and asks for help when a task is unclear.",
]

STRENGTHS = [
    "explains ideas clearly", "works well in groups", "shows curiosity", "checks work carefully",
    "persists with challenging tasks", "shares thoughtful questions", "organizes notes well",
    "connects lessons to examples", "supports classmates", "learns new concepts quickly",
]

IMPROVEMENTS = [
    "show each calculation step", "review key vocabulary", "check answers before submitting",
    "practice fraction problems", "read each question twice", "revise weekly notes",
    "explain reasoning in complete sentences", "submit homework consistently",
    "practice multiplication facts", "ask questions sooner when stuck",
]

LEARNING_STYLES = ["visual examples", "guided practice", "discussion", "written steps", "hands-on activities"]
PARTICIPATION = ["Low", "Moderate", "High"]
SUBJECTS = ["Mathematics", "Science", "English", "Social Studies", "Computer Studies"]


def make_records() -> list[dict]:
    rng = random.Random(20261013)
    now = datetime.now(timezone.utc)
    updated_at = now.isoformat(timespec="seconds")
    records = []
    for index, name in enumerate(NAMES, start=1):
        location = LOCATIONS[(index - 1) % len(LOCATIONS)]
        grade = rng.choice([6, 7, 8, 9, 10])
        marks = {subject: rng.randint(48, 99) for subject in SUBJECTS}
        average = round(sum(marks.values()) / len(marks), 1)
        math_score = marks["Mathematics"]
        assessment_history = [
            {
                "subject": "Mathematics",
                "assessment": rng.choice(["Fractions Quiz", "Unit Review", "Problem-Solving Check"]),
                "score": math_score,
                "out_of": 100,
                "assessed_on": (now.date() - timedelta(days=3)).isoformat(),
            },
            {
                "subject": "Science",
                "assessment": "Science Unit Review",
                "score": marks["Science"],
                "out_of": 100,
                "assessed_on": (now.date() - timedelta(days=17)).isoformat(),
            },
            {
                "subject": "English",
                "assessment": "Reading Comprehension Check",
                "score": marks["English"],
                "out_of": 100,
                "assessed_on": (now.date() - timedelta(days=31)).isoformat(),
            },
        ]
        records.append(
            {
                "student_id": f"IND-{index:03d}",
                "name": name,
                "age": grade + rng.choice([5, 6]),
                "grade": grade,
                "section": rng.choice(["A", "B", "C"]),
                "school_name": rng.choice(
                    ["Vidya Niketan School", "Pragati Public School", "Sarvodaya Learning Academy"]
                ),
                "city": location[0],
                "state": location[1],
                "attendance_percentage": round(rng.uniform(72.0, 100.0), 1),
                "marks": marks,
                "average_marks": average,
                "latest_assessment": assessment_history[0],
                "assessment_history": assessment_history,
                "homework_completion_percentage": round(rng.uniform(62.0, 100.0), 1),
                "participation_level": rng.choice(PARTICIPATION),
                "class_conduct": rng.choice(CONDUCT_NOTES),
                "strengths": rng.sample(STRENGTHS, k=2),
                "areas_for_improvement": rng.sample(IMPROVEMENTS, k=2),
                "learning_style": rng.choice(LEARNING_STYLES),
                "teacher_notes": rng.choice(
                    [
                        "Responds well to clear examples and short practice sets.",
                        "Has made steady progress this term; encourage regular revision.",
                        "Benefits from explaining the approach aloud before writing.",
                        "Shows confidence in familiar topics and is building confidence in new ones.",
                        "A consistent study routine may help consolidate recent lessons.",
                    ]
                ),
                "term": "Term 1, 2026-27",
                "sample_record": True,
                "updated_at": updated_at,
            }
        )
    return records


def main() -> None:
    uri = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    database_name = os.getenv("TUTOR_DB_NAME", "local")
    collection_name = os.getenv("TUTOR_COLLECTION", "Student_Data")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    collection = client[database_name][collection_name]
    collection.create_index("student_id", unique=True)
    operations = [
        UpdateOne({"student_id": record["student_id"]}, {"$set": record}, upsert=True)
        for record in make_records()
    ]
    result = collection.bulk_write(operations, ordered=False)
    count = collection.count_documents({})
    print("MongoDB connection: successful")
    print(f"Database: {database_name} | collection: {collection_name}")
    print(f"Seeded 50 fictional Indian student records (total in collection: {count}).")
    print(f"Collections in this database: {', '.join(client[database_name].list_collection_names())}")
    print(f"Upserted: {result.upserted_count} | updated: {result.modified_count}")
    client.close()


if __name__ == "__main__":
    main()

# V1: Three MongoDB-backed student tutors

Each tutor runs as an independent local web app with its own system prompt. All
three apps read the same MongoDB collection: `local.Student_Data`.

## Start MongoDB and load the sample students

MongoDB Community Server should be running at `mongodb://127.0.0.1:27017`. From this
project folder, install the requirements and load the 50 fictional sample students:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\seed_students.py
```

The seed script upserts by `student_id` into `local.Student_Data`. In MongoDB Compass,
refresh `localhost:27017`, expand the `local` database, then open `Student_Data`.

## Start the tutors

Start the parent gateway first using the command in the V2 section below. Each
tutor requires `MODEL_PARENT_URL` and its app-specific
`MODEL_GATEWAY_TOKEN_TUTOR_*` value; it will not call Azure directly if the
parent is unavailable.

Open each command in a separate PowerShell window:

```powershell
py -3.11 .\tutor_abhishek\app.py
```

```powershell
py -3.11 .\tutor_siddhant\app.py
```

```powershell
py -3.11 .\tutor_vicky\app.py
```

Then open:

- Tutor Abhishek: <http://127.0.0.1:5101>
- Tutor Siddhant: <http://127.0.0.1:5102>
- Tutor Vicky: <http://127.0.0.1:5103>

Ask about a student using their full name or student ID. Each question searches the
MongoDB collection for that student's current document. Class summaries and rankings
are calculated with MongoDB aggregation queries. Each tutor keeps its own
`system_prompt.txt`; the prompt guides tone and judging, while MongoDB supplies the
student facts.

The sample-data script is only a loader. The running apps do not use a hardcoded
student roster. They read student records from MongoDB for each request.

## Azure and parent gateway configuration

The tutor servers require MongoDB and the V2 parent gateway. Set
`MODEL_PARENT_URL` and the matching `MODEL_GATEWAY_TOKEN_TUTOR_*` value for each
tutor service. Tutor apps do not contain an Azure client and cannot call a model
directly. If the parent is unreachable or the token is invalid, they return an
error instead of bypassing the parent.

Only the parent process needs `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`,
and `TUTOR_MODEL`. Use the Azure OpenAI v1 endpoint ending in `/openai/v1`; the
deployment name must exist in that Azure resource. The API key stays on the
parent host and is never returned by application endpoints. Do not commit
`.env`.

The parent loads versioned tutor prompts and a shared response contract. Student
facts still come from MongoDB for each request. The gateway keeps the tutor API
response shape stable and checks answers for the configured fact, persona, and
format requirements. Different models can phrase text differently, so V2 uses
regression examples and a gated promotion instead of promising byte-identical
answers.

You can override the MongoDB URI, database, and collection through `.env` with
`MONGODB_URI`, `TUTOR_DB_NAME`, and `TUTOR_COLLECTION`.

## V2 requirements

V2 is the parent/base application for model upgrades. Its requirement is
recorded in [V2_MODEL_UPGRADE_REQUIREMENTS.md](V2_MODEL_UPGRADE_REQUIREMENTS.md):
switching a model should preserve each downstream app's prompt, persona, grading
rules, and response contract, with compatibility checks before rollout.

## V2 parent application (working local implementation)

The parent application is the Azure model gateway and behavior control plane.
It stores active and candidate releases in `config/model_control.json`. On the
first launch, `TUTOR_MODEL` seeds the active deployment. On later launches, a
different `TUTOR_MODEL` stages and evaluates a candidate. The active release
continues serving until an administrator reviews the result and promotes the
candidate. Failed or unavailable candidates are not promoted. The candidate
starts empty when no model upgrade is staged.

Set `MODEL_PARENT_URL`, `MODEL_ADMIN_TOKEN`, and the Azure endpoint/key in the
parent's `.env`. Each sample tutor has its own `MODEL_GATEWAY_TOKEN_TUTOR_*`
credential. Keep the Azure key on the parent host; tutor services need only the
parent URL and their own gateway credential. The registry is
`config/applications.json`; it records each sample app's prompt fingerprint,
API/tool schema, data integration description, and regression-case coverage.
Set the three `TUTOR_*_HEALTH_URL` values if the tutor services run on other
hosts.

Start the control plane from this folder:

```powershell
py -3.11 -m model_migration.run_parent --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`, choose the account button, and enter
`MODEL_ADMIN_TOKEN`. Start the three tutor services as usual. Their student
facing `/api/ask` contract and data selection remain in the tutor applications;
each sends its selected student context to the parent, which applies that app's
saved prompt and the active model. If the parent is unavailable or rejects the
app credential, the tutor returns an error instead of silently calling a local
model.

### Test a model change

1. Leave `TUTOR_MODEL=gpt-5-mini` and confirm the saved active model is mini.
   Capture baseline examples from the tutor apps.
2. Change `TUTOR_MODEL` to the actual Azure deployment name for the candidate,
   such as `gpt-5-nano`, and restart the parent. A parent restart is sufficient
   for a model-only change; the tutor services keep calling the active gateway.
3. The parent compares the saved active model and candidate against the
   application-specific cases in the background. Mini remains live while the
   dashboard shows the candidate result. Use **Compare tutor behavior** to run
   the same question and student records through both deployments.
4. Review the deterministic fact/score/format checks and the persona/quality
   evaluation. Promote only after the evidence meets the agreed behavior
   requirements. After promotion, new tutor requests use nano. Rollback restores
   the previous release.

The current suite has two synthetic cases for each of the three sample tutors.
It checks facts, score pairs, response rules, persona fidelity, and clarity;
generation also has a one-repair attempt for selected live response-contract
failures. These cases are a POC baseline, not proof for every real student
request. Add representative app-specific cases and have a person review
high-impact answers before treating the gate as a client guarantee. Different
models can phrase answers differently; the promise is behavior and API-contract
preservation within the agreed checks, not identical wording.

The dashboard supports registering an app's prompt snapshot, schemas, data
integration notes, cases, and a one-time app-scoped token. A registered caller
sends its token as a Bearer credential to `POST /api/v1/respond` with
`application_id`, `question`, and `student_records`; the response includes
`answer`, `mode`, `release_id`, `contract_revision`, and `prompt_revision`. The
registry stores a client's API/tool schema, but this
POC does not translate arbitrary client-specific request formats or execute
client tools. A client with a different gateway format needs a parent-side
adapter before it can remain unchanged at that integration boundary. The three
sample tutor apps already use the shared envelope while keeping their own
student-facing endpoints.

After a dashboard rollback, set `TUTOR_MODEL` to the restored deployment before
the next parent restart. Otherwise the newer value in `.env` will stage it again
for evaluation.

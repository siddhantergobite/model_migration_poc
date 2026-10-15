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

## Model compatibility and release control

The parent routes registered applications through Azure and keeps active and
candidate releases in `config/model_control.json`. It compares real requests,
runs core and reserved validation cases, then allows promotion or rollback only
when the current candidate passes. The provider adds a versioned candidate
compatibility layer; client prompts and tutor service code remain unchanged.

Keep `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `MODEL_ADMIN_TOKEN`, and
`MODEL_GATEWAY_TOKEN*` in the parent `.env`. The Azure key stays on the parent.
The active model is Mini in the current local state. A candidate can be staged
by changing the parent's `TUTOR_MODEL` and restarting it, or by entering the
Azure deployment name in the dashboard. Neither path changes a tutor's prompt.
When a changed deployment is staged from `.env`, startup runs a core review only.
The separate final-validation action is still required before promotion.

Start the parent from this project folder:

```powershell
py -3.11 -m model_migration.run_parent --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765` and enter `MODEL_ADMIN_TOKEN` when prompted. The
current setup registers Abhishek, Siddhant, and Vicky in
`config/applications.json`.

### Compare and evaluate

1. Enter the candidate's exact Azure deployment name.
2. Choose Abhishek, Siddhant, or Vicky. Enter the same question used in the
   tutor app, including the student's full name or ID. For these local apps,
   the parent looks up question-relevant fields in their shared MongoDB and
   sends the same context and saved tutor prompt to both models. No student JSON
   needs to be pasted.
3. Review both first drafts and final drafts, their repair histories, the
   record and response-rule checks, and each tutor's persona. A draft that
   still violates the contract is shown for diagnosis; a live tutor request
   withholds it.
4. Shape candidate behavior through the versioned parent guidance for each
   registered application. The client prompt stays unchanged. Use **Run core
   checks** while refining guidance. This evaluates the six core cases and is
   review-only. Once guidance is set, use **Run final validation**. It evaluates
   the core cases plus six reserved validation cases and is the only run that
   can authorize promotion. Both models get three paired generations per case
   by default. Deterministic checks validate recorded facts, recommendation
   anchoring, response shape, and unsupported extras. One to three configured
   Azure deployments judge persona, contract compliance, factual accuracy, and
   clarity; every configured judge must pass each case in two blinded answer
   orders, and the candidate must pass in both. Configure judge
   deployment names in `MODEL_EVAL_JUDGE_MODELS` as a comma-separated list.
   The candidate cannot be part of its own judge panel. The dashboard shows
   first-pass rates, repair counts, judge decisions, and paired responses.
5. Promote only if final validation passes every case and you approve the
   comparison. A failed case, changed suite, or incomplete evaluation blocks
   promotion. Verify
   the active model at `/api/health`. Use rollback to restore the previous
   release.

The core and reserved cases cover representative facts, scores, recommendations,
response shape, and tutor personas. They do not prove behavior for every future
request, and multiple LLM judges cannot guarantee zero drift. Model wording can
differ, so review tutor tone and use case results as the agreed release evidence.
Guidance is stored per candidate deployment and application; editing it
invalidates the earlier evaluation. If final results lead you to change
guidance, refresh the reserved examples before relying on a later final run;
cases already reviewed are no longer independent validation. Use distinct,
stable judge deployments where possible. A single judge is supported when that
is all that is available, but provides less independent evidence than a
multi-model panel. If the judge is also the active model, its evaluation may
share that model's preferences; the dashboard and server log warn about this.
The service requires an explicitly configured judge and does not silently
select a fallback. A manual comparison that resolves a real MongoDB record does
not send that context to an LLM judge. It returns the paired model answers and
deterministic checks for human tone review. Automated judge calls use only the
saved synthetic regression fixtures.

### Langfuse traces

The parent can send privacy-safe request and model-call traces to the local
Langfuse server. Traces contain application/tutor IDs, model and release
metadata, status, and answer length; they omit client prompts, student records,
questions, and answer text. Langfuse is optional: the parent keeps serving if it
is not configured or temporarily unavailable.

The local Langfuse instance is started separately from this repository. If it
is stopped, launch it from its existing deployment folder:

```powershell
Set-Location C:\Users\admin\langfuse
docker compose up -d
```

Open `http://localhost:3000` and use the existing `Model-Migration-Demo`
project, or create a dedicated project if you prefer. Copy that project's
public and secret keys into the parent `.env`, along with:

```dotenv
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_BASE_URL=http://127.0.0.1:3000
LANGFUSE_PUBLIC_KEY=<project-public-key>
LANGFUSE_SECRET_KEY=<project-secret-key>
```

Install the dependencies with `py -3.11 -m pip install -r requirements.txt`,
restart only the parent, and check `/api/health` for
`observability.langfuse.enabled`. Use the admin-authenticated
`/api/observability/langfuse` endpoint to check credentials. After a tutor sends
a request through the parent gateway, open Langfuse's **Traces** view to inspect
the parent request span and nested Azure generation. Keep API keys in `.env`;
never put them in client apps or browser code.

The three local tutors share a MongoDB, which lets the parent resolve a student
name in dashboard comparisons. Other clients must send selected context through
the parent, provide an authorized data connector, or use regression fixtures.
The parent cannot read a client's database by itself. An application currently
calling Azure directly needs a one-time integration with the parent gateway
before the parent can manage its model releases.

After rollback, update the parent's `TUTOR_MODEL` to the restored deployment
before restarting it. Otherwise the newer environment value will stage that
model again.

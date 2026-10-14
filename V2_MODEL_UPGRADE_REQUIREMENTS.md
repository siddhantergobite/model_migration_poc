# V2 model-upgrade requirements

## Goal

Build a parent/base application that lets customer applications move to a newer
foundation model while preserving each application's configured behavior.

## Application model

- The parent application manages foundation-model providers and model selections.
- Each customer application has its own persona character, system prompt, grading
  approach, response rules, and application workflow.
- The customer may configure personas such as strict, playful, friendly, direct, or
  suggestion-focused tutors.

## Upgrade behavior

- A customer should not have to rewrite a prompt, change an integration, or rebuild
  their app just because the parent selects a newer model.
- Keep the persona, assessment rubric, grading policy, and response shape stable as
  the underlying model changes.
- Evaluate a candidate against representative examples for each customer app before
  promoting it, and retain a rollback path.
- Optimize for behavior preservation in V2; cost and latency controls can follow.

## V2 implementation contract

- The parent control plane is the only model gateway and uses the configured
  Azure OpenAI endpoint and credentials. Tutor hosts do not select another
  provider or fall back to a local model.
- Each registered client has an identity, immutable prompt snapshot and hash,
  API/tool schema metadata, data integration scope, app-scoped credential, and
  app-specific regression cases.
- The parent dashboard lists connected apps, their active model, prompt
  fingerprint, candidate/evaluation status, data scope, and test coverage.
- Each request selects the active release at request time. A model, shared
  contract, or persona prompt change is staged in a candidate release and has
  no live effect until the current candidate passes regression checks and an
  administrator promotes it.
- The saved release includes the Azure deployment name, shared behavior
  contract, and each tutor's persona prompt, together as one versioned unit.
- The V1 tutor request and response contract stays stable across model swaps.
- Every tutor has at least one synthetic behavior regression case. Evaluation
  checks recorded facts deterministically and judges factual accuracy, persona,
  contract compliance, and clarity. Failed, blocked, or stale evaluations
  cannot be promoted.
- Promoted releases retain a parent release and an administrator can roll back
  the active pointer.
- A changed `TUTOR_MODEL` stages and evaluates a candidate on parent restart;
  it does not change live traffic until an administrator promotes it.
- Evaluation is evidence for the cases configured in the suite; it does not
  prove identical wording or behavior on every possible prompt. Expand the suite
  before using it as a production approval gate.

The target is consistent observable behavior and the same application contract.
Different models can phrase answers differently, so V2 should define behavior
checks and tolerances instead of promising identical text for every possible input.

## Current POC boundary

The local POC implements the registry and dashboard for the three sample tutor
apps, plus registration with a shared `/api/v1/respond` gateway envelope. It
stores arbitrary apps' API/tool schema metadata but does not translate every
client-specific model API or execute arbitrary client tools. A parent-side
adapter is required for a client with a different request/response interface.
The current six synthetic cases demonstrate the release flow; they are not
enough to make a blanket production guarantee for arbitrary client workloads.

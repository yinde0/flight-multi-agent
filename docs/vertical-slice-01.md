# Vertical slice 01: itinerary PDF to canonical trip

## User-visible behavior

`POST /v1/documents/parse` accepts a PDF plus opaque trip metadata. The API validates
the upload, discovers the document agent through its A2A Agent Card, sends the PDF as
an A2A raw part using JSON-RPC `SendMessage` with the `A2A-Version: 1.0`
header, and returns either:

- `status: parsed` with a canonical itinerary; or
- `status: review_required` with explicit fields the system must not infer.

The API is the only host-exposed service. It joins an edge network and the private
agent network. The document agent has no published port; vertical slice 02 adds a
separate outbound bridge so it can reach Mistral without exposing the agent to the
host.

## Components exercised

1. FastAPI upload boundary.
2. A2A 1.0 Agent Card discovery and JSON-RPC `SendMessage`.
3. CrewAI Flow with two deterministic steps: text extraction and structuring.
4. Canonical itinerary contract and golden fixtures.
5. Two Docker containers connected on an internal network.

No LLM is called in this slice. PDF parsing is deterministic, so failures can be
reproduced exactly. The parser is behind the CrewAI Flow boundary and can later be
augmented by an LLM without changing the API or A2A contract.
The Compose evaluation environment also disables CrewAI telemetry and first-run trace
prompts.

## Acceptance tests

| Fixture | Assertion |
|---|---|
| Clean direct e-ticket | Parsed itinerary exactly equals its golden JSON |
| Clean connecting itinerary | Parsed itinerary exactly equals its golden JSON |
| Image-only ambiguous scan | Review is required and no protected field is invented |

The ambiguous golden contains safe partial OCR fields. This first slice did not claim
those fields because it had no OCR engine. Vertical slice 02 now recovers those safe
fields while continuing to abstain on the redacted/low-confidence values.

## Run locally

```powershell
uv sync --extra app
uv run python -m unittest discover -s tests -v
```

## Run through Docker

```powershell
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
uv run --extra app python tools/run_vertical_document_test.py
docker compose -f compose.yaml -f compose.test.yaml down
```

The live runner prints observed and expected JSON for every fixture and exits non-zero
if any acceptance assertion fails. It also waits up to 60 seconds for the published API
port, so it is safe to run immediately after Compose starts.

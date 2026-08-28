# Vertical slice 16: Azure OpenAI itinerary fallback

## Outcome

This slice uses an LLM where it has the greatest product impact: understanding
real tickets whose layout is unfamiliar to the deterministic parser. It does not
give the model notification, search, booking, cancellation, or payment authority.

```text
PDF
  -> native text, or Mistral OCR for an image-only document
  -> deterministic itinerary parser
       -> parsed: finish with zero LLM calls
       -> review_required:
            -> Azure OpenAI strict structured extraction
            -> quoted-evidence and confidence validation
            -> canonical Pydantic validation
                 -> valid: parsed itinerary
                 -> any failure or uncertainty: human review
```

## Why this boundary

Airline and agency documents vary widely in wording and layout, while the current
deterministic parser intentionally understands only a narrow synthetic format.
An LLM can generalize across those layouts. Flight-state diffing, notification
suppression, Eval authority, idempotency, and provider actions remain
deterministic because variability there would directly create spam or unsafe
actions.

## Safety controls

- The fallback is disabled unless `DOCUMENT_LLM_MODE=fallback`.
- A successfully parsed familiar ticket never invokes the model.
- The source PDF is not sent; only extracted text is sent after the first parser
  abstains.
- The ticket is marked as untrusted content in the prompt. Instructions inside
  it must not be followed.
- Azure must return the strict `LlmItineraryExtraction` JSON schema.
- Every confirmation code, flight number, route, departure, and arrival requires
  a short exact evidence excerpt present in the extracted ticket text.
- Each leg must meet `DOCUMENT_LLM_MIN_CONFIDENCE`, default `0.90`.
- Timestamps require explicit offsets and are normalized deterministically to
  UTC. Arrival must follow departure and flight duration may not exceed 48 hours.
- Trip ID, traveler authority, document authority, and leg IDs are added by the
  application, never accepted from the model.
- Provider errors, refusals, invalid JSON, unsupported evidence, and low
  confidence all fail closed to `review_required`.

## Azure configuration

The adapter accepts the existing `AZURE_ENDPOINT` and `AZURE_API_KEY` names, or
the more explicit `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` aliases.
Azure also requires the deployment name assigned in the resource:

```dotenv
DOCUMENT_LLM_MODE=fallback
AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com
AZURE_OPENAI_API_KEY=runtime-secret
AZURE_OPENAI_DEPLOYMENT=your-structured-output-capable-deployment
AZURE_OPENAI_API_VERSION=2024-10-21
```

The deployment name is not the endpoint and should not be guessed from the base
model catalogue. Keep the mode off until this value is configured and the chosen
deployment is confirmed to support strict JSON-schema output.
Existing `CHAT_DEPLOYMENT` and `CHAT_API_VERSION` settings are accepted as
aliases for the deployment and API version.

## Safe vertical proof

The test uses the image-only PDF, a local Mistral OCR stub returning a synthetic
alternate ticket layout, and a local Azure OpenAI contract stub returning the
checked-in structured extraction. It sends no ticket outside Docker and spends
no provider credits.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.document-llm-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_document_llm_test.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

Acceptance requires:

- deterministic parser abstention before the LLM call;
- exactly one OCR call and one LLM call;
- an exact match with `expected_llm_fallback_itinerary.json`;
- Azure provider/result metadata in orchestration;
- zero real Azure calls.

LangSmith records the nested
`agent.document.resolve_ambiguous_itinerary` run. Its input contains only a text
hash, character count, prompt version, provider/model, and deterministic reason
codes; raw ticket text and confirmation codes are not exported.

## Current activation status

The Azure endpoint and API key are present locally, but no
`AZURE_OPENAI_DEPLOYMENT` is configured. The code and safe contract test are
complete; real Azure fallback remains off until that deployment name is added.

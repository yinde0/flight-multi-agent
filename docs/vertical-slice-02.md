# Vertical slice 02: image-only itinerary through Mistral OCR

## User-visible behavior

The upload and A2A boundaries are unchanged. Inside the document agent, the CrewAI
flow first checks the PDF text layer. It calls Mistral OCR only when that layer is
empty. The OCR page Markdown then enters the same deterministic itinerary parser used
for native PDFs.

For the ambiguous scan, the expected result is intentionally not a complete
itinerary. OCR may safely recover:

- origin: `LHR`
- destination: `LIS`
- travel date: `2026-10-18`

It must not infer the redacted confirmation code, the uncertain flight number
`QW 7?4`, or the uncertain departure time `07:4?`. The result remains
`review_required` with those fields listed in `must_not_infer`.

## Runtime path

```text
Travel API
  -> A2A JSON-RPC
    -> CrewAI document flow
       -> pypdf text-layer extraction
       -> if empty: POST /v1/ocr to Mistral
       -> deterministic structuring and safety checks
       -> canonical itinerary or review-required result
```

Mistral receives the PDF as an in-memory base64 data URL. This slice does not use the
Mistral Files API, does not persist the OCR response, and never writes document text to
application logs. Production use still requires an approved policy for sending ticket
data to an external processor.

The document agent keeps its private A2A network and also joins a dedicated outbound
bridge named `document-egress`. No document-agent or OCR port is published to the
host; the second network exists only so the container can resolve and call Mistral.

## Why this is not MCP or RAG

OCR is an implementation capability owned by the document agent, so it sits behind a
small provider interface. RAG would add retrieval but would not improve reading the
current PDF. The planned MCP boundaries remain flight status/search, weather, and
post-evaluation notification in the monitoring slice.

## Deterministic evaluation

The local test overlay starts a private HTTP service that implements the subset of
Mistral's `/v1/ocr` contract used here. It replays the checked-in OCR response fixture.
This verifies the real HTTP adapter and container wiring without an API key, usage
charges, network variability, or external data transfer.

```powershell
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_document_test.py
docker compose -f compose.yaml -f compose.test.yaml down
```

The image-only assertion checks all of the following:

- exactly one OCR call was recorded in orchestration provenance;
- `text_source` is `mistral_ocr`;
- safe partial fields exactly match the golden JSON;
- protected ambiguous fields remain absent; and
- the review reason codes exactly match the golden JSON.

## Real Mistral smoke test

Copy `.env.example` to `.env`, add a Mistral API key, and run the base Compose file:

```powershell
Copy-Item .env.example .env
# Edit .env and set MISTRAL_API_KEY without committing it.
docker compose up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_document_test.py
docker compose down
```

The application returns `OCR_NOT_CONFIGURED` when a scan needs OCR but the key is
absent. Provider timeouts, HTTP failures, invalid JSON, and empty page results return
`OCR_PROCESSING_FAILED`. Both cases require human review and contain no itinerary.

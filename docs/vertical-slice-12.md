# Vertical Slice 12: Streamlit Traveler Experience

## Outcome

This slice adds the first customer-facing surface to the working backend. A
traveler can upload one text or scanned PDF, consent to storage and monitoring,
and see the parsed journey become an active travel watch.

The UI is deliberately a thin client. It calls only `travel-api`; provider
credentials, document-agent access, MCP services, event subjects, storage
locations, and operational controls remain inside the container network.

## Customer journey

1. The traveler enters a display name, an optional consented SMS number, and
   selects a PDF up to 5 MB.
2. The browser-facing app validates the PDF signature and creates opaque upload
   identifiers that are reused if the request is retried.
3. `travel-api` activates the trip through the existing orchestrator vertical.
4. A parsed itinerary is shown as route and schedule cards.
5. A low-confidence parse becomes a calm `review_required` state and is never
   scheduled against a guessed flight.
6. The traveler can refresh the persisted trip view to see monitoring progress.

## Experience and safety decisions

- Booking references are masked in the UI.
- Raw upstream response bodies are never shown to the traveler.
- The upload requires explicit storage-and-monitoring consent.
- Gate-only changes, sub-30-minute delays, and unchanged observations are
  explained as suppressed noise.
- Rebooking remains a read-only search; this slice does not purchase anything.
- Trip recovery is session-local for now. Account authentication must be added
  before exposing cross-device trip lookup.

## Container boundary

`traveler-ui` uses its own small image and joins two networks:

- `travel-edge` exposes only Streamlit on host port `8501`.
- `travel-internal` permits the UI to call `travel-api:8000`.

It receives no AviationStack, OpenWeatherMap, Duffel, Mistral, database, S3, or
notification credentials.

## Evaluation gate

Install the UI and test extras, then run the vertical check:

```powershell
uv sync --extra app --extra test --extra ui
.\.venv\Scripts\python.exe tools\run_vertical_frontend_test.py
```

The automated checks assert that:

- invalid evidence is rejected;
- multipart activation uses the correct API boundary;
- service errors are sanitized;
- booking references are masked;
- a customer upload reaches the expected ready state; and
- the golden `MAN → FRA` itinerary and one monitored flight are visible.

For the container smoke test:

```powershell
docker compose up -d --build --wait
```

Open `http://localhost:8501`. The API remains available on port `8080` for
development and integration testing.

## Deliberate limitations

- There is no customer account, authentication, or authorization boundary yet.
- Notifications are not displayed as an in-app history yet.
- Refresh is manual in this slice; the backend scheduler remains automatic.
- Accessibility is covered by native Streamlit controls and labels, but needs a
  dedicated keyboard and screen-reader acceptance pass before production.

# Vertical slice 17: friendly disruption explanations

This slice adds Azure OpenAI only after deterministic Eval has approved a
notification. The model can explain a disruption; it cannot decide whether the
traveler is contacted or whether a replacement-flight search runs.

## Runtime boundary

```text
disruption_confirmed
  -> Notification Action Service re-verifies the persisted Eval decision
  -> A2A Communication Agent receives PII-free operational facts
  -> Azure OpenAI returns schema-constrained friendly wording
  -> local safety validator accepts it or selects a deterministic fallback
  -> Notification MCP sends the already-authorized message once
```

The Communication Agent has its own container and external egress network. It
has no access to ticket PDFs, Postgres, DynamoDB, phone numbers, Twilio
credentials, search, booking, cancellation, or payment tools. The Notification
Action Service keeps the consented recipient on its private network and sends
only category, delay/connection/weather facts, verdict, and reason codes over
A2A.

## Safety properties

- Eval authority is checked before explanation generation.
- `SUPPRESS` is rejected by both the confirmed-event and explanation contracts.
- Azure receives no traveler identifier, phone number, confirmation code, or
  ticket text.
- Strict JSON Schema output is followed by local validation of category,
  confidence, cited fields, and numeric facts.
- Claims of booking, rebooking, refunds, compensation, guarantees, URLs, and
  internal reason codes are rejected.
- Azure refusal, outage, malformed output, unsafe wording, or an unavailable A2A
  agent immediately selects the deterministic friendly template. Notification
  is not blocked.
- Notification idempotency remains keyed by the Eval decision, so a redelivered
  event does not regenerate or resend the message.

## LangSmith view

Development tracing records `agent.communication.explain_disruption` beneath
the confirmed-disruption workflow. Its input contains the exact PII-free fact
contract and its output contains the generated or fallback message, source,
model, prompt version, confidence, and failure code. Generic HTTP spans remain
hidden.

## Safe vertical test

The test uses the flight-agency simulator, recording notification provider, and
a local Azure-compatible stub. It does not call Azure or send an SMS.

```powershell
docker compose -f compose.yaml -f compose.agency-demo.yaml -f compose.communication-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_communication_test.py
```

The golden sequence stores an on-time baseline, creates a 45-minute delay,
expects `NOTIFY`, and verifies this exact model-authored wording:

> Your flight is now delayed by 45 minutes. We'll keep watching for further changes.

## Real Azure activation

The shared Azure endpoint, key, API version, and deployment settings are reused.
Existing `CHAT_DEPLOYMENT` and `CHAT_API_VERSION` settings are accepted aliases,
so the Azure chat configuration does not need to be duplicated.
The default `auto` mode uses Azure only when the endpoint, key, and deployment
are all present. Force deterministic templates with `off`, or require an Azure
attempt with `azure`:

```env
DISRUPTION_EXPLANATION_MODE=auto
AZURE_OPENAI_DEPLOYMENT=your-actual-azure-deployment-name
```

When the mode is `off`, or `auto` finds incomplete configuration, the same A2A
agent returns deterministic friendly templates and the remainder of the
pipeline is unchanged.

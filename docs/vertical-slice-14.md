# Vertical Slice 14: Signed SMS Delivery Reconciliation

## Outcome

This slice distinguishes Twilio accepting an SMS from a carrier delivering it.
The notification MCP supplies a `StatusCallback` URL when it creates a Message.
A dedicated webhook service verifies the callback, resolves the Message SID to
the existing notification, and atomically advances its durable DynamoDB state.

WhatsApp is deliberately out of scope. The only customer delivery channel in
this slice is explicitly consented SMS after Eval approval.

## Security and data path

```text
Eval-approved action -> Notification MCP -> Twilio Message (queued)
                                              |
                                              | signed form callback
                                              v
public HTTPS ingress -> notification-webhook-service -> DynamoDB delivery state
```

- The sending MCP holds the API key and secret. The webhook service does not.
- The webhook service holds the Primary Auth Token required to verify
  `X-Twilio-Signature` and does not call the Twilio REST API.
- Validation uses Twilio's official `RequestValidator`, the exact configured
  public callback URL, and the complete evolving form payload.
- The callback persists only Account SID, Message SID, status, numeric provider
  error code, and timestamps. It does not persist callback phone fields.
- A provider Message SID reverse index resolves the existing action record; a
  callback cannot create a notification or bypass Eval authority.

The normal Compose stack starts the webhook disabled. Production must terminate
TLS at a public ingress and route the exact `TWILIO_STATUS_CALLBACK_URL` to the
webhook container. Twilio cannot call `localhost` or a Docker-only hostname.

## Monotonic delivery rules

Progress states are `accepted -> queued -> sending -> sent`. Terminal states are
`delivered`, `undelivered`, `failed`, and `canceled`.

- `delivered` maps to action status `delivered`.
- `undelivered`, `failed`, and `canceled` map to action status `failed`.
- Repeated callbacks are acknowledged without another write.
- A late progress callback cannot overwrite a newer state.
- Once a terminal state is stored, later callbacks cannot change it.
- DynamoDB compare-and-set protects concurrent callback processing.

Provider failures retain only a privacy-safe code such as `TWILIO_30003`. They
do not trigger an automatic resend, avoiding accidental notification spam.

## Configuration

```dotenv
NOTIFICATION_PROVIDER=twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_API_KEY=SK...
TWILIO_API_SECRET=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+...
TWILIO_WEBHOOK_ENABLED=true
TWILIO_STATUS_CALLBACK_URL=https://your-domain.example/v1/webhooks/twilio/status
```

`TWILIO_AUTH_KEY` is accepted as a backward-compatible local alias, but
`TWILIO_AUTH_TOKEN` is the standard name. For the current trial account only,
`TWILIO_SMS_BODY_OVERRIDE=sms_appointment_reminders` can reproduce Twilio's
predefined test SMS. It must not be mistaken for a real disruption template.

## Replayable container proof

The test overlay uses synthetic credentials and a local Twilio stub. It does not
contact Twilio or send a real SMS.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml -f compose.twilio-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_sms_delivery_test.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

The runner proves one SMS follows an Eval-approved cancellation, the Message
contains a callback URL, signed `sent` and `delivered` callbacks are accepted,
duplicates and stale updates do not regress state, a forged callback receives
HTTP 403, and no phone number appears in public trip or delivery output.

## Remaining production work

- Provision a stable public HTTPS ingress before enabling callbacks.
- Add callback latency/failure metrics and operational alerting.
- Add a deliberate resend policy, if ever required, with a separate authority
  decision and strict attempt limits.
- Add authenticated customer accounts, phone ownership verification, contact
  encryption, retention/deletion workflows, and verified opt-out handling.

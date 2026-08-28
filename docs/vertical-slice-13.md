# Vertical Slice 13: Consented Twilio SMS Notifications

## Outcome

This slice collects an optional international mobile number during ticket
upload and can submit significant, Eval-approved disruption alerts to Twilio.
The browser never receives Twilio credentials and Twilio cannot be called before
the existing `disruption_confirmed` authority gate.

## Data and authority path

1. Streamlit normalizes the number to E.164 and asks for explicit operational
   SMS consent.
2. `travel-api` revalidates the number and consent pair before forwarding it.
3. The trip orchestrator stores the phone and consent timestamp in
   `trip_notification_contacts`, separate from itinerary JSON.
4. Public trip and activation responses never include the phone number.
5. After Eval approves a disruption, the notification action service resolves
   the consented recipient over the private Docker network.
6. Only then does `send_notification` receive an SMS command and submit it to
   Twilio.

The SMS contains a customer-safe disruption summary, an opt-out reminder, and
no internal trip, leg, decision, or candidate identifier.

## Twilio configuration

Twilio Messaging uses HTTP Basic authentication. For the recommended API-key
flow, configure all of:

```dotenv
NOTIFICATION_PROVIDER=twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_API_KEY=SK...
TWILIO_API_SECRET=...
TWILIO_MESSAGING_SERVICE_SID=MG...
```

`TWILIO_API_KEY` is the public `SK...` key identifier; it is not the API-key
secret. For local testing only, `TWILIO_AUTH_TOKEN` can replace the API key and
secret. `TWILIO_FROM_NUMBER` can replace the Messaging Service SID, although a
Messaging Service is preferred for sender selection and opt-out management.

Credentials stay in `.env` and are injected only into `notification-mcp`.

## Replayable vertical test

The Twilio test overlay uses synthetic credentials and a local stub. It never
contacts Twilio or sends a real SMS.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml -f compose.twilio-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_twilio_test.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

The runner proves that exactly one SMS follows an Eval-approved cancellation,
the synthetic API-key authentication and sender are correct, the destination
matches the consented number, and the phone is absent from public trip output.

## Deliberate limitations

- Twilio message creation reports provider acceptance; Slice 14 now reconciles
  signed delivery callbacks to a durable terminal state.
- The in-process duplicate guard cannot close the small crash window between
  Twilio accepting a message and DynamoDB recording its receipt.
- Production needs customer authentication, phone ownership verification,
  contact deletion/retention workflows, and application-level contact
  encryption in addition to managed database encryption.
- Configure Twilio Messaging Service opt-out handling and jurisdiction-specific
  sender registration before sending real customer traffic.

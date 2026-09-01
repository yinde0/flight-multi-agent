# SMS delivery troubleshooting

## Custom text and Twilio Trial

Twilio's current trial supports predefined SMS templates, not arbitrary custom
message bodies. A generated flight explanation therefore requires an account
that supports custom SMS. Upgrading is an operator action; the application does
not change account billing or bypass trial restrictions. Follow Twilio's
[trial documentation](https://www.twilio.com/docs/usage/trials/try-out-sms),
including the sender setup required after upgrade.

`TWILIO_SMS_BODY_OVERRIDE` replaces the entire SMS body. Only set it to an
approved trial template when deliberately testing template delivery. That test
does **not** demonstrate delivery of the Communication Agent's flight message.
For custom flight messages, leave the override empty and use an eligible account
and sender. Recreate `travel-tools-mcp` after changing its environment, using
the same Compose overlays as the running stack.

The manual agency demo also requires `DEMO_NOTIFICATION_PROVIDER=twilio`, a
saved phone number, and explicit SMS consent. Its default `recording` provider
does not contact a phone. Eval suppression and deduplication still apply: checking
an unchanged flight is not a request to resend a previously approved alert.

## Read the delivery state, not the message preview

- **Prepared message:** the text is ready; nothing about delivery is implied.
- **Accepted:** the provider returned a message identifier. Delivery is unconfirmed.
- **Delivered:** the configured provider confirmed delivery. A recording-provider
  result is simulated, not real SMS delivery.
- **Failed/rejected:** inspect the safe error code and remediation in the demo.
- **Duplicate:** no new message was submitted; this is not a new delivery receipt.

For real Twilio delivery confirmation, configure a public HTTPS
`TWILIO_STATUS_CALLBACK_URL` and `TWILIO_WEBHOOK_ENABLED=true`; the webhook
verifies Twilio signatures. Without this setup an accepted message can remain
`accepted` locally even after the handset receives it. Check the corresponding
Message SID in Twilio's message log. See [slice 14](vertical-slice-14.md).

## Safe errors and retries

Notification MCP returns a structured, sanitized failure. The action service
persists it; the orchestrator and demo expose the error code and static guidance.
Raw provider messages, URLs, phone numbers, and credentials are not propagated
in this failure contract or its traces.

| Failure | Action |
| --- | --- |
| Trial/custom-body restriction | Upgrade and configure a sender, or explicitly test an approved trial template. |
| `TWILIO_21608` | Verify the recipient and check trial restrictions. |
| `TWILIO_20003`, HTTP 401/403 | Check credentials and account permissions. |
| `TWILIO_21606` / `TWILIO_21607` | Check the sender configuration. |
| HTTP 429 or 5xx | Retry within the existing worker's bounded retry budget. |
| Other provider 4xx | Quarantine immediately; fix the provider rejection before an operator retry. |
| `TWILIO_SUBMISSION_UNCERTAIN` / `TWILIO_INVALID_RESPONSE` | Check Twilio's message log before retrying; a failed response does not prove that no SMS was created. |

Non-retryable submissions are quarantined on the first worker attempt instead
of repeatedly submitting the same rejected request. Quarantined alerts are not
automatically resent after configuration changes. An authorized operator can
redrive the specific original event after checking for an existing Message SID;
do not bulk-redrive old traveler alerts. See [slice 09](vertical-slice-09.md).

## Regression checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_twilio_notification.py tests/test_notification_vertical_slice.py tests/test_traveler_ui.py tests/test_telemetry.py
```

These tests use mocked providers and synthetic contacts; they do not send SMS.
They cover safe provider codes, permanent/transient failure handling, uncertain
POST outcomes, FastMCP structured results, durable action records, safe trace
outputs, and the Streamlit failure display. Real handset delivery requires a
separate, explicitly authorized live test with an eligible Twilio account.

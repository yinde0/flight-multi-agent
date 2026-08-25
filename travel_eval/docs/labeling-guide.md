# Golden Scenario Labeling Guide

## Label the consequence, not the raw field change

Annotators should distinguish:

1. Provider observation: what the external source reported.
2. Derived delta: what changed relative to the last accepted state.
3. Candidate: what travel consequence might follow.
4. Decision: whether the consequence is significant and actionable.
5. Action: whether to notify and whether to search for alternatives.

## Allowed verdicts

- `SUPPRESS`: retain evidence, but do not interrupt the traveler.
- `NOTIFY`: communicate an actionable material change.
- `NOTIFY_AND_SEARCH`: communicate and initiate a read-only alternative search.

No verdict permits booking, payment, cancellation, check-in, or traveler-profile modification.

## Required annotation fields

- Candidate category and critical derived values.
- Verdict.
- One or more stable reason codes.
- Applicable policy version.
- Whether a rebooking search is requested.
- A short adjudication note outside the machine contract when reviewers disagree.

## Review process

Two reviewers independently label high-impact scenarios. Disagreements about cancellation, diversion, connection risk, or notification suppression require adjudication. Keep a hidden test partition that prompt and policy authors do not use while tuning.

## Privacy

Use synthetic or consented and irreversibly redacted documents. Never retain passenger names, email addresses, ticket numbers, loyalty identifiers, barcodes, payment data, or usable PNRs in a public fixture set.

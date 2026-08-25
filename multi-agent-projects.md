
## 1. Financial — Personal Finance & Tax-Loss Harvesting Copilot

**Problem statement:** Ingest bank/brokerage statements and live market data, detect abnormal spending or portfolio drift, and propose tax-loss-harvesting trades — without ever surfacing a suggestion that would violate a wash-sale rule or exceed the user's stated risk tolerance.

**Why multi-agent:** Parsing a statement PDF into structured transactions is a different skill from computing portfolio drift/anomaly scores from live prices — and neither of those agents should be the one deciding whether a resulting recommendation is *safe to show*. That's a separate, adversarial check.

**Agents:**
- **Orchestrator (light):** routes "parse this statement" vs. "what's my risk today" vs. "any harvesting opportunities," maintains per-user session state, calls the other three as needed
- **Doc Parsing Agent:** extracts transactions/holdings/cost-basis from uploaded brokerage/bank PDFs (pulled from S3)
- **ML Analysis Agent:** computes spend-anomaly scores, portfolio drift, and candidate tax-loss-harvesting trades from live + historical price data
- **Eval Agent:** checks every harvesting candidate against wash-sale timing rules and the user's declared risk tolerance before it's allowed to reach the user; rejects or flags candidates that fail

**MCP integration:** MCP server exposing `get_quote`, `get_holdings`, `get_transactions` (market/brokerage API), and a `parse_statement` tool backed by the Doc Parsing Agent's pipeline.

**DB architecture:** RDS/Postgres for the transaction/holdings ledger (needs relational integrity for cost-basis lot matching) · DynamoDB for live price cache and rolling anomaly windows · S3 for raw uploaded statements.

**Stateful design:** ML Analysis Agent keeps a rolling per-ticker price window in DynamoDB so drift is incremental, not recomputed from scratch each call. Eval Agent persists which candidates it already rejected and why, so the same wash-sale conflict isn't re-flagged as "new" tomorrow.

**Communication:** Orchestrator calls Doc Parsing Agent synchronously on upload; ML Analysis Agent runs on a schedule and writes candidates to a shared table; Eval Agent consumes from that table and writes an approved/rejected status back to it — Orchestrator only ever reads the *final* status, never raw ML output.

**APIs:** Alpha Vantage (equities), CoinGecko (crypto), Frankfurter (FX).

---

## 2. Healthcare — Clinical Trial Matching & Vitals Monitor

**Problem statement:** Continuously watch wearable vitals for anomalies and, separately, match a patient's (synthetic/de-identified) profile against clinical trial eligibility criteria — with a hard safety check before either an alert fires or a trial match gets scheduled.

**Why multi-agent:** Vitals anomaly detection is a continuous numerical task; trial-eligibility matching requires parsing unstructured trial-protocol documents against structured patient data — different tools entirely. Given this is healthcare, neither should get to act unilaterally: a third agent has to confirm the match/alert actually meets hard clinical criteria before anything reaches a human.

**Agents:**
- **Orchestrator (light):** routes streaming vitals to the ML agent, routes "find me a trial" requests to the doc parsing agent, holds per-patient session state
- **ML Analysis Agent:** maintains rolling vitals baselines per patient, flags statistical anomalies
- **Doc Parsing Agent:** extracts structured eligibility criteria (age, diagnosis, meds, exclusions) from trial protocol PDFs and matches against patient record
- **Eval Agent:** clinical-safety gate — confirms an anomaly is severe enough to alert (vs. noise) and confirms a trial match doesn't violate a hard exclusion criterion before scheduling is allowed

**MCP integration:** MCP server wrapping ClinicalTrials.gov (`search_trials`, `get_trial_details`) and openFDA (`get_adverse_events`); separate MCP server for calendar tools (`create_appointment`) — gated behind Eval Agent approval.

**DB architecture:** RDS/Postgres for patient record, diagnosis codes, medication list · DynamoDB for the live vitals stream (keyed by patient_id + timestamp, TTL'd) · S3 for uploaded lab reports and trial protocol documents.

**Stateful design:** ML Analysis Agent's per-patient baseline must persist across restarts — losing it means every reading looks "new." Eval Agent logs every accept/reject decision with justification to S3 for audit (required in this domain).

**Communication:** Pub/sub — ML Agent publishes `anomaly_candidate`; Eval Agent subscribes, checks severity, and only *then* publishes `anomaly_confirmed`, which is what actually reaches the Orchestrator/notification path.

**APIs:** ClinicalTrials.gov API, openFDA.

---

## 3. Retail — Shelf-Vision Stock Verification & Dynamic Pricing

**Problem statement:** Use photos from store shelves/warehouses to verify stock counts against the system of record, forecast demand, and recommend price changes — without ever applying a price change that violates a minimum-margin or MAP (minimum advertised price) policy.

**Why multi-agent:** Reading a shelf photo to estimate stock is a vision task; forecasting demand and computing a price recommendation is a numerical/ML task; neither of those should have unilateral write-access to the live price — a policy check is a distinct, non-negotiable gate.

**Agents:**
- **Orchestrator (light):** decides which SKUs need a shelf-photo check vs. a forecast refresh, applies eval-approved price changes via the MCP tool
- **Image Parsing Agent:** analyzes shelf/warehouse photos, estimates current stock level, flags discrepancies vs. system-of-record count
- **ML Forecasting Agent:** produces per-SKU demand forecasts and a candidate price recommendation from forecast + competitor price data
- **Eval Agent:** rejects any price recommendation that breaches minimum margin or MAP policy, or any stock correction that's implausibly large (likely a mis-read) before it's applied

**MCP integration:** MCP server wrapping the product/catalog API (`get_product`, `update_price`, `update_stock_count`) — all writes go through this tool and only after Eval Agent sign-off.

**DB architecture:** RDS/Postgres for product master and warehouse stock ledger · DynamoDB for live per-store stock counters and current price · S3 for uploaded shelf photos and historical sales exports used as forecast input.

**Stateful design:** ML Forecasting Agent persists seasonality coefficients per SKU for incremental updates rather than full retrains. Image Parsing Agent keeps a per-shelf-location confidence history — repeated low-confidence reads at one location get flagged for a human recount instead of auto-applied.

**Communication:** Shared-state table in DynamoDB — Image and Forecasting agents write independently on their own schedules; Eval Agent polls the table, approves/rejects, and only approved rows are ever read by the Orchestrator for action.

**APIs:** Fake Store API / Open Food Facts (catalog data), Frankfurter (multi-currency pricing).

---

## 4. E-commerce — Returns, Refunds & Fraud Triage

**Problem statement:** Given a customer's return/refund request with photos of the item, assess whether the photo evidence is consistent with the claim, score the request for fraud risk, and decide approve/deny/escalate — with a policy check that neither the vision agent nor the fraud model gets to bypass.

**Why multi-agent:** Judging "does this photo match the damage described" is a vision task; scoring fraud risk from account/order history is a numerical/pattern task; the actual approve/deny decision needs to weigh both against a written refund policy — a third kind of judgment neither upstream agent is positioned to make.

**Agents:**
- **Orchestrator (light):** classifies the request, maintains per-customer conversation state across sessions, invokes the two doer agents, applies the Eval Agent's verdict
- **Image Parsing Agent:** analyzes uploaded photos of the returned/damaged item, checks consistency with the customer's description
- **ML Fraud Scoring Agent:** scores the request using order history, account age, claim frequency, and address-mismatch signals
- **Eval Agent:** combines the image assessment + fraud score against the written refund policy and issues approve / deny / escalate-to-human

**MCP integration:** MCP server wrapping the order-management system (`get_order`, `issue_refund`) — `issue_refund` is a write tool that only fires on an Eval Agent "approve" verdict, never directly from the fraud or image agent.

**DB architecture:** RDS/Postgres for orders and refund transactions (needs strong consistency for money-adjacent writes) · DynamoDB for per-customer conversation state (so multi-day conversations resume correctly) and a rolling claim-frequency counter · S3 for uploaded item photos and chat transcripts.

**Stateful design:** Conversation state persists per customer so they don't repeat themselves across sessions. ML Fraud Agent keeps a sliding-window claim counter per customer/address to catch abuse patterns over time, not just within one conversation.

**Communication:** Orchestrator calls Image and Fraud agents as parallel tool-calls, then hands both outputs to Eval Agent as a single synchronous step before responding — this project doesn't need async pub/sub since the whole loop happens within one customer interaction.

**APIs:** DummyJSON / FakeStoreAPI (order/product data), a free package-tracking API (e.g., TrackingMore free tier) for shipment status lookups feeding the fraud signal (mismatched delivery address vs. claim).

---

## 5. Travel — Itinerary Parsing & Disruption Monitoring

**Problem statement:** Parse a booked itinerary (e-tickets, confirmation PDFs), then continuously monitor flight status and weather for disruptions, and decide whether a detected disruption is significant enough to notify the traveler or trigger a rebooking search — without spamming them over every minor gate change.

**Why multi-agent:** Extracting flight numbers/confirmation codes from PDFs is a document task done once per trip; monitoring status deltas is a continuous polling task; deciding what's "notify-worthy" is a judgment call distinct from the raw detection — conflating it with the monitor means every status change becomes a notification.

**Agents:**
- **Orchestrator (light):** triggers itinerary parsing on booking, then hands the trip off to the monitoring loop, wakes the notification path only on Eval Agent approval
- **Doc Parsing Agent:** extracts flight numbers, confirmation codes, and scheduled times from e-ticket/itinerary PDFs
- **ML Monitoring Agent:** polls flight status + weather, diffs against last known state per flight, produces a disruption-candidate score
- **Eval Agent:** applies suppression rules (e.g., ignore delays under 30 minutes, ignore gate-only changes) and decides whether a candidate warrants notifying the traveler or triggering a rebooking search

**MCP integration:** MCP server wrapping flight-status/search APIs (`get_flight_status`, `search_flights`) and a separate MCP server for notification (`send_notification`) — notification only fires post-Eval-approval.

**DB architecture:** RDS/Postgres for itinerary structure (legs, confirmation numbers) · DynamoDB for live flight-status cache (last known state per flight, so the monitor can diff instead of re-alerting on unchanged status) · S3 for uploaded e-tickets and generated itinerary PDFs.

**Stateful design:** ML Monitoring Agent must persist last-known-status per flight — without it, it can't distinguish "changed" from "unchanged" and would re-flag every poll.

**Communication:** Event bus — Monitoring Agent publishes `disruption_candidate`; Eval Agent subscribes, applies suppression logic, and only publishes `disruption_confirmed`, which is what the Orchestrator acts on.

**APIs:** AviationStack (flight status), OpenWeatherMap (forecasts).

---

## 6. Insurance — Auto Claims Intake & Fraud Triage

**Problem statement:** Take an inbound auto claim (photos, description, VIN, incident time/location), assess whether the photos are consistent with the claimed damage, score the claim for fraud risk using VIN/weather/plausibility checks, and route it to auto-approval, adjuster review, or investigation.

**Why multi-agent:** Photo damage assessment is a vision task; fraud scoring from VIN history + weather-at-incident-time plausibility is a different, evidence-correlation task; the routing decision has to weigh both against policy coverage rules — again, a distinct judgment from either input agent.

**Agents:**
- **Orchestrator (light):** normalizes the incoming claim, invokes the two doer agents, applies the routing decision from Eval Agent
- **Image Parsing Agent:** analyzes uploaded damage photos (and OCRs any scanned police report) for consistency with the claim description
- **ML Fraud Scoring Agent:** decodes VIN, checks weather-at-incident-time plausibility, and scores the claim against historical fraud patterns
- **Eval Agent:** combines photo assessment + fraud score against policy coverage rules to route: auto-approve / adjuster review / investigation — the only agent allowed to make the routing call

**MCP integration:** MCP server wrapping NHTSA's VIN-decode API (`decode_vin`) and a weather-history API (`get_historical_weather`), used by the Fraud Scoring Agent.

**DB architecture:** RDS/Postgres for policies and claims (needs relational integrity — a claim must join to exactly one active policy) · DynamoDB for the claim's processing state machine (current stage + each agent's persisted output, so a claim can pause for more photos and resume days later) · S3 for uploaded damage photos and police reports.

**Stateful design:** Each claim carries a state object in DynamoDB that accumulates agent outputs as it moves through the pipeline — Eval Agent reads Image and Fraud agents' *persisted* conclusions rather than recomputing them, and the state history itself is the audit trail for "why was this claim escalated."

**Communication:** Explicit state-graph/DAG — each agent's completion transitions the claim's state and triggers the next step; this (rather than free-form messaging) is what makes the routing decision auditable after the fact.

**APIs:** NHTSA VIN Decoder API, OpenWeatherMap (historical weather).

---

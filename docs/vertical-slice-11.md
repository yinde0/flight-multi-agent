# Vertical Slice 11: CrewAI Eval Reasoning in Shadow Mode

## Outcome

This slice adds a real CrewAI task backed by Mistral to review each deterministic
Eval decision. The reviewer returns a structured recommendation and rationale,
but it has no tools and no authority to notify, search, book, cancel, or pay.

The deterministic, versioned suppression policy remains the only decision
authority:

```text
disruption_candidate
  -> deterministic policy -> authoritative decision -> durable action boundary
                         |
                         `-> CrewAI + Mistral shadow review
                             -> stored audit advisory only
```

If the model times out, returns malformed output, or disagrees, the authoritative
decision is unchanged. The failure or disagreement is stored for evaluation.

## Bounded model input

The reviewer receives only a decision-evidence allowlist:

- disruption category, severity band, delay, connection risk, and weather
  corroboration;
- versioned policy rules and safety invariants;
- the deterministic verdict and reason codes it is reviewing.

It does not receive the traveler reference, confirmation code, trip ID, leg ID,
PDF/OCR text, full provider payload, or notification content. Candidate fields
are explicitly described as untrusted evidence, not instructions.

## Versioned prompt and strict output

`travel_eval/prompts/eval_advisory.v1.md` is a checked-in, versioned prompt. It
requires exactly one JSON object matching the `EvalAdvisory` Pydantic schema:

- prompt and schema versions;
- `recommended_verdict`;
- ordered `reason_codes`;
- bounded confidence;
- short rationale.

CrewAI is configured without tools, delegation, memory, planning, or code
execution. Returned text is parsed as one JSON object and validated strictly.
Extra fields, renamed wrappers, prose outside the object, and invalid verdicts
are rejected and recorded as `EVAL_REASONING_FAILED`.

## Policy drift found by the golden review

The shadow evaluation exposed a specification mismatch: the policy table used a
single generic location-only label while executable rules and goldens used the
more precise `GATE_ONLY_CHANGE` and `TERMINAL_ONLY_CHANGE` labels. Policy version
`1.2.0` now declares both rules explicitly. The deterministic implementation,
goldens, prompt evidence, and policy table therefore agree.

## Evaluation gates

Run the real networked CrewAI/Mistral review over every golden decision:

```powershell
.\.venv\Scripts\python.exe tools\run_vertical_eval_reasoning_test.py
```

The runner prints aggregate results only. It checks structured-output success,
verdict agreement, exact verdict-plus-reason agreement, and zero unauthorized
actions. Current acceptance thresholds require at least 95% structured success
and verdict agreement in shadow evaluation; zero unauthorized actions remains a
release gate.

The containerized end-to-end proof enables shadow mode with:

```powershell
docker compose -f compose.yaml -f compose.langsmith.yaml -f compose.langsmith-development.yaml -f compose.eval-reasoning.yaml -f compose.trace-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_langsmith_end_to_end_trace.py
```

Normal Compose defaults `EVAL_REASONING_MODE=off`. The overlay requires
`MISTRAL_API_KEY`, changes the mode to `shadow`, and gives only the Eval
container a dedicated external egress network. Normal/off mode remains on the
private application network.

## Promotion criteria

This slice does not promote the model to decision authority. Any future proposal
to do so requires a separate reviewed change, representative production-shadow
data, calibrated disagreement analysis, adversarial prompt-injection tests,
latency and cost budgets, and a rollback path. Notification/search services must
continue to re-read an authoritative persisted decision regardless of how that
decision is produced.

## Deliberate limitations

- The checked-in golden set has nine decisions and is a contract suite, not a
  statistically representative production dataset.
- Model results can vary across provider/model revisions. The deterministic
  policy prevents that variation from changing traveler-visible consequences.
- Rationale quality is stored for inspection but is not scored semantically;
  the automated comparison focuses on schema, verdict, and reason-code agreement.

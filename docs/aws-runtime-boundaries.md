# AWS runtime boundaries

The production application uses the same backend image for every backend ECS
service, but each task has a different command, IAM role, security group, and
environment. `EVENT_BUS_PROVIDER=sqs` selects SQS; local Compose keeps
`EVENT_BUS_PROVIDER=nats`.

## SQS topology

Provision three source queues and three dead-letter queues:

| Source queue | Consumer | Application environment |
| --- | --- | --- |
| disruption candidate | Eval Agent | `SQS_QUEUE_URL_DISRUPTION_CANDIDATE` |
| confirmed notification | Notification Action | `SQS_QUEUE_URL_NOTIFICATION` |
| confirmed search | Flight Search Action | `SQS_QUEUE_URL_FLIGHT_SEARCH` |

The separate confirmed queues are intentional fan-out. Publishing one
`disruption_confirmed` event sends a copy to both queues. Each source queue must
also have an AWS redrive policy pointing to its own DLQ, with the same maximum
receive count as `EVENT_MAX_DELIVERIES`. The application explicitly copies a
quarantined message to the configured `SQS_DLQ_URL_*` before deleting it, while
the AWS redrive policy remains a safety net for process crashes.

Set `SQS_IDEMPOTENCY_TABLE` to a DynamoDB table whose partition key is the
string attribute `event_key` and enable TTL on `expires_at`. The consumer claims
`consumer-name#event-id`, extends SQS visibility while work is active, marks the
claim complete before ACK/deletion, and releases the claim on retry.

Required event-task IAM actions are scoped to their own queues:

- Publishers: `sqs:SendMessage`.
- Consumers: `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
  `sqs:ChangeMessageVisibility`, `sqs:GetQueueAttributes`.
- Quarantine paths: `sqs:SendMessage` on that consumer's DLQ.
- Consumers: DynamoDB `PutItem`, `GetItem`, `UpdateItem`, and `DeleteItem` on
  the idempotency table.

## Internet boundary

Set `EXTERNAL_CALLS_PROVIDER=mcp` on Document, Eval, Communication, Monitoring,
Notification, and Flight Search tasks. Only `travel-tools-mcp` sets it to
`direct` and receives the Azure OpenAI, Mistral, AviationStack,
OpenWeatherMap, Duffel, Twilio, and LangSmith secrets.

The private tasks call authenticated MCP tools for OCR, itinerary extraction,
Eval shadow review, disruption wording, flight status, weather, search, and
notification. Their OTLP exporter targets the private MCP relay at
`http://travel-tools-mcp:8003/otel/v1/traces`; the relay alone forwards accepted
agent/MCP spans to LangSmith. Do not put provider credentials in any other ECS
task definition.

The MCP task is assigned the only backend public IP. Its security group has no
public inbound rule; private backend security groups may reach its MCP port.
The other Fargate tasks use VPC endpoints and IAM roles for SQS, DynamoDB, S3,
ECR, CloudWatch Logs, Secrets Manager, and STS, so they require no NAT gateway.

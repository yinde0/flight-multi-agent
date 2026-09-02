# AWS deployment templates

The three buildspecs run lint, the complete pytest suite, and the golden replay
suite before building and pushing one immutable, commit-tagged image. They do
not register task definitions or update ECS services. This makes the first
manual CodeBuild run an image-publication step only.

Create these CodeBuild projects with privileged mode enabled and no artifacts:

| Project | Buildspec |
| --- | --- |
| `travel-dev-ui-build` | `deploy/aws/buildspec.frontend.yml` |
| `travel-dev-backend-build` | `deploy/aws/buildspec.backend.yml` |
| `travel-dev-mcp-build` | `deploy/aws/buildspec.mcp.yml` |

All projects use the existing `travel-dev-codebuild-role` and remain outside a
VPC. Each build expects its named ECR repository to exist in the build Region.

## Task-definition placeholders

The task-definition files are JSON templates. Replace these markers before
registering a revision:

- `REPLACE_ACCOUNT_ID`: the 12-digit AWS account ID.
- `REPLACE_IMAGE_TAG`: the successful CodeBuild source-version tag.
- `REPLACE_APPLICATION_SECRET_ARN`: the complete ARN of
  `travel/dev/application`, without a JSON-key suffix.
- `REPLACE_TRAVEL_API_URL`: the private URL that the Streamlit task can resolve.

The application secret must contain every JSON key referenced by the MCP task
definition. ECS fails task startup when a referenced secret or JSON key is
missing. Remove an optional secret entry from the task definition if that
provider is intentionally disabled.

`taskdef.backend.json` is the initial Travel API definition. The backend image
does not have one universal command: document, monitoring, eval,
communication, notification, search, operations, webhook, and orchestration
services all reuse it with their own command, port, environment, health check,
and least-privilege task role. Create those service-specific definitions when
the ECS runtime services are provisioned; do not point the API at nonexistent
service-discovery names and expect the complete workflow to operate.

Create the three CloudWatch log groups referenced by the templates before
starting tasks:

- `/ecs/travel-dev/frontend`
- `/ecs/travel-dev/backend`
- `/ecs/travel-dev/mcp`

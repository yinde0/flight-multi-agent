from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AWS_DEPLOY = ROOT / "deploy" / "aws"

EXPECTED_BUILDS = {
    "frontend": ("Dockerfile.frontend", "flight-multi-agent-frontend"),
    "backend": ("Dockerfile.backend", "flight-multi-agent-backend"),
    "mcp": ("Dockerfile.mcp", "flight-multi-agent-mcp"),
}


def test_buildspecs_test_then_publish_commit_tagged_images_only() -> None:
    for component, (dockerfile, repository) in EXPECTED_BUILDS.items():
        text = (AWS_DEPLOY / f"buildspec.{component}.yml").read_text()

        assert f"IMAGE_DOCKERFILE: {dockerfile}" in text
        assert f"IMAGE_REPOSITORY_NAME: {repository}" in text
        assert "python -m ruff check" in text
        assert "python -m pytest -q" in text
        assert "python -m travel_eval.runner" in text
        assert 'IMAGE_TAG="${CODEBUILD_RESOLVED_SOURCE_VERSION}"' in text
        assert "aws ecr get-login-password" in text
        assert "docker build" in text
        assert "docker push" in text
        assert "aws ecs" not in text


def test_task_definitions_are_fargate_json_templates_without_plaintext_secrets() -> None:
    for component in EXPECTED_BUILDS:
        path = AWS_DEPLOY / f"taskdef.{component}.json"
        definition = json.loads(path.read_text())

        assert definition["networkMode"] == "awsvpc"
        assert definition["requiresCompatibilities"] == ["FARGATE"]
        assert definition["executionRoleArn"].endswith(
            ":role/travel-dev-ecs-execution-role"
        )
        assert len(definition["containerDefinitions"]) == 1

        container = definition["containerDefinitions"][0]
        assert "REPLACE_IMAGE_TAG" in container["image"]
        assert container["logConfiguration"]["logDriver"] == "awslogs"

        for secret in container.get("secrets", []):
            assert secret["valueFrom"].startswith("REPLACE_APPLICATION_SECRET_ARN:")


def test_mcp_is_the_only_initial_template_with_provider_credentials() -> None:
    frontend = json.loads((AWS_DEPLOY / "taskdef.frontend.json").read_text())
    backend = json.loads((AWS_DEPLOY / "taskdef.backend.json").read_text())
    mcp = json.loads((AWS_DEPLOY / "taskdef.mcp.json").read_text())

    assert "secrets" not in frontend["containerDefinitions"][0]
    assert "secrets" not in backend["containerDefinitions"][0]

    mcp_secret_names = {
        item["name"] for item in mcp["containerDefinitions"][0]["secrets"]
    }
    assert {
        "MISTRAL_API_KEY",
        "AviationStack_API_KEY",
        "OpenWeatherMap_API_KEY",
        "DUFFEL_TOKEN",
        "TWILIO_API_SECRET",
        "AZURE_API_KEY",
        "LANGSMITH_API_KEY",
    } <= mcp_secret_names

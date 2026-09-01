from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

COMPOSE_CASES = [
    ("production", []),
    ("activation replay", ["compose.activation-test.yaml"]),
    ("agency demo", ["compose.agency-demo.yaml"]),
    ("communication replay", ["compose.communication-test.yaml"]),
    ("document LLM replay", ["compose.test.yaml", "compose.document-llm-test.yaml"]),
    ("Duffel replay", ["compose.duffel-test.yaml"]),
    ("Eval reasoning", ["compose.eval-reasoning.yaml"]),
    ("LangSmith development", ["compose.langsmith-development.yaml"]),
    ("LangSmith export", ["compose.langsmith.yaml"]),
    ("notification replay", ["compose.notification-test.yaml"]),
    ("observability", ["compose.observability.yaml"]),
    ("operations replay", ["compose.operations-test.yaml"]),
    ("reliability replay", ["compose.reliability-test.yaml"]),
    ("search replay", ["compose.search-test.yaml"]),
    ("document replay", ["compose.test.yaml"]),
    ("trace replay", ["compose.trace-test.yaml"]),
    ("Twilio replay", ["compose.twilio-test.yaml"]),
    ("weather replay", ["compose.weather-test.yaml"]),
]


def validate_case(name: str, overlays: list[str], env: dict[str, str]) -> None:
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.example",
        "-f",
        "compose.yaml",
    ]
    for overlay in overlays:
        command.extend(["-f", overlay])
    command.extend(["config", "--quiet"])

    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Compose validation failed for {name}")
    print(f"PASS: {name}")


def main() -> int:
    env = os.environ.copy()
    env.setdefault("GRAFANA_ADMIN_PASSWORD", "ci-compose-validation-only")
    env.setdefault("LANGSMITH_API_KEY", "ci-compose-validation-only")
    env.setdefault("MISTRAL_API_KEY", "ci-compose-validation-only")

    try:
        for name, overlays in COMPOSE_CASES:
            validate_case(name, overlays, env)
    except (FileNotFoundError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

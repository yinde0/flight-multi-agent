from __future__ import annotations

import argparse
import json
import time

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
TRACE_NAME = "document.parse"


def find_trace(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if value.get("name") == TRACE_NAME:
            return value
        for child in value.values():
            found = find_trace(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_trace(child)
            if found is not None:
                return found
    return None


def trace_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            names.add(value["name"])
        for child in value.values():
            names.update(trace_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(trace_names(child))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Send one synthetic document parse through the development stack "
            "and verify its input/output trace in LangSmith."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    config = dotenv_values(ROOT / ".env")
    api_key = str(config.get("LANGSMITH_API_KEY") or "").strip()
    project = str(config.get("LANGSMITH_PROJECT") or "").strip()
    endpoint = str(config.get("LANGSMITH_ENDPOINT") or "").strip().rstrip("/")
    if not api_key or not project or not endpoint:
        raise RuntimeError("Required LangSmith configuration is missing")

    pdf_path = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"
    started_at = datetime.now(timezone.utc)
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.post(
            f"{args.base_url.rstrip('/')}/v1/documents/parse",
            files={
                "file": (
                    pdf_path.name,
                    pdf_path.read_bytes(),
                    "application/pdf",
                )
            },
            data={
                "trip_id": "trip-langsmith-development",
                "traveler_ref": "traveler-synthetic-langsmith",
                "fixture_id": "doc-direct-clean-langsmith",
            },
        )
        response.raise_for_status()
        parse_result = response.json()

        api_root = endpoint
        if not api_root.endswith("/api/v1"):
            api_root = f"{api_root}/api/v1"
        headers = {"X-API-Key": api_key}
        project_response = client.get(
            f"{api_root}/sessions",
            headers=headers,
            params={"name": project},
        )
        project_response.raise_for_status()
        projects = project_response.json()
        record = projects[0] if isinstance(projects, list) and projects else projects
        project_id = str(record.get("id") or "")
        if not project_id:
            raise RuntimeError("Configured LangSmith project was not found")

        traced_run: dict[str, Any] | None = None
        recent_names: set[str] = set()
        query_status = 0
        for _ in range(8):
            query = client.post(
                f"{api_root}/runs/query",
                headers=headers,
                json={
                    "session": [project_id],
                    "filter": f'eq(name, "{TRACE_NAME}")',
                    "start_time": (
                        started_at - timedelta(seconds=5)
                    ).isoformat(),
                    "select": [
                        "id",
                        "name",
                        "run_type",
                        "start_time",
                        "inputs",
                        "outputs",
                    ],
                    "limit": 10,
                },
            )
            query_status = query.status_code
            query.raise_for_status()
            traced_run = find_trace(query.json())
            if traced_run is not None:
                break
            time.sleep(2)

        if traced_run is None:
            recent = client.post(
                f"{api_root}/runs/query",
                headers=headers,
                json={
                    "session": [project_id],
                    "start_time": (
                        started_at - timedelta(seconds=5)
                    ).isoformat(),
                    "select": ["id", "name", "run_type", "start_time"],
                    "limit": 100,
                },
            )
            recent.raise_for_status()
            recent_names = trace_names(recent.json())

    inputs_visible = bool((traced_run or {}).get("inputs"))
    outputs_visible = bool((traced_run or {}).get("outputs"))
    report = {
        "passed": (
            parse_result.get("status") == "parsed"
            and traced_run is not None
            and inputs_visible
            and outputs_visible
        ),
        "synthetic_fixture": pdf_path.name,
        "parse_status": parse_result.get("status"),
        "trace_name": TRACE_NAME,
        "trace_verified": traced_run is not None,
        "input_visible": inputs_visible,
        "output_visible": outputs_visible,
        "query_status": query_status,
        "recent_trace_names": sorted(recent_names)[:30],
        "trace_content_printed": False,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class TravelApiError(RuntimeError):
    """A user-safe failure returned by the travel orchestration API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class TravelApiClient:
    """Small typed boundary between Streamlit and the public travel API."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 150.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def health(self) -> bool:
        try:
            response = self._client.get("/health/live", timeout=2.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def activate_trip(
        self,
        *,
        document_bytes: bytes,
        filename: str,
        trip_id: str,
        traveler_ref: str,
        fixture_id: str,
        phone_e164: str | None = None,
        sms_consent: bool = False,
    ) -> dict[str, Any]:
        data = {
            "trip_id": trip_id,
            "traveler_ref": traveler_ref,
            "fixture_id": fixture_id,
            "sms_consent": str(sms_consent).lower(),
        }
        if phone_e164:
            data["phone_e164"] = phone_e164
        return self._request_json(
            "POST",
            "/v1/trips/activate",
            data=data,
            files={
                "file": (filename, document_bytes, "application/pdf"),
            },
        )

    def get_trip(self, trip_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/trips/{trip_id}")

    def document_status(self, trip_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/trips/{trip_id}/document-status")

    def _request_json(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise TravelApiError(
                "This is taking longer than expected. Please try again.",
                retryable=True,
            ) from error
        except httpx.HTTPError as error:
            raise TravelApiError(
                "We cannot reach the travel service right now. Please try again shortly.",
                retryable=True,
            ) from error

        if response.is_error:
            raise self._safe_http_error(response)

        try:
            payload = response.json()
        except ValueError as error:
            raise TravelApiError(
                "The travel service returned an unexpected response. Please try again.",
                status_code=response.status_code,
                retryable=True,
            ) from error
        if not isinstance(payload, Mapping):
            raise TravelApiError(
                "The travel service returned an unexpected response. Please try again.",
                status_code=response.status_code,
                retryable=True,
            )
        return dict(payload)

    @staticmethod
    def _safe_http_error(response: httpx.Response) -> TravelApiError:
        messages = {
            404: "We could not find that trip.",
            409: "This ticket conflicts with a trip already being watched. Start a new trip and try again.",
            413: "That PDF is larger than 5 MB. Please upload a smaller copy.",
            415: "Please upload a valid PDF ticket or itinerary.",
        }
        if response.status_code in messages:
            return TravelApiError(
                messages[response.status_code],
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            return TravelApiError(
                "One of our travel services is temporarily unavailable. Please try again shortly.",
                status_code=response.status_code,
                retryable=True,
            )
        return TravelApiError(
            "We could not process that request. Please check the ticket and try again.",
            status_code=response.status_code,
        )

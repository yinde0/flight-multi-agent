from __future__ import annotations

from flight_agent.notification_contracts import NotificationSubmissionFailure


class NotificationSubmissionError(RuntimeError):
    """Carry only the sanitized provider failure through application boundaries."""

    def __init__(self, failure: NotificationSubmissionFailure) -> None:
        self.failure = failure
        super().__init__(failure.error_code)


def rejected_submission(*, http_status: int, payload: object) -> NotificationSubmissionFailure:
    data = payload if isinstance(payload, dict) else {}
    code = data.get("code")
    if (
        isinstance(code, bool)
        or not str(code).isascii()
        or not str(code).isdigit()
        or not 100 <= int(code) <= 999999
    ):
        code = None
    else:
        code = int(code)
    message = str(data.get("message") or "").lower()
    retryable = http_status == 429 or http_status >= 500
    remediation = "retry_later" if retryable else "provider_rejected"
    if not retryable:
        if "trial" in message and any(
            word in message
            for word in ("custom", "predefined", "pre-defined", "template", "body")
        ):
            remediation = "upgrade_or_use_trial_template"
        elif code == 21608:
            remediation = "verify_recipient"
        elif code == 20003 or http_status in {401, 403}:
            remediation = "check_credentials"
        elif code in {21606, 21607}:
            remediation = "check_sender"
    return NotificationSubmissionFailure(
        error_code=f"TWILIO_{code}" if code is not None else f"TWILIO_HTTP_{http_status}",
        retryable=retryable,
        http_status=http_status,
        remediation=remediation,
    )

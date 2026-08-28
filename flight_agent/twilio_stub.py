from __future__ import annotations

import base64
import hashlib
import os
import threading

from fastapi import FastAPI, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from twilio.request_validator import RequestValidator

import httpx


app = FastAPI(title="Twilio SMS Stub", version="0.1.0")
_lock = threading.Lock()
_messages: list[dict[str, str | bool]] = []


class CallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_sid: str = Field(pattern=r"^(?:SM|MM)[0-9a-fA-F]{32}$")
    message_status: str
    error_code: int | None = None
    valid_signature: bool = True


def _expected_authorization() -> str:
    username = os.getenv("TWILIO_STUB_API_KEY", "")
    password = os.getenv("TWILIO_STUB_API_SECRET", "")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    return f"Basic {token}"


@app.get("/health/live")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/2010-04-01/Accounts/{account_sid}/Messages.json", status_code=201)
async def create_message(
    account_sid: str,
    request: Request,
    To: str = Form(...),
    Body: str = Form(...),
    MessagingServiceSid: str | None = Form(default=None),
    From: str | None = Form(default=None),
    StatusCallback: str | None = Form(default=None),
) -> dict[str, str]:
    auth_valid = request.headers.get("authorization") == _expected_authorization()
    account_valid = account_sid == os.getenv("TWILIO_STUB_ACCOUNT_SID", "")
    sender_valid = (
        MessagingServiceSid
        == os.getenv("TWILIO_STUB_MESSAGING_SERVICE_SID", "")
        or From == os.getenv("TWILIO_STUB_FROM_NUMBER", "")
    )
    if not auth_valid or not account_valid or not sender_valid:
        raise HTTPException(status_code=401, detail="Invalid synthetic Twilio request")
    delivery_id = f"SM{hashlib.sha256(f'{To}:{Body}'.encode()).hexdigest()[:32]}"
    with _lock:
        _messages.append(
            {
                "sid": delivery_id,
                "to_sha256": hashlib.sha256(To.encode("utf-8")).hexdigest(),
                "body": Body,
                "auth_valid": auth_valid,
                "account_valid": account_valid,
                "sender_valid": sender_valid,
                "status_callback": StatusCallback or "",
            }
        )
    return {"sid": delivery_id, "status": "queued"}


@app.post("/v1/test/callback")
async def send_callback(callback: CallbackRequest) -> dict[str, object]:
    with _lock:
        message = next(
            (
                dict(item)
                for item in _messages
                if item.get("sid") == callback.message_sid
            ),
            None,
        )
    if message is None:
        raise HTTPException(status_code=404, detail="Synthetic message not found")
    callback_url = str(message.get("status_callback") or "")
    if not callback_url:
        raise HTTPException(status_code=409, detail="No callback URL was supplied")
    params = {
        "AccountSid": os.getenv("TWILIO_STUB_ACCOUNT_SID", ""),
        "MessageSid": callback.message_sid,
        "MessageStatus": callback.message_status,
    }
    if callback.error_code is not None:
        params["ErrorCode"] = str(callback.error_code)
    signature = RequestValidator(
        os.getenv("TWILIO_STUB_AUTH_TOKEN", "")
    ).compute_signature(callback_url, params)
    if not callback.valid_signature:
        signature = "invalid-synthetic-signature"
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            callback_url,
            data=params,
            headers={"X-Twilio-Signature": signature},
        )
    return {
        "callback_http_status": response.status_code,
        "message_status": callback.message_status,
        "signature_valid": callback.valid_signature,
    }


@app.post("/v1/test/reset")
async def reset() -> dict[str, int]:
    with _lock:
        _messages.clear()
    return {"message_count": 0}


@app.get("/v1/test/audit")
async def audit() -> dict[str, object]:
    with _lock:
        messages = list(_messages)
    last = messages[-1] if messages else {}
    return {
        "message_count": len(messages),
        "last_to_sha256": last.get("to_sha256"),
        "last_body": last.get("body"),
        "last_message_sid": last.get("sid"),
        "status_callback_supplied": bool(last.get("status_callback")),
        "auth_valid": last.get("auth_valid", False),
        "account_valid": last.get("account_valid", False),
        "sender_valid": last.get("sender_valid", False),
    }

from __future__ import annotations

import hashlib
import os

from typing import Any, Protocol

import boto3

from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from flight_agent.contracts import DocumentMetadata
from flight_agent.trip_contracts import DocumentObjectRef


class DocumentStore(Protocol):
    def ensure_bucket(self) -> None: ...

    def put_pdf(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> DocumentObjectRef: ...

    def verify(self, document: DocumentObjectRef) -> bool: ...


class S3DocumentStore:
    """Idempotent, content-addressed PDF storage using the S3 API."""

    def __init__(
        self,
        *,
        bucket: str,
        client: BaseClient,
        region: str,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._region = region

    @classmethod
    def from_environment(cls) -> "S3DocumentStore":
        region = os.getenv("AWS_REGION", "eu-west-2")
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )
        return cls(
            bucket=os.getenv("ITINERARY_BUCKET_NAME", "travel-itineraries"),
            client=client,
            region=region,
        )

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise

        arguments: dict[str, Any] = {"Bucket": self._bucket}
        if self._region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {
                "LocationConstraint": self._region
            }
        self._client.create_bucket(**arguments)

    def put_pdf(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> DocumentObjectRef:
        digest = hashlib.sha256(document_bytes).hexdigest()
        if digest != metadata.sha256:
            raise ValueError("Document bytes do not match trusted SHA-256 metadata")
        key = f"trips/{metadata.trip_id}/documents/{digest}.pdf"
        try:
            existing = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            stored_digest = (existing.get("Metadata") or {}).get("sha256")
            if stored_digest != digest:
                raise ValueError("Existing S3 object checksum metadata does not match")
            return DocumentObjectRef(
                bucket=self._bucket,
                key=key,
                sha256=digest,
                etag=str(existing.get("ETag") or "").strip('"') or None,
            )

        response = self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=document_bytes,
            ContentType="application/pdf",
            Metadata={
                "sha256": digest,
                "fixture-id": metadata.fixture_id,
            },
        )
        return DocumentObjectRef(
            bucket=self._bucket,
            key=key,
            sha256=digest,
            etag=str(response.get("ETag") or "").strip('"') or None,
        )

    def verify(self, document: DocumentObjectRef) -> bool:
        if document.bucket != self._bucket:
            return False
        try:
            response = self._client.head_object(
                Bucket=document.bucket, Key=document.key
            )
        except ClientError:
            return False
        return (response.get("Metadata") or {}).get("sha256") == document.sha256

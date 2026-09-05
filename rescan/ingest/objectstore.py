"""Object storage the resumes are pulled from.

Resumes for a hiring round live in an S3-compatible bucket under a per-job
prefix — `<prefix>/<jobId>/...` — written there by whatever collects
applications. The service pulls them from that prefix on request; it never
lists the whole bucket.

`S3ObjectStore` speaks to anything with an S3 API (MinIO, Cloudflare R2, AWS)
through boto3. `LocalObjectStore` is a directory with the same layout, for
development and tests, in the same way the stub stands in for inference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rescan.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObjectRef:
    key: str
    size: int


class ObjectStore(Protocol):
    def list_objects(self, prefix: str) -> list[ObjectRef]: ...

    def get_object(self, key: str) -> bytes: ...


class ObjectStoreError(RuntimeError):
    pass


class LocalObjectStore:
    """A directory laid out like the bucket: <root>/<prefix>/<jobId>/<file>."""

    name = "local"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else settings.local_object_store_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def list_objects(self, prefix: str) -> list[ObjectRef]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(
            (
                ObjectRef(key=str(path.relative_to(self.root)).replace("\\", "/"), size=path.stat().st_size)
                for path in base.rglob("*")
                if path.is_file()
            ),
            key=lambda ref: ref.key,
        )

    def get_object(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ObjectStoreError(f"key {key!r} escapes the store root")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ObjectStoreError(f"cannot read {key!r}: {exc}") from exc

    def put_object(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class S3ObjectStore:
    """Any S3-compatible endpoint via boto3. Path-style addressing so MinIO
    and other self-hosted stores work without virtual-host DNS."""

    name = "s3"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str | None = None,
        client=None,
    ) -> None:
        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise ObjectStoreError("RESCAN_S3_BUCKET is not set")
        if client is not None:
            self.client = client
            return
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ObjectStoreError("boto3 is required for the s3 object store") from exc
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or settings.s3_endpoint_url or None,
            aws_access_key_id=access_key_id or settings.s3_access_key_id or None,
            aws_secret_access_key=secret_access_key or settings.s3_secret_access_key or None,
            region_name=region or settings.s3_region or None,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )

    def list_objects(self, prefix: str) -> list[ObjectRef]:
        refs: list[ObjectRef] = []
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    refs.append(ObjectRef(key=item["Key"], size=int(item.get("Size", 0))))
        except Exception as exc:  # botocore raises a family of exceptions
            raise ObjectStoreError(f"cannot list s3://{self.bucket}/{prefix}: {exc}") from exc
        return refs

    def get_object(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:
            raise ObjectStoreError(f"cannot read s3://{self.bucket}/{key}: {exc}") from exc


def build_object_store(kind: str | None = None) -> ObjectStore:
    kind = (kind or settings.object_store).lower()
    if kind == "s3":
        return S3ObjectStore()
    if kind == "local":
        return LocalObjectStore()
    raise ValueError(f"unknown object store {kind!r}; use 's3' or 'local'")

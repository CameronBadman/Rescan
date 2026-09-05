"""Pull one job's resumes out of the object store."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from rescan.config import settings
from rescan.ingest.objectstore import ObjectRef, ObjectStore, ObjectStoreError
from rescan.ingest.uploads import MAX_UNPACKED_BYTES, expand_uploads, is_usable

log = logging.getLogger(__name__)


@dataclass
class BucketPull:
    prefix: str
    documents: list[tuple[str, bytes]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def bytes_pulled(self) -> int:
        return sum(len(data) for _, data in self.documents)


def job_prefix(job_id: str, prefix: str | None = None) -> str:
    base = settings.s3_prefix if prefix is None else prefix
    base = base.strip("/")
    return f"{base}/{job_id.strip('/')}/" if base else f"{job_id.strip('/')}/"


def pull_job_documents(
    store: ObjectStore,
    job_id: str,
    *,
    prefix: str | None = None,
    max_bytes: int = MAX_UNPACKED_BYTES,
) -> BucketPull:
    """List `<prefix>/<jobId>/`, download every usable document, expand archives.

    Objects that are not resumes (a manifest, a thumbnail) are skipped and
    reported rather than failing the pull; a document that cannot be read is
    reported the same way so the recruiter can see what did not arrive.
    """
    where = job_prefix(job_id, prefix)
    pull = BucketPull(prefix=where)
    refs: list[ObjectRef] = store.list_objects(where)

    raw: list[tuple[str, bytes]] = []
    pulled = 0
    for ref in refs:
        name = Path(ref.key).name
        if not (is_usable(ref.key) or ref.key.lower().endswith(".zip")):
            pull.skipped.append({"key": ref.key, "reason": "not a supported document type"})
            continue
        if pulled + ref.size > max_bytes:
            pull.skipped.append({"key": ref.key, "reason": "size limit for one job reached"})
            continue
        try:
            data = store.get_object(ref.key)
        except ObjectStoreError as exc:
            log.warning("could not read %s: %s", ref.key, exc)
            pull.skipped.append({"key": ref.key, "reason": str(exc)})
            continue
        pulled += len(data)
        raw.append((name, data))
        pull.keys.append(ref.key)

    pull.documents = expand_uploads(raw, max_bytes=max_bytes)
    return pull

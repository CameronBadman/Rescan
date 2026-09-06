import io
import json
import time
import zipfile

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from rescan.api.main import app, state
from rescan.config import settings
from rescan.ingest import (
    LocalObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    build_object_store,
    job_prefix,
    pull_job_documents,
)

ROLE = {"title": "Senior Backend Engineer", "required_skills": ["Python"], "min_years_experience": 3}


def seed_local(root, job_id, samples):
    store = LocalObjectStore(root)
    for path in sorted(samples.glob("*.txt")):
        store.put_object(f"jobs/{job_id}/{path.name}", path.read_bytes())
    store.put_object(f"jobs/{job_id}/manifest.json", b"{}")
    store.put_object(f"jobs/{job_id}/.DS_Store", b"junk")
    store.put_object("jobs/other-job/999.txt", b"not ours")
    return store


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------


def test_job_prefix_layout():
    assert job_prefix("abc") == "jobs/abc/"
    assert job_prefix("/abc/", prefix="/tenants/x/") == "tenants/x/abc/"
    assert job_prefix("abc", prefix="") == "abc/"


def test_local_store_lists_only_the_job_prefix(tmp_path, samples):
    store = seed_local(tmp_path, "round-1", samples)
    keys = [ref.key for ref in store.list_objects("jobs/round-1/")]
    assert all(key.startswith("jobs/round-1/") for key in keys)
    assert "jobs/other-job/999.txt" not in keys
    assert store.list_objects("jobs/nothing/") == []


def test_local_store_refuses_to_escape_its_root(tmp_path):
    store = LocalObjectStore(tmp_path / "bucket")
    with pytest.raises(ObjectStoreError):
        store.get_object("../../etc/passwd")


def test_pull_skips_non_documents_and_expands_archives(tmp_path, samples):
    store = seed_local(tmp_path, "round-2", samples)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("batch/extra.txt", b"Extra Person\nSkills: Go\n")
        archive.writestr("__MACOSX/._x", b"junk")
    store.put_object("jobs/round-2/batch.zip", buffer.getvalue())

    pull = pull_job_documents(store, "round-2")
    names = {name for name, _ in pull.documents}
    assert "001_priya_nair.txt" in names and "extra.txt" in names
    assert "manifest.json" not in names and ".DS_Store" not in names
    assert {s["key"] for s in pull.skipped} == {"jobs/round-2/manifest.json", "jobs/round-2/.DS_Store"}
    assert pull.bytes_pulled > 0


def test_pull_enforces_the_size_limit(tmp_path, samples):
    store = seed_local(tmp_path, "round-3", samples)
    pull = pull_job_documents(store, "round-3", max_bytes=1500)
    assert pull.documents, "at least the first document fits"
    assert any("size limit" in s["reason"] for s in pull.skipped)


@mock_aws
def test_s3_store_against_the_s3_api(samples):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="resumes")
    for path in sorted(samples.glob("*.txt"))[:3]:
        client.put_object(Bucket="resumes", Key=f"jobs/s3-job/{path.name}", Body=path.read_bytes())
    client.put_object(Bucket="resumes", Key="jobs/s3-job/notes.exe", Body=b"nope")

    store = S3ObjectStore(bucket="resumes", client=client)
    refs = store.list_objects("jobs/s3-job/")
    assert len(refs) == 4 and all(ref.size > 0 for ref in refs)
    assert b"PRIYA" in store.get_object("jobs/s3-job/001_priya_nair.txt")

    pull = pull_job_documents(store, "s3-job")
    assert len(pull.documents) == 3
    assert pull.skipped == [{"key": "jobs/s3-job/notes.exe", "reason": "not a supported document type"}]

    with pytest.raises(ObjectStoreError):
        store.get_object("jobs/s3-job/missing.txt")


@mock_aws
def test_s3_store_paginates_large_prefixes():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="big")
    for index in range(1203):
        client.put_object(Bucket="big", Key=f"jobs/j/{index:04d}.txt", Body=b"x")
    assert len(S3ObjectStore(bucket="big", client=client).list_objects("jobs/j/")) == 1203


def test_s3_store_requires_a_bucket(monkeypatch):
    monkeypatch.setattr(settings, "s3_bucket", "")
    with pytest.raises(ObjectStoreError):
        S3ObjectStore()


def test_build_object_store_from_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "local_object_store_dir", tmp_path / "b")
    assert isinstance(build_object_store("local"), LocalObjectStore)
    with pytest.raises(ValueError):
        build_object_store("ftp")


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "b.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "local_object_store_dir", tmp_path / "bucket")
    # A developer's .env may point at a real bucket; these tests never should.
    monkeypatch.setattr(settings, "object_store", "local")
    with TestClient(app) as test_client:
        yield test_client


def wait_for(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/jobs/{job_id}/status").json()
        if status["status"] in {"complete", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_from_bucket_runs_end_to_end(client, tmp_path, samples):
    seed_local(tmp_path / "bucket", "round-7", samples)
    response = client.post(
        "/jobs/from-bucket",
        json={"job_id": "round-7", "role": ROLE, "plan": "Must have 3+ years experience. Nice to have: Kubernetes."},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"] == "round-7", "the bucket's job id is the Rescan job id"
    assert body["accepted_documents"] == 10
    assert {s["key"] for s in body["skipped"]} == {"jobs/round-7/manifest.json", "jobs/round-7/.DS_Store"}

    status = wait_for(client, "round-7")
    assert status["status"] == "complete" and status["total"] == 10
    events = [e for e in client.get("/jobs/round-7/audit").json()["entries"] if e["event"] == "bucket_pulled"]
    assert events and events[0]["detail"]["documents"] == 10 and len(events[0]["detail"]["keys"]) == 10
    assert client.get("/jobs/round-7/shortlist").json()["entries"]


def test_job_from_bucket_with_injected_store(client, samples):
    class Memory:
        name = "memory"

        def __init__(self):
            self.objects = {
                f"jobs/mem/{p.name}": p.read_bytes() for p in sorted(samples.glob("*.txt"))[:2]
            }

        def list_objects(self, prefix):
            from rescan.ingest import ObjectRef

            return [ObjectRef(k, len(v)) for k, v in self.objects.items() if k.startswith(prefix)]

        def get_object(self, key):
            return self.objects[key]

    state.object_store = Memory()
    response = client.post("/jobs/from-bucket", json={"job_id": "mem", "role": ROLE})
    assert response.status_code == 202 and response.json()["accepted_documents"] == 2
    assert wait_for(client, "mem")["status"] == "complete"


def test_job_from_bucket_rejects_bad_ids_and_empty_prefixes(client):
    assert client.post("/jobs/from-bucket", json={"job_id": "a/b", "role": ROLE}).status_code == 400
    assert client.post("/jobs/from-bucket", json={"job_id": "..", "role": ROLE}).status_code == 400
    response = client.post("/jobs/from-bucket", json={"job_id": "empty", "role": ROLE})
    assert response.status_code == 404
    assert "no usable documents" in response.json()["detail"]["message"]


def test_job_from_bucket_is_not_run_twice(client, tmp_path, samples):
    seed_local(tmp_path / "bucket", "dup", samples)
    assert client.post("/jobs/from-bucket", json={"job_id": "dup", "role": ROLE}).status_code == 202
    assert client.post("/jobs/from-bucket", json={"job_id": "dup", "role": ROLE}).status_code == 409
    wait_for(client, "dup")


def test_object_store_failure_is_a_502_not_a_crash(client):
    class Broken:
        name = "broken"

        def list_objects(self, prefix):
            raise ObjectStoreError("connection refused")

        def get_object(self, key):
            raise ObjectStoreError("connection refused")

    state.object_store = Broken()
    response = client.post("/jobs/from-bucket", json={"job_id": "x", "role": ROLE})
    assert response.status_code == 502

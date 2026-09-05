"""Exercise create/upload/submit/read/result against a running local backend.

python3 scripts/smoke.py [--count 4000] [resume.pdf image.png ...]
Uses the loopback development issuer; production callers should use their own token.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument("files", nargs="*")
parser.add_argument("--count", type=int, default=1)
parser.add_argument("--timeout", type=int, default=300)
parser.add_argument("--delete", action="store_true")
parser.add_argument("--check-all", action="store_true", help="Fetch and validate every result, not just the first page's first result")
args = parser.parse_args()
token = os.getenv("RESCAN_ACCESS_TOKEN") or json.load(urllib.request.urlopen("http://localhost:9000/token"))["access_token"]
base = os.getenv("RESCAN_API_URL", "http://localhost:8080").rstrip("/") + "/v1/jobs"


def api(path, method="GET", body=None, headers=None):
    request = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


files = [(Path(p).name, Path(p).read_bytes()) for p in args.files] or [(f"resume-{i}.txt", b"Jane Doe\nSoftware Engineer\nJava and PostgreSQL\n") for i in range(args.count)]
key = str(uuid4())
manifest = {"files": [{"filename": name, "sizeBytes": len(content)} for name, content in files]}
created = api("", "POST", manifest, {"Idempotency-Key": key})
job = created["jobId"]
print("Created job", job, "documents", len(files), flush=True)
page = created
while True:
    for upload in page["uploads"]:
        request = urllib.request.Request(upload["url"], method="PUT", data=files[upload["fileIndex"]][1],
            headers={k: ",".join(v) for k, v in upload["headers"].items()})
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
    if not page.get("nextCursor"):
        break
    page = api(f"/{job}/upload-urls?after={page['nextCursor']}", "POST")
api(f"/{job}/submit", "POST")
assert api("", "POST", manifest, {"Idempotency-Key": key})["jobId"] == job
deadline = time.monotonic() + args.timeout
while time.monotonic() < deadline:
    state = api(f"/{job}")
    if state["status"] == "UPLOADING":
        raise AssertionError("Upload verification failed; inspect document error_code values")
    if state["status"] in ("SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"):
        print(json.dumps(state, indent=2), flush=True)
        assert state["status"] == "SUCCEEDED", state
        document_page = api(f"/{job}/documents")
        documents = document_page["items"]
        if args.check_all:
            while document_page.get("nextCursor"):
                document_page = api(f"/{job}/documents?after={document_page['nextCursor']}")
                documents.extend(document_page["items"])
            assert len(documents) == len(files)
        for document in documents if args.check_all else documents[:1]:
            result = api(f"/{job}/documents/{document['id']}/result")
            output = json.load(urllib.request.urlopen(result["url"]))
            assert output["schemaVersion"] == 1 and output["text"].strip()
            print(json.dumps(output, indent=2), flush=True)
        if args.delete:
            assert api(f"/{job}", "DELETE")["status"] == "DELETING"
            try:
                api(f"/{job}/documents/{documents[0]['id']}/result")
            except urllib.error.HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError("Deleting job still exposes its result")
        break
    time.sleep(3)
else:
    raise TimeoutError("Job did not complete within smoke-test timeout")

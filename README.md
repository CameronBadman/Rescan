# Rescan

A Java resume-batching backend accepting **1–4,000 files per job**. Turso stores user/job/document metadata, S3 stores originals and normalized JSON, and Redis Streams distributes work. Apache Tika extracts native text; TrOCR Small handles images and scanned PDF pages.

## Local staging

Run the complete stack with Docker Compose:

```sh
docker compose up --build -d
python3 scripts/smoke.py --timeout 600 --delete
```

The initial worker build downloads pinned OCR weights and dependencies. Compose starts libSQL, Redis, LocalStack, a migration job, development authentication, the API, controller, and one worker. Local volumes persist between starts. Stop with `docker compose down`; only use `down -v` when you intend to discard that Compose project's local test data.

API: http://localhost:8080. Local test token: http://localhost:9000/token. The unauthenticated development issuer is loopback-published and must never be deployed publicly.

Production uses Cognito access tokens. See [browser integration](examples/upload.mjs), [OpenAPI](docs/openapi.yaml), and [result JSON schema](docs/result.schema.json). No frontend application is included.

## Data flow

Create a manifest → receive job ID/upload URLs → upload directly to S3 → submit → `VERIFYING` → `QUEUED` → processing → JSON results.

Verification runs asynchronously in resumable chunks. Missing or invalid uploads return the job to `UPLOADING`; inspect document `error_code` values, correct the uploads, and resubmit. Existing uploaded objects cannot be overwritten through the issued URLs.

Turso is authoritative. Short transactions and attempt tokens fence concurrent workers; a durable outbox restores Redis work after a queue failure. S3 writes occur outside database transactions, with conditional publication and orphan cleanup.

Limits remain 25 MiB/file, 5 GiB/batch, 50 pages, 12 million pixels/page, 10 MiB extracted text, and 15 minutes/document. Unsupported, encrypted, malformed, empty, and over-limit documents fail individually. Three attempts are allowed for transient failures.

Submitted files remain until deletion. Abandoned uploads expire after 24 hours. Deletion immediately blocks new result access and removes S3 versions/job records after a 20-minute grace period covering outstanding uploads and attempts. Already issued result links may remain usable for up to five minutes.

## Low-idle-cost AWS deployment

The Java API runs on Lambda through AWS Lambda Web Adapter and API Gateway. Java controller/verifier Lambdas coordinate Fargate workers. No RDS, ElastiCache, API Fargate service, ALB, or NAT gateway is required.

Workers target 1 vCPU/4 GB each, zero when idle and at most ten. An explicit demo session keeps one OCR-preloaded worker warm for two hours; expiry removes that minimum without interrupting active work. Workers have public outbound connectivity but no inbound security-group rules.

See [deployment](docs/deployment.md), [operations](docs/operations.md), and [verification evidence](docs/verification.md). Deployment requires external Turso/Upstash credentials and approved AWS/GitHub configuration; repository tests do not deploy AWS resources.

## Verification

```sh
mvn verify
python3 -m unittest discover -s worker/python -p 'test_*.py'
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra/bootstrap init -backend=false
terraform -chdir=infra/bootstrap validate
```

Use JDK 21 and Maven 3.9+. Integration tests require a running Docker-compatible engine and fail instead of silently skipping. Rootless Podman users can set `DOCKER_HOST` to its service socket and `TESTCONTAINERS_RYUK_DISABLED=true`.

OCR is printed-English extraction, not structured employment inference. The smaller model uses greedy decoding and may misread names, punctuation, and layouts. Benchmark accuracy and memory before enabling the production controller; no real-resume accuracy or completion-time SLA is claimed.

# Rescan

Batch resume processing: a Java API accepts **1–4,000 resumes**, returns a job ID, and exposes progress and JSON results. PostgreSQL holds users, IDs and job state; S3 holds originals and JSON; Redis Streams distributes documents to Java workers. Tika extracts document text. Images and scanned PDF pages use a pinned TrOCR vision encoder/text decoder through Python.

## Run locally

Requirements: JDK 21, Maven 3.9+, Docker Compose (or a compatible Podman service), Python 3 and OpenSSL. The first worker-image build downloads approximately 1.3 GB of model weights plus ML dependencies.

```sh
docker compose up -d
sh scripts/init-s3.sh
cp .env.example .env
set -a
. ./.env
set +a
mvn verify
```

Run each service in a separate terminal with the same environment:

```sh
# Local-only JWT issuer; never expose this development utility publicly.
python3 scripts/dev_auth.py

java -jar api/target/api-0.1.0-SNAPSHOT.jar
java -cp controller/target/controller-0.1.0-SNAPSHOT.jar dev.rescan.controller.Controller --loop
```

The API applies Flyway migrations at startup. Start it before the controller or workers. Stop local Java processes before rebuilding their jars. Obtain a development access token from `http://localhost:9000/token`; production uses Cognito authorization code + PKCE.

For native-document processing without installing the ML stack:

```sh
java -jar worker/target/worker-0.1.0-SNAPSHOT.jar
```

For image/scanned-PDF processing, build the complete worker image and run it against the local services (Linux host networking):

```sh
docker build --target worker -t rescan-worker:dev .
docker run --rm --network host --env-file .env rescan-worker:dev
```

The API is at `http://localhost:8080`. See [the frontend integration example](examples/upload.mjs) and [OpenAPI contract](docs/openapi.yaml). No frontend application is included.

## Processing behavior

- Tika: text-bearing PDFs, DOC/DOCX, RTF, ODT, HTML, plain text, and other supported document formats.
- ViT: PNG, JPEG, TIFF, BMP, and PDF pages without non-whitespace native text. Native PDF pages use Tika; page order is preserved.
- Image recognition: PaddleOCR detects lines, then `microsoft/trocr-base-printed` recognizes printed English text. Bounding boxes use page-image pixels. Reading order uses whitespace-based column detection and may require review for unusual layouts.
- The OCR checkpoint may uppercase text and misread proper names. The [measured synthetic benchmark](docs/ocr-benchmark.json) records this limitation; OCR results include a review warning.
- JSON contains text and metadata, not inferred skills or employment fields. See [the JSON schema](docs/result.schema.json).
- Unsupported, encrypted, corrupt, empty, and over-limit documents fail individually. Three attempts are allowed for transient failures; other resumes continue.
- Defaults: 25 MiB/file, 5 GiB/batch, 50 PDF/image pages, 12 million pixels/page, 10 MiB extracted text, and 15 minutes/document. File/page/time limits have corresponding environment overrides in the implementation; the hard batch count is 4,000.
- Signed upload URLs expire after 15 minutes and are paginated in groups of 100. They use conditional writes to prevent replacement. Submission verifies sizes and records S3 version IDs.
- Submitted data remains until deletion. Abandoned uploads expire after 24 hours. Deletion sweeps S3 immediately on the next controller cycle and again after 20 minutes to cover outstanding upload URLs. Previously issued result URLs can remain usable until their five-minute expiry or object deletion.

## AWS and verification

[Terraform deployment instructions](docs/deployment.md) cover the private data services, HTTPS API, Cognito, Fargate workers, and scheduled Java Lambda controller. No AWS resources are provisioned by tests.

```sh
mvn verify
python3 -m unittest discover -s worker/python -p 'test_*.py'
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
```

Java integration tests require a running container engine and fail rather than silently skip when it is absent. For rootless Podman, set `DOCKER_HOST` to its socket and `TESTCONTAINERS_RYUK_DISABLED=true`; test containers are closed by the test lifecycle.

See [operations and acceptance checks](docs/operations.md), including the CPU OCR benchmark and AWS scaling smoke test. Worker capacity reaches zero; the API, database, Redis, load balancer, and networking remain billable.

# Implementation verification — 2026-09-06

- Maven `verify`: **25 Java tests passed**, none skipped, using real PostgreSQL and Redis test containers. Coverage includes the 4,000-document manifest, persistence, submission and pagination; rejection at 4,001; owner isolation; duplicate completion; expired leases; retries; deletion fencing; password rotation; format routing; mixed PDFs; encrypted PDFs; and scaling decisions.
- Python reading-order suite: **2 tests passed**.
- Both API and worker container images built successfully with Java 21. The shaded Java Lambda artifact was built and exercised locally.
- Terraform provider initialization, formatting checks and validation passed. No AWS resources were provisioned.
- End-to-end local text and PNG jobs passed through signed S3 uploads, authenticated API submission, Redis, workers, PostgreSQL status updates, and signed JSON result retrieval.
- Packaged Java-to-Python image parsing passed with networking disabled and the non-root worker user.
- Authenticated deletion immediately blocked result API access. With the synthetic job's deletion timestamp advanced past the capability-expiry window, the controller removed its job record and all original/result S3 versions.
- The [OCR benchmark](ocr-benchmark.json) records measured timing, memory, and recognition errors. It is a synthetic fixture, not an accuracy guarantee for real resumes.

Not executed: deployment to AWS, actual Fargate scaling, a 4,000-document OCR throughput run, or accuracy evaluation on real customer resumes. The 4,000-document integration test mocks S3 HEAD responses; the separate end-to-end smoke tests use LocalStack S3. See `operations.md` for the AWS acceptance procedure.

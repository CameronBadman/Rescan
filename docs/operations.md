# Operations and acceptance

## Reliability

PostgreSQL is authoritative. The controller republishes queued/retryable documents that have not been enqueued recently. Redis stream delivery is at least once; duplicate entries cannot create two successful completions. If Redis loses its data, consumer-group initialization and database reconciliation rebuild work. Streams contain identifiers only and are bounded; exhausted processing failures also remain in PostgreSQL.

Workers heartbeat every twenty seconds with two-minute leases. A lost heartbeat kills parsing. Result publication holds the job lock, preventing deletion or a stale attempt from publishing afterward. Transient failures receive at most three attempts with 30-second and two-minute delays. A whole worker crash is recovered on the next controller cycle after lease expiry.

The controller runs once per minute. Desired capacity is `min(10, ceil(outstanding / 25))`; outstanding includes queued, processing, and retry-wait documents. It preserves capacity on dependency failure and waits five idle minutes before requesting zero. ECS task protection can delay actual scale-in while work finishes.

## Test coverage and smoke tests

`mvn verify` exercises real PostgreSQL/Redis containers, duplicate completion, expired leases, deletion fencing, retry exhaustion, owner isolation, upload validation, 4,000-document persistence/submission/pagination, parser fixtures, and scale-controller boundaries. S3 HEAD responses in batch service tests are mocked; use the following smoke workflow to check signed uploads against the real storage implementation.

1. Start local services, initialize the versioned bucket, and start the development issuer/API/controller/worker.
2. Upload a native text resume, a PNG, and a mixed PDF through `examples/upload.mjs`; verify all result URLs contain schema-valid JSON.
3. Resubmit with the same idempotency key. Confirm the same job ID and no duplicate document records.
4. Submit 4,001 manifest entries and expect HTTP 400. Submit 4,000 small documents and verify terminal counts sum to 4,000.
5. Stop a worker during processing and restart it; confirm recovery without duplicate success counts. Restart Redis with an empty queue and confirm queued documents are republished.
6. Delete a processing job. Confirm results become inaccessible and all S3 versions and job records disappear after the final sweep.

For AWS, begin with zero running workers, submit a batch, and observe workers starting after the next controller tick. Submit enough work to reach ten; confirm no more than ten worker tasks, including deployment behavior. After completion, verify desired count returns to zero after five idle minutes. Actual wall time also includes scheduling and Fargate startup.

## OCR measurement

Run `scripts/benchmark_ocr.py` inside the worker container to generate a synthetic printed-English fixture and report recognized text, character error rate, elapsed time, and peak process memory. It writes a standalone JSON report to a caller-selected output directory. This is a smoke benchmark, not evidence of accuracy across real resumes. For acceptance on real layouts, use consented/anonymized resume images with reviewed transcriptions, including columns, small fonts, rotation, and poor scans. No completion-time SLA is claimed.

The checked-in [local benchmark](ocr-benchmark.json) took 23.1 seconds and approximately 2.3 GiB peak RSS. Four-beam decoding preserved `PostgreSQL` spelling but returned uppercase text and misread `Doe` as `DUE`: case-sensitive CER was 67.4%, and case-insensitive CER was 2.17%. Case normalization is therefore a material limitation, not just a performance detail. ViT results include a review warning. Tika extraction retains native document text.

## Troubleshooting

- Jobs remain queued: inspect controller errors, Redis connectivity, database availability, and ECS task startup events.
- OCR fails: inspect model availability and container memory; models are downloaded only at build time. Increase the configurable timeout only after measuring the failing document.
- Upload verification fails: check missing objects, exact file sizes, S3 versioning, and URL expiry. File IDs use manifest indices, so duplicate filenames are supported.
- Pending deletion: allow twenty minutes for the upload capability expiry sweep; inspect S3 delete permissions and controller errors if it remains afterward.
- API 401: check issuer, access-token expiry, `token_use=access`, and Cognito `client_id`.

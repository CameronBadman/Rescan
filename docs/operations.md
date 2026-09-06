# Operations and acceptance

## Reliability and idle behavior

Turso is authoritative; Redis Streams is an at-least-once delivery mechanism. Workers claim using short write transactions and attempt tokens. Heartbeats run every 20 seconds, leases last two minutes, and transient failures get three attempts with 30/120-second retry delays. Lost leases fence old workers. Redis loss is repaired from the durable outbox and document state.

SQL-over-HTTP never blindly retries an uncertain mutation. Client retries of job creation must reuse the same idempotency key. Controller/worker reconciliation resolves uncertain outcomes from authoritative state.

S3 writes are outside database transactions. Before uploading an attempt result, the worker records a potential orphan. It only publishes the key in a valid, non-deleting job transaction. Cleanup checks that an orphan key is not referenced before removing it after 20 minutes.

The controller runs every minute, uses an expiring database lease, dispatches bounded batches, and requests `min(10, ceil(outstanding / 25))` workers. Dependencies failing before scaling preserve the existing desired count. Outside demo sessions it requests zero after 60 seconds empty; ECS protection can delay actual scale-in. Demo warming has a durable two-hour expiry.

Upload verification uses `VERIFYING`, generation tokens and 100-document checkpoints. A failure does not enqueue a partial batch. Validation failures return to `UPLOADING`, exposing `MISSING_UPLOAD` or `INVALID_UPLOAD` on document records. Correct and resubmit; duplicate filenames retain their original indices.

## Acceptance checks

- Run `mvn verify` against real libSQL/Redis containers, not PostgreSQL or an in-memory SQL substitute.
- Run Compose smoke tests with native text, PNG/JPEG/TIFF and scanned/mixed PDFs. Confirm non-empty schema-compatible JSON and correct page order.
- Verify 4,000 entries succeed and 4,001 fail. The database integration test checks the 4,000-entry lifecycle; a full 4,000-file AWS processing run is a separate paid load test.
- Exercise duplicate submission, ten concurrent claims, lease recovery, queue loss, deletion during verification and during S3 upload, transaction rollback and ambiguous responses.
- Confirm another Cognito user cannot access the job/documents/results. Validate the access-token issuer and client ID.
- In AWS, demonstrate zero → one → ten → zero. Confirm a demo session retains one idle worker and expiry removes the minimum. Never manually force termination of real customer work for testing.
- Delete synthetic jobs and verify the final sweep removes all object versions and job/document records. Already issued result URLs may remain usable until expiry.
- Exercise an older release manifest rollback before introducing real data.

## OCR benchmark

Run `scripts/benchmark_ocr.py` inside the image with network disabled, one CPU and 4 GB memory. It emits JSON on stdout for six synthetic fixtures (clean/small/blurred/JPEG/TIFF/multipage) including normalized character error rate, elapsed time and peak process RSS. The first fixture includes cold initialization; later fixtures reuse the small model.

Run the same script against the previous base-model image for a comparable accuracy reference. The historic single-fixture result remains in `ocr-benchmark.json`; do not compare its elapsed time directly with the new multi-fixture suite.

The measured [small-model comparison](ocr-small-benchmark.json) reduced OCR-process peak RSS from about 2.44 GiB to 1.08 GiB, but mean normalized character error rate increased from 2.17% to 7.23%. These are six synthetic fixtures, not real-resume accuracy estimates. Full staging worker memory observations and test limitations are in [verification](verification.md).

Synthetic tests are not a general accuracy guarantee. Before setting `worker_profile_verified=true`, inspect proper names, email addresses, small fonts, columns, noisy scans and page limits using consented/anonymized examples. Record the model revision, CPU/memory limit and actual costs. No GPU, per-token API charge, or inferred employment-field extraction is used.

## Cost model

At Sydney on-demand rates checked during planning, target worker compute is approximately US$0.06984 per worker-hour (1 vCPU + 4 GB), plus public IPv4, logs, storage and data transfer. Two demo hours add about US$0.14 of worker compute. This is not a per-batch quote: slower OCR, retries and cold starts increase total worker time.

Turso's free tier and Upstash usage pricing avoid managed RDS/ElastiCache node bills. Lambda has no provisioned concurrency, and the deployment has no ALB/NAT gateway. Check current provider pricing and usage dashboards before the event. AWS US$10/US$25 alerts require cost-tag activation and are not hard caps.

## Troubleshooting

- `VERIFYING` stuck: inspect verifier errors, object versions, per-document validation results, and invocation permissions.
- Queued jobs: inspect controller errors/lease, Redis connectivity and ECS task events. If the schedule is disabled, migrations and acceptance must precede activation.
- Worker not ready: inspect the model benchmark and pinned tokenizer dependencies. Preload has a 180-second deadline. Do not increase memory silently.
- Turso failures: check endpoint/token expiry, transaction timeouts, provider quotas and primary-region latency. No external I/O belongs inside write transactions.
- Redis failures: verify TLS and consumer-group/auto-claim support against a dedicated provider test database.
  Run the isolated-key probe with the provider's `REDIS_URL` in the environment: `java -cp controller/target/controller-0.1.0-SNAPSHOT.jar dev.rescan.common.Queue`. It checks publishing, consumer-group reads, auto-claim, acknowledgement/deletion and dead-letter insertion, then removes only its random probe keys. It incurs provider usage; it never flushes the database.
- Pending deletion: allow the 20-minute capability-expiry grace period, then inspect S3 delete permissions.
- API 401: check issuer, expiry, `token_use=access`, and Cognito `client_id`.

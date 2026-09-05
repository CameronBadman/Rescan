# Rescan storage on AWS

The bucket a job's resumes are pulled from, and an identity scoped to it.

```bash
cd infra/aws
terraform init && terraform apply
terraform output -raw rescan_env >> ../../.env   # bucket, region, prefix, service keys
aws s3 cp ./resumes/ s3://<bucket>/jobs/<jobId>/ --recursive
curl -s localhost:8080/jobs/from-bucket -H 'content-type: application/json' \
  -d '{"job_id": "<jobId>", "role": {"title": "..."}, "plan": "..."}'
```

What it creates, and why:

- **`rescan-resumes-<account id>`** in **ap-southeast-2 (Sydney)** — candidate
  documents stay in Australia. Private (public access blocked, bucket-owner
  enforced), encrypted at rest, versioned, TLS-only by bucket policy.
- **Retention.** Objects under `jobs/` expire after 90 days
  (`retention_days`), old versions after 7. Resumes are personal information
  collected for one hiring round; keeping them indefinitely is the wrong
  default.
- **`rescan-service` IAM user** with read access to `jobs/*` in this bucket
  and nothing else. Its keys are what Rescan runs on; the account's root
  credentials should never be in a `.env`. Whatever collects applications
  writes to the bucket under its own identity.

The layout is `jobs/<jobId>/<files>` — the same shape `RESCAN_S3_PREFIX` and
`POST /jobs/from-bucket` expect, and the same shape the local directory store
uses in development.

State is local (`terraform.tfstate`, gitignored). Fine for one operator; move
it to an S3 backend if more than one person applies.

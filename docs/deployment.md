# Low-cost AWS deployment

This replaces the earlier PostgreSQL/ElastiCache deployment. It assumes a fresh environment. **Do not apply against an existing production state without reviewing deletions and migrating its data.** No cloud resources are created by building or testing this repository.

## Prerequisites

- Existing AWS account, region `ap-southeast-2`, Route 53 hosted zone/API hostname, frontend origin and Cognito callback URL.
- Turso Cloud database, primary endpoint and database-scoped token. Start on the free plan; metadata only, not resume text.
- Upstash Redis database, preferably in the nearest available AWS region, with a TLS `rediss://` URL. Use pay-as-you-go for load tests. Run the Redis compatibility tests against a dedicated test database before using it for jobs.
- Store the Turso token and full Redis URL as two Secrets Manager string secrets. Terraform receives their ARNs, not their values. Secrets must use the default AWS-managed encryption key, or the task roles must receive explicit decrypt access to your selected KMS key.
- Check regional Fargate On-Demand quota and existing consumption leave at least ten vCPUs available. Check Lambda concurrency supports the reserved counts (API five, controller one, verifier two) plus the account's required unreserved capacity.
- Enable the `Project` cost-allocation tag; confirm the operations email subscription. Budget notifications are delayed alerts, not a hard cap.

External providers are deliberately provisioned outside AWS Terraform. Do not put their management API tokens in GitHub. Runtime credentials are retrieved from Secrets Manager and cached for one minute.

## One-time bootstrap

Use an administrator's short-lived AWS session, not long-lived access keys.

1. Review `infra/bootstrap`. Supply exact GitHub OIDC subjects for environments `release-publish`, `production-plan`, `production`, and `demo`. Verify whether your repository uses immutable repository/owner IDs in its subjects.
2. Reuse the account's existing GitHub OIDC provider when present.
3. Supply an administrator-reviewed project provisioning policy ARN. This is intentionally not generated as broad AdministratorAccess. It must allow managing this project's VPC/subnets/routes/security groups, S3 bucket, ECS cluster/service/task definitions, Lambda functions, API Gateway, Cognito, ACM/Route 53 records, logs/alarms/SNS/budget, and the five `rescan-*` runtime/execution roles. Scope named resources, Route 53 hosted-zone access, and regional/tagged EC2 resources. The bootstrap adds PassRole only for those five roles. The plan role uses AWS ReadOnlyAccess plus restricted state-lock/plan-artifact access; review that account-wide read scope.
4. Run `terraform -chdir=infra/bootstrap init`, plan, review, and apply. It creates private versioned state/artifact buckets, immutable ECR repositories, and four GitHub roles.
5. Copy `infra/bootstrap/backend.tf.example` to `infra/bootstrap/backend.tf`. Run bootstrap `init -migrate-state` with the created state bucket, key `bootstrap/terraform.tfstate`, region, `encrypt=true`, and `use_lockfile=true`. Keep the initial local backup securely until migration is verified; never commit it.
6. Initialize the application stack with the same bucket but key `production/terraform.tfstate`, using `infra/backend.hcl.example` as a guide.

State and saved plans are sensitive. Bucket encryption, versioning, TLS-only access and scoped permissions are configured; no DynamoDB lock table is needed.

## GitHub configuration

Merge reviewed implementation changes before releasing. These workflows accept only the repository's default branch.

Create the four environments named above. Require reviewer approval for `production`; restrict all four to the default branch. Configure repository variables (or repeat them in each environment):

| Variable | Meaning |
|---|---|
| `STATE_BUCKET`, `ARTIFACT_BUCKET` | Bootstrap outputs |
| `ECR_API`, `ECR_WORKER` | Bootstrap repository URLs |
| `PUBLISH_ROLE_ARN`, `PLAN_ROLE_ARN`, `APPLY_ROLE_ARN`, `DEMO_ROLE_ARN` | Bootstrap roles |
| `RESOURCE_NAME` | `rescan`, matching bootstrap |
| `API_DOMAIN`, `ROUTE53_ZONE_ID` | API DNS configuration |
| `FRONTEND_ORIGIN`, `AUTH_CALLBACK_URL` | Exact frontend URLs |
| `TURSO_DATABASE_URL` | HTTPS SQL endpoint |
| `TURSO_SECRET_ARN`, `REDIS_SECRET_ARN` | Existing runtime secrets |
| `OPERATIONS_EMAIL` | Confirmed notification destination |
| `CONTROLLER_FUNCTION` | `rescan-controller` |

No AWS access-key secrets are required. Protect the workflow definitions themselves through branch review. Runtime secrets are not required in CI or local staging.

## Release and activation

1. Run **publish release**. It runs tests, builds the API/worker once, exercises those images through Compose, and publishes their immutable digests plus the exact staging controller JAR and checksum. The release ID is source SHA + run ID + attempt.
2. Run **deploy approved release** with that release ID and `activate=false`. Review the saved plan in the restricted S3 artifact bucket before approving. The apply job verifies its checksum and applies exactly that plan.
3. The apply job invokes the migration action and checks API health. The controller schedule remains disabled, so workers stay at zero. It records a migration marker scoped to the database endpoint and schema checksum; activation refuses to plan without that marker. Recreating/restoring a database at the same endpoint requires rerunning the inactive deployment to revalidate its schema.
4. Run the synthetic OCR benchmark at 1 vCPU/4 GB; inspect accuracy and memory. Do not check `worker_profile_verified` merely because an image built successfully.
5. Run deployment again with the same release ID, `activate=true`, and `worker_profile_verified=true`, after review. Complete authenticated AWS acceptance using synthetic resumes before introducing real data.
6. Configure the frontend using Terraform's Cognito outputs; use authorization code + PKCE and an access token, not an ID token.

The worker-image contract includes the private OCR socket and preloaded model. Fargate health checks wait for preload readiness. Cold starts still include image download and model loading.

For cloud smoke tests, set `RESCAN_API_URL` and `RESCAN_ACCESS_TOKEN` in your shell, then run `python3 scripts/smoke.py --timeout 900 --delete`. Do not paste tokens into workflow inputs, logs, or issue comments. Cloud smoke tests write only synthetic job data but incur usage charges.

## Demo sessions and rollback

**demo worker session** starts/renews a two-hour warm minimum, or ends it with `enabled=false`. The schedule must be enabled first; the controller rejects warming otherwise. The controller owns desired count and caps it at ten; Terraform ignores that count. Session expiry requests zero after the idle window, not while documents remain outstanding. To disable processing later, first end warming, drain jobs and verify desired/running counts are zero; disabling the schedule alone does not terminate existing workers.

Roll back by deploying a prior release ID from an ancestor of the current default branch. Keep current compatible infrastructure and use the previous image digests/JAR from its manifest. The pipeline validates artifact checksums; it does not reverse schema migrations. Restore Turso data separately using a reviewed provider restore/export procedure.

Before enabling customer traffic, verify provider quotas, Redis command behavior, Cognito isolation, real S3 signed requests/deletion, AWS task protection/scaling, and the smaller model's recognition quality. These cannot be proved by local Compose alone.

## Cleanup

Stopping workers does not delete retained data. S3 objects/versions, ECR images, state, secrets, DNS, and external-provider usage can remain billable. Plan and review cleanup explicitly after the hackathon. Buckets do not force-delete data, and bootstrap state/artifact buckets have destroy protection. No automated database deletion or broad account cleanup is supplied.

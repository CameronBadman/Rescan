# AWS deployment

Terraform under `infra/` defines the entire backend. It requires an AWS account, a Route 53 hosted zone for the API domain, a frontend origin, and a Cognito callback URL. Implementation does not apply this configuration.

1. Run `mvn verify` to produce the API/worker jars and shaded Lambda artifact.
2. Configure AWS credentials in your normal credential chain. Create `infra/deployment.auto.tfvars` with the following non-secret inputs:

```hcl
region            = "ap-southeast-2"
api_domain        = "rescan-api.example.com"
route53_zone_id   = "YOUR_HOSTED_ZONE_ID"
frontend_origin   = "https://app.example.com"
auth_callback_url = "https://app.example.com/auth/callback"
```

3. Run `terraform -chdir=infra init`. For shared use, configure an encrypted remote state backend with locking before provisioning. Terraform state contains the generated Redis credential; do not commit state or publish plan files.
4. Bootstrap image repositories using `terraform -chdir=infra apply -target=aws_ecr_repository.images`. This intentionally creates only the repositories needed before ECS can start.
5. Build the `api` and `worker` Dockerfile targets for `linux/amd64`; log in to ECR using `aws ecr get-login-password`. Tag and push each image with a unique release identifier. The repositories enforce immutable tags.
6. Set `api_image` and `worker_image` in the tfvars file to the pushed image URIs, preferably digest-qualified. Run `terraform -chdir=infra plan -out=deployment.tfplan`, review it, then apply that plan.
7. Wait for the API health endpoint. The API runs Flyway migrations; the controller may alarm while the first API task initializes the schema. Workers begin at zero.
8. Configure the frontend from `api_url`, `cognito_issuer`, `cognito_client_id`, and `cognito_login_domain`. Use authorization code + PKCE and send the **access token**, not the ID token.
9. Subscribe an operations destination to `alarm_topic_arn`; the template creates the topic, not an unsolicited email subscription. Run the scaling smoke test in `operations.md`.

The Lambda package path defaults to `controller/target/controller-0.1.0-SNAPSHOT.jar`, relative to the Terraform directory. The controller—not a competing target-tracking policy—owns worker desired count, capped at ten. Terraform ignores changes to that desired count.

Defaults include multi-AZ PostgreSQL and Redis, one API task, one NAT gateway, private task/data subnets, HTTPS ingress, encrypted storage, RDS deletion protection, and final snapshots. A single NAT gateway is a cost/availability tradeoff; API redundancy and per-AZ NAT are future deployment choices. Zero workers does not mean zero infrastructure cost.

Database credentials are RDS-managed and retrieved from Secrets Manager when the pool opens each physical connection, so new connections follow secret rotation. The initial deployment uses the managed database user for migrations and application access. Before operating under a stricter database privilege model, provision separate migration and runtime roles. Rotate Redis credentials by rolling consumers after the secret update. Resume data is not included in application log messages, but retained database snapshots may contain job/user records until their seven-day backup retention expires.

For upgrades, publish new image digests, build the controller jar, and apply the reviewed plan. Workers protect active tasks for up to twenty minutes; deployments wait for protection expiry/completion. Force-stopped processing is recovered through PostgreSQL leases.

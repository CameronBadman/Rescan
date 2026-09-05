#!/usr/bin/env bash
set -euo pipefail
[[ "$RELEASE_ID" =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]]
aws s3 cp "s3://$ARTIFACT_BUCKET/releases/$RELEASE_ID/manifest.json" /tmp/release.json
mkdir -p controller/target
aws s3 cp "s3://$ARTIFACT_BUCKET/releases/$RELEASE_ID/controller.jar" controller/target/controller-0.1.0-SNAPSHOT.jar
JAR_SHA=$(jq -r .controller_sha256 /tmp/release.json)
[[ "$JAR_SHA" =~ ^[a-f0-9]{64}$ ]]
echo "$JAR_SHA  controller/target/controller-0.1.0-SNAPSHOT.jar" | sha256sum --check
jq '{api_image,worker_image}' /tmp/release.json > infra/release.auto.tfvars.json

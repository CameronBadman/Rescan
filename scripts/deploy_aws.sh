#!/usr/bin/env bash
# Deploy the current commit to the AWS API instance.
#
# Archives HEAD (tracked files only — never .env or local data), uploads it
# to the private artifacts bucket, and asks the instance over SSM to refresh:
# reinstall the package and restart the service. Needs an AWS session with
# access to the artifacts bucket and SSM; the instance itself has no SSH.
#
#   ./scripts/deploy_aws.sh            # deploy HEAD
#   REF=main ./scripts/deploy_aws.sh   # deploy another ref
set -euo pipefail

cd "$(dirname "$0")/.."
REF="${REF:-HEAD}"
TF="terraform -chdir=infra/aws"

BUCKET="$($TF output -json api | python3 -c 'import json,sys; print(json.load(sys.stdin)["artifacts"])')"
INSTANCE="$($TF output -json api | python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])')"
URL="$($TF output -json api | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
REGION="$($TF output -json bucket | python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])')"

archive="$(mktemp -t rescan-XXXXXX.zip)"
trap 'rm -f "$archive"' EXIT
git archive --format=zip -o "$archive" "$REF"
echo "uploading $(git rev-parse --short "$REF") ($(du -h "$archive" | cut -f1)) to s3://$BUCKET/rescan.zip"
aws s3 cp "$archive" "s3://$BUCKET/rescan.zip" --region "$REGION" --only-show-errors

echo "refreshing $INSTANCE over SSM"
command_id="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["/opt/rescan/refresh.sh"]' \
  --query 'Command.CommandId' --output text)"

for _ in $(seq 1 60); do
  status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" --instance-id "$INSTANCE" \
    --query Status --output text 2>/dev/null || echo Pending)"
  case "$status" in
    Success) echo "refresh succeeded"; break ;;
    Failed|Cancelled|TimedOut)
      echo "refresh $status" >&2
      aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" --instance-id "$INSTANCE" \
        --query StandardErrorContent --output text >&2
      exit 1 ;;
    *) sleep 5 ;;
  esac
done

for _ in $(seq 1 30); do
  if curl -fsS -m 10 "$URL/health" >/dev/null 2>&1; then
    echo "healthy: $URL/health"
    curl -sS -m 10 "$URL/health"; echo
    exit 0
  fi
  sleep 5
done
echo "API did not report healthy at $URL within 150s" >&2
exit 1

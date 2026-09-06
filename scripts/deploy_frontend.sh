#!/usr/bin/env bash
# Build the frontend against the deployed API and publish it.
#
# Bakes the API URL and the frontend's key into a static export, syncs it to
# the frontend bucket and invalidates CloudFront. Needs Node 20+, and an AWS
# session that can write the bucket and invalidate the distribution.
set -euo pipefail

cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/aws"
out() { $TF output -json "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)$2)"; }

API_URL="$(out api '["url"]')"
FRONTEND_KEY="$($TF output -raw frontend_env | sed -n 's/^NEXT_PUBLIC_RESCAN_API_KEY=//p')"
BUCKET="$(out frontend '["bucket"]')"
DIST="$(out frontend '["distribution_id"]')"
URL="$(out frontend '["url"]')"
REGION="$(out bucket '["region"]')"

cd frontend
[ -d node_modules ] || npm install --no-audit --no-fund
NEXT_PUBLIC_RESCAN_API_URL="$API_URL" NEXT_PUBLIC_RESCAN_API_KEY="$FRONTEND_KEY" npm run build
cd ..

echo "publishing frontend/out to s3://$BUCKET"
aws s3 sync frontend/out "s3://$BUCKET" --delete --region "$REGION" --only-show-errors
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" --query 'Invalidation.Id' --output text >/dev/null
echo "frontend: $URL  (API: $API_URL)"

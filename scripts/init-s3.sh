#!/bin/sh
set -eu
docker compose exec -T s3 awslocal s3api create-bucket --bucket rescan-local --region ap-southeast-2 --create-bucket-configuration LocationConstraint=ap-southeast-2
docker compose exec -T s3 awslocal s3api put-bucket-versioning --bucket rescan-local --versioning-configuration Status=Enabled
docker compose exec -T s3 awslocal s3api put-bucket-cors --bucket rescan-local --cors-configuration '{"CORSRules":[{"AllowedOrigins":["http://localhost:5173"],"AllowedMethods":["PUT","GET","HEAD"],"AllowedHeaders":["*"],"ExposeHeaders":["ETag","x-amz-version-id"]}]}'

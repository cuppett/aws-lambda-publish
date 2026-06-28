#!/usr/bin/env bash
# Register DDNS Lambda functions for automatic image updates on ddns-route53:latest pushes.
set -euo pipefail

PROFILE="${PROFILE:-cuppett}"
REGION="${REGION:-us-east-1}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)}"
TABLE="${TABLE:-ImageTagSubscriptions}"
REPO_TAG="${REPO_TAG:-ddns-route53:latest}"

for FN in ddns-authorizer ddns-update-handler ddns-checkip; do
  aws dynamodb put-item \
    --profile "$PROFILE" \
    --region "$REGION" \
    --table-name "$TABLE" \
    --item "{
      \"PK\": {\"S\": \"${REPO_TAG}\"},
      \"SK\": {\"S\": \"${ACCOUNT_ID}/${REGION}/${FN}\"},
      \"mode\": {\"S\": \"direct\"},
      \"updateStrategy\": {\"S\": \"code-only\"},
      \"target\": {\"M\": {
        \"accountId\": {\"S\": \"${ACCOUNT_ID}\"},
        \"region\": {\"S\": \"${REGION}\"},
        \"functionName\": {\"S\": \"${FN}\"}
      }}
    }"
  echo "Subscribed ${FN} to ${REPO_TAG}"
done

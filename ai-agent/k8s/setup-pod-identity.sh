#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="eks-cluster"
AWS_REGION="us-west-2"
NAMESPACE="ai-agent"
SERVICE_ACCOUNT="ai-agent"
ROLE_NAME="ai-agent-bedrock"
POLICY_NAME="ai-agent-bedrock-invoke"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "pods.eks.amazonaws.com" },
    "Action": ["sts:AssumeRole", "sts:TagSession"]
  }]
}'

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST"
else
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST"
fi

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"bedrock:InvokeModel\", \"bedrock:InvokeModelWithResponseStream\"],
      \"Resource\": [
        \"arn:aws:bedrock:*::foundation-model/*\",
        \"arn:aws:bedrock:*:${ACCOUNT_ID}:inference-profile/*\"
      ]
    }]
  }"

ASSOC_ID="$(aws eks list-pod-identity-associations \
  --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
  --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
  --query 'associations[0].associationId' --output text 2>/dev/null || echo None)"

if [ "$ASSOC_ID" != "None" ] && [ -n "$ASSOC_ID" ]; then
  aws eks update-pod-identity-association \
    --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --association-id "$ASSOC_ID" --role-arn "$ROLE_ARN"
else
  aws eks create-pod-identity-association \
    --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
    --role-arn "$ROLE_ARN"
fi

echo "Mapped $NAMESPACE/$SERVICE_ACCOUNT -> $ROLE_ARN"

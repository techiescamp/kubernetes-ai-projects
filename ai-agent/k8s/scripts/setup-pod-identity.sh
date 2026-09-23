#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="eks-cluster"
AWS_REGION="us-west-2"
NAMESPACE="ai-agent"
SERVICE_ACCOUNT="ai-agent"
ROLE_NAME="ai-agent-bedrock"
POLICY_NAME="ai-agent-bedrock-invoke"

ACTION="${1:-}"
if [ "$ACTION" != "create" ] && [ "$ACTION" != "cleanup" ]; then
  echo "usage: $0 create|cleanup" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

find_association() {
  aws eks list-pod-identity-associations \
    --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
    --query 'associations[0].associationId' --output text 2>/dev/null || echo None
}

create() {
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
    echo "role $ROLE_NAME already exists, trust policy refreshed"
  else
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" >/dev/null
    echo "role $ROLE_NAME created"
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
  echo "policy $POLICY_NAME attached"

  ASSOC_ID="$(find_association)"
  if [ "$ASSOC_ID" != "None" ] && [ -n "$ASSOC_ID" ]; then
    aws eks update-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
      --association-id "$ASSOC_ID" --role-arn "$ROLE_ARN" >/dev/null
    echo "association $ASSOC_ID updated"
  else
    aws eks create-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
      --namespace "$NAMESPACE" --service-account "$SERVICE_ACCOUNT" \
      --role-arn "$ROLE_ARN" >/dev/null
    echo "association created"
  fi
}

cleanup() {
  ASSOC_ID="$(find_association)"
  if [ "$ASSOC_ID" != "None" ] && [ -n "$ASSOC_ID" ]; then
    aws eks delete-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" \
      --association-id "$ASSOC_ID" >/dev/null
    echo "association $ASSOC_ID deleted"
  else
    echo "no association found, nothing to delete"
  fi

  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    if aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" >/dev/null 2>&1; then
      aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME"
      echo "policy $POLICY_NAME deleted"
    fi

    LEFTOVER="$(aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames' --output text)"
    ATTACHED="$(aws iam list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies[].PolicyName' --output text)"
    if [ -n "$LEFTOVER" ] || [ -n "$ATTACHED" ]; then
      echo "role $ROLE_NAME still has policies attached, not deleting it:" >&2
      [ -n "$LEFTOVER" ] && echo "  inline:   $LEFTOVER" >&2
      [ -n "$ATTACHED" ] && echo "  attached: $ATTACHED" >&2
      exit 1
    fi

    aws iam delete-role --role-name "$ROLE_NAME"
    echo "role $ROLE_NAME deleted"
  else
    echo "no role found, nothing to delete"
  fi

  echo
  echo "Removed Pod Identity for $NAMESPACE/$SERVICE_ACCOUNT"
}

"$ACTION"

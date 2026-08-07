#!/usr/bin/env bash
set -euo pipefail

export AWS_PAGER=""

NAMESPACE="default"
SERVICE_ACCOUNT_NAME="k8sgpt-readonly"
CLUSTER_ROLE_NAME="k8sgpt-readonly"
CLUSTER_ROLE_BINDING_NAME="k8sgpt-readonly"

IAM_ROLE_NAME="k8sgpt-bedrock-role"
IAM_POLICY_NAME="bedrock-invoke"
CLUSTER_NAME="gvisor-demo"
AWS_REGION="us-west-2"

create() {
  echo "==> Applying RBAC (ServiceAccount, ClusterRole, ClusterRoleBinding)..."

  kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SERVICE_ACCOUNT_NAME}
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${CLUSTER_ROLE_NAME}
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, services, endpoints, configmaps,
                namespaces, nodes, events, persistentvolumeclaims]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: [deployments, daemonsets, replicasets, statefulsets]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: [jobs, cronjobs]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${CLUSTER_ROLE_BINDING_NAME}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${CLUSTER_ROLE_NAME}
subjects:
  - kind: ServiceAccount
    name: ${SERVICE_ACCOUNT_NAME}
    namespace: ${NAMESPACE}
EOF

  echo "==> Creating IAM role: ${IAM_ROLE_NAME}..."
  if aws iam get-role --role-name "$IAM_ROLE_NAME" >/dev/null 2>&1; then
    echo "    Role already exists, skipping creation."
  else
    aws iam create-role \
      --role-name "$IAM_ROLE_NAME" \
      --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": { "Service": "pods.eks.amazonaws.com" },
          "Action": ["sts:AssumeRole", "sts:TagSession"]
        }]
      }'
  fi

  echo "==> Attaching Bedrock invoke policy..."
  aws iam put-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-name "$IAM_POLICY_NAME" \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["bedrock:InvokeModel"],
        "Resource": "*"
      }]
    }'

  echo "==> Associating Pod Identity: ${SERVICE_ACCOUNT_NAME} -> ${IAM_ROLE_NAME}..."
  ROLE_ARN=$(aws iam get-role --role-name "$IAM_ROLE_NAME" --query 'Role.Arn' --output text)

  EXISTING_ASSOCIATION=$(aws eks list-pod-identity-associations \
    --cluster-name "$CLUSTER_NAME" \
    --namespace "$NAMESPACE" \
    --service-account "$SERVICE_ACCOUNT_NAME" \
    --region "$AWS_REGION" \
    --query 'associations[0].associationId' --output text 2>/dev/null || true)

  if [[ -n "$EXISTING_ASSOCIATION" && "$EXISTING_ASSOCIATION" != "None" ]]; then
    echo "    Pod Identity association already exists, skipping."
  else
    aws eks create-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" \
      --namespace "$NAMESPACE" \
      --service-account "$SERVICE_ACCOUNT_NAME" \
      --role-arn "$ROLE_ARN" \
      --region "$AWS_REGION"
  fi

  echo "==> create complete"
}

delete() {
  echo "==> Removing Pod Identity association..."
  ASSOCIATION_ID=$(aws eks list-pod-identity-associations \
    --cluster-name "$CLUSTER_NAME" \
    --namespace "$NAMESPACE" \
    --service-account "$SERVICE_ACCOUNT_NAME" \
    --region "$AWS_REGION" \
    --query 'associations[0].associationId' --output text 2>/dev/null || true)

  if [[ -n "$ASSOCIATION_ID" && "$ASSOCIATION_ID" != "None" ]]; then
    aws eks delete-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" \
      --association-id "$ASSOCIATION_ID" \
      --region "$AWS_REGION"
  else
    echo "    No association found, skipping."
  fi

  echo "==> Detaching and deleting IAM role: ${IAM_ROLE_NAME}..."
  aws iam delete-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-name "$IAM_POLICY_NAME" 2>/dev/null || echo "    Policy already removed, skipping."

  aws iam delete-role \
    --role-name "$IAM_ROLE_NAME" 2>/dev/null || echo "    Role already removed, skipping."

  echo "==> Deleting RBAC (ClusterRoleBinding, ClusterRole, ServiceAccount)..."
  kubectl delete clusterrolebinding "$CLUSTER_ROLE_BINDING_NAME" --ignore-not-found
  kubectl delete clusterrole "$CLUSTER_ROLE_NAME" --ignore-not-found
  kubectl delete serviceaccount "$SERVICE_ACCOUNT_NAME" -n "$NAMESPACE" --ignore-not-found

  echo "==> delete complete"
}

usage() {
  echo "Usage: $0 {create|delete}"
  exit 1
}

case "${1:-}" in
  create) create ;;
  delete) delete ;;
  *) usage ;;
esac
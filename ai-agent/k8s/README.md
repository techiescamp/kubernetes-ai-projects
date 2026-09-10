# Deploying `ai-agent` to Kubernetes

This folder contains everything needed to run the backend (LangGraph agent + FastAPI) and
frontend (Next.js chat UI) as a Kubernetes workload, with durable memory (Postgres), RBAC scoped
to what the agent's tools actually do, network policy, health probes, and resource limits.

## Prerequisites

| Needed for | Requirement | Status on this machine (as of last check) |
|---|---|---|
| Everything | `kubectl` pointed at your target cluster | Done - `do-ams3-crunchmedia-k8s-cluster` (DigitalOcean) is the current context |
| Phase 1 (infra-only) | Cluster-admin `kubectl` access (to create a ClusterRole/ClusterRoleBinding) | Assumed yes - same kubeconfig as above |
| Phase 1 (infra-only) | A Postgres password for the checkpoint store | Not generated yet - any strong random string, doesn't need to be memorable |
| Phase 2 (images) | Docker or Podman, to build `Dockerfile`/`frontend/Dockerfile` | **Not installed** - neither `docker` nor `podman` found |
| Phase 2 (images) | A container registry reachable from the cluster (e.g. DigitalOcean Container Registry, Docker Hub, GHCR) | **Not set up** - no DOCR registry exists on this DO account yet (`doctl registry get` returns 404) |
| Phase 3 (real Bedrock calls) | AWS IAM access key + secret with `bedrock:InvokeModel` on the two model IDs in `.env.example`, since DigitalOcean has no IRSA equivalent | Not provided - needed only once you want the agent to actually call Bedrock, not for infra validation |

**Why phases:** you don't need Docker, a registry, or AWS credentials to validate that the RBAC,
Postgres, NetworkPolicy, and ConfigMap manifests are correct and reconcile cleanly on the real
cluster - that's Phase 1 below. Docker/registry are only needed once you want the backend/frontend
pods themselves running (Phase 2). AWS credentials are only needed once you want the agent to make
real Bedrock calls (Phase 3).

## Phase 1: infra-only validation (no Docker, no AWS credentials needed)

```sh
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-serviceaccount.yaml
kubectl apply -f k8s/02-rbac.yaml
kubectl apply -f k8s/03-configmap.yaml
# k8s/secret.yaml here only needs POSTGRES_PASSWORD/DATABASE_PASSWORD filled in - AWS keys can be
# left as empty strings or omitted entirely (they're marked optional: true in the Deployment).
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/05-postgres.yaml
kubectl apply -f k8s/11-networkpolicy.yaml
kubectl apply -f k8s/12-pdb.yaml
```

Verify: `kubectl -n ai-agent get pods` should show the `ai-agent-postgres-0` pod reach `Running`/
`1/1`; `kubectl get clusterrole,clusterrolebinding ai-agent` should show the RBAC objects created.
The NetworkPolicies and PDBs will exist but have no effect yet (they select pods from the backend/
frontend Deployments, which don't exist until Phase 2) - that's expected, not an error.

## Phase 2: build and push the images

Run these from the `ai-agent/` root - both the backend and frontend now live in their own
subfolders with their own `Dockerfile`, mirroring each other:

```sh
docker build -t <your-registry>/ai-agent-backend:latest backend/
docker push <your-registry>/ai-agent-backend:latest

docker build -t <your-registry>/ai-agent-frontend:latest \
  --build-arg NEXT_PUBLIC_BACKEND_URL=http://ai-agent-backend:8000 \
  frontend/
docker push <your-registry>/ai-agent-frontend:latest
```

Then update the `image:` field in `06-backend-deployment.yaml` and `08-frontend-deployment.yaml`
to point at your pushed images.

## Phase 3: real AWS credentials + full deploy

Fill in the rest of `k8s/secret.yaml` (copied from `04-secret.example.yaml`) with real values:

- **AWS credentials for Bedrock** - two options:
  - **EKS (recommended, not applicable to DigitalOcean):** leave `AWS_ACCESS_KEY_ID`/
    `AWS_SECRET_ACCESS_KEY` out of `secret.yaml` entirely, and instead uncomment the
    `eks.amazonaws.com/role-arn` annotation in `01-serviceaccount.yaml`, pointing at an IAM role
    with `bedrock:InvokeModel` permissions (IRSA). No static keys ever touch the cluster;
    `bedrock_clients.py` picks this up automatically via boto3's default credential chain.
  - **Any other cluster (including DigitalOcean, which has no IRSA equivalent):** fill in real
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` values - they're mounted as env vars in
    `06-backend-deployment.yaml` (marked `optional: true` so the Deployment still works if you go
    the IRSA route and omit them).

Then apply the remaining manifests:

```sh
kubectl apply -f k8s/06-backend-deployment.yaml
kubectl apply -f k8s/07-backend-service.yaml
kubectl apply -f k8s/08-frontend-deployment.yaml
kubectl apply -f k8s/09-frontend-service.yaml
# Optional:
kubectl apply -f k8s/10-ingress.yaml         # only if you want external access via Ingress
kubectl apply -f k8s/13-servicemonitor.yaml  # only if the Prometheus Operator is installed
```

Or simply `kubectl apply -f k8s/` once images/credentials are ready (excluding
`04-secret.example.yaml`, which is a template, not a real manifest) - the numeric prefixes keep
`kubectl apply` roughly dependency-ordered, though Kubernetes will happily retry objects that
reference something not-yet-created.

### Verify

```sh
kubectl -n ai-agent get pods            # both Deployments + the postgres StatefulSet Ready
kubectl -n ai-agent logs deploy/ai-agent-backend -f
kubectl -n ai-agent port-forward svc/ai-agent-frontend 3000:3000   # open http://localhost:3000
```

## RBAC rationale and boundaries (`02-rbac.yaml`)

The `ClusterRole` is intentionally broad within a **documented boundary**, not a blanket
`resources: ["*"]`/`verbs: ["*"]` grant:

- Read access (`get/list/watch`) is granted cluster-wide across the resource kinds the diagnostic
  tools in `k8s_tools.py` actually query (pods, nodes, deployments, services, ingresses, jobs,
  PVCs, HPAs, events, etc.) - this is what lets the agent answer "what's wrong with the cluster"
  for essentially any resource type, not just pods.
- Write access is scoped to exactly what the remediation tools do (patch pod/deployment images,
  scale, restart, create/delete pods, create namespaces).
- `apply_kubernetes_yaml` and `delete_resource` can create/update/delete a **curated set of common
  workload kinds only** (Pod, Deployment, Service, ConfigMap, PVC, Ingress, Job, CronJob,
  StatefulSet, DaemonSet) - matching `k8s_tools.py`'s `_ALLOWED_KINDS`.
- **Two things are deliberately never granted, regardless of what gets approved:**
  1. `rbac.authorization.k8s.io` (Roles/ClusterRoles/RoleBindings/ClusterRoleBindings) - so the
     agent can never grant itself (or anything else) more permissions than it starts with.
  2. Namespace **deletion** and Secret **values** - the agent can list Secret names/keys for
     troubleshooting wiring issues, but never reads or writes secret data, and can create
     namespaces but never delete one (a namespace delete cascades to everything inside it).

The primary safety control is still the human-approval gate in `agents.py`
(`interrupt_before=["apply_remediation", "propose_retry"]`) - no write action, including
`apply_kubernetes_yaml`, ever runs without a person approving it via `POST /api/decision` first.
RBAC is the backstop that bounds what an *approved* action is even capable of doing, in the spirit
of the least-privilege authorization principle from
[kube-agentic-networking](https://kube-agentic-networking.sigs.k8s.io/) (whose actual CRDs aren't
stable/installable yet, so this uses core `ClusterRole`/`NetworkPolicy` instead).

If you want tighter scoping later, the natural next step is splitting this into two
ServiceAccounts/ClusterRoles (one read-only for the diagnostics/verification model, one with
write access for the remediation model) - not done here since `agents.py` currently runs both
tool sets from the same process/pod identity.

## Using a managed database instead of the in-cluster Postgres

Delete `05-postgres.yaml` and change `DATABASE_HOST`/`DATABASE_PORT`/`DATABASE_NAME`/
`DATABASE_USER` in `03-configmap.yaml` (and `DATABASE_PASSWORD` in `secret.yaml`) to point at your
managed instance (RDS, Cloud SQL, etc.). No code change is needed - `checkpointer.py` just reads
`DATABASE_URL`, which `06-backend-deployment.yaml` assembles from those same keys.

## Resource sizing

Backend: `requests: {cpu: 250m, memory: 256Mi}`, `limits: {cpu: 1, memory: 512Mi}` - sized for a
Python process making Bedrock/K8s API calls, not local model inference. Frontend: `requests:
{cpu: 100m, memory: 128Mi}`, `limits: {cpu: 500m, memory: 256Mi}`. Postgres: `requests: {cpu:
100m, memory: 256Mi}`, `limits: {cpu: 500m, memory: 512Mi}`. Adjust based on real usage once
deployed - these are starting points, not measured values.

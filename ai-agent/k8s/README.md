# Deploying `ai-agent` (KubeMedic) to Kubernetes

Runs the backend (LangGraph agent + FastAPI) and frontend (Next.js chat UI) as a Kubernetes
workload, with durable state in Postgres, RBAC scoped to what the agent's tools actually do,
NetworkPolicies, health probes and resource limits.

---

## 1. Prerequisites

| Needed for | Requirement |
|---|---|
| Everything | `kubectl` pointed at your target cluster |
| Everything | Cluster-admin rights (the install creates a ClusterRole/ClusterRoleBinding) |
| Everything | A default StorageClass (Postgres requests a 5Gi PVC) |
| Building images | Docker (or Podman) |
| Building images | A registry the cluster can pull from — Docker Hub, GHCR, DOCR, ECR… |
| Running the agent | AWS credentials with `bedrock:InvokeModel` on the model IDs in `03-configmap.yaml` |
| Running the agent | A strong Postgres password |

**On AWS credentials:** on EKS, prefer IRSA — annotate the ServiceAccount in
`01-serviceaccount.yaml` with `eks.amazonaws.com/role-arn` and leave the AWS keys out entirely;
boto3 picks the role up automatically. On any other cluster (DigitalOcean, GKE, kind…) there is no
IRSA equivalent, so static keys go in `04-secret.yaml`.

Check your cluster is ready:

```sh
kubectl cluster-info
kubectl get storageclass          # at least one marked (default)
```

---

## 2. Dockerization

Backend and frontend each have their own `Dockerfile`. Build from the `ai-agent/` root:

```sh
export REGISTRY=docker.io/<your-username>
export TAG=v1.4.4

docker build -t $REGISTRY/ai-agent-backend:$TAG  backend/
docker build -t $REGISTRY/ai-agent-frontend:$TAG frontend/

docker push $REGISTRY/ai-agent-backend:$TAG
docker push $REGISTRY/ai-agent-frontend:$TAG
```

Then point the manifests at your images by editing **`kustomization.yaml`** (not the Deployment
files — kustomize overrides them at build time):

```yaml
images:
  - name: docker.io/devopscube/ai-agent-backend
    newName: docker.io/<your-username>/ai-agent-backend   # only if your registry differs
    newTag: v1.4.4
  - name: docker.io/devopscube/ai-agent-frontend
    newName: docker.io/<your-username>/ai-agent-frontend
    newTag: v1.4.4
```

**No build-time config is needed for the frontend.** The browser only ever calls relative `/api/*`
paths, which a Next.js Route Handler (`frontend/app/api/[...path]/route.ts`) proxies server-side to
the backend, reading `BACKEND_URL` fresh per request. That's why the same image works behind
`port-forward`, a LoadBalancer or an Ingress without rebuilding.

### Private registry

Docker Hub repos are **private by default**, and the manifests already reference an
`ai-agent-registry` pull secret. Create it once:

```sh
kubectl -n ai-agent create secret docker-registry ai-agent-registry \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<username> \
  --docker-password=<access token, not your password> \
  --docker-email=<email>
```

If your images are public, delete the `imagePullSecrets` block from
`06-backend-deployment.yaml` and `08-frontend-deployment.yaml`.

---

## 3. Deploy with Kustomize

First fill in `04-secret.yaml` — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`DATABASE_PASSWORD`, and `POSTGRES_PASSWORD` (the two passwords **must match**; Postgres sets its
password only on first `initdb`, so a mismatch later means the backend can't authenticate).

> `04-secret.yaml` holds real credentials. Keep it out of version control.

Preview, then apply:

```sh
cd ai-agent

kubectl kustomize k8s/          # render everything, change nothing
kubectl apply -k k8s/           # create/update all objects
kubectl diff -k k8s/            # what would change vs the live cluster
```

`kustomization.yaml` applies two transforms for you:
- **`namespace: ai-agent`** on every namespaced object (and on the ClusterRoleBinding's subject)
- **`images:`** rewrites the backend/frontend tags — one edit instead of two Deployment files

Optional extras are commented out in `kustomization.yaml`; uncomment if your cluster has the
prerequisites:

```yaml
#  - 10-ingress.yaml        # needs an ingress controller
#  - 12-servicemonitor.yaml # needs the Prometheus Operator
```

### Verify

```sh
kubectl -n ai-agent get pods                       # 2 backend, 2 frontend, 1 postgres - all Ready
kubectl -n ai-agent logs deploy/ai-agent-backend -f
kubectl -n ai-agent get clusterrole,clusterrolebinding ai-agent
```

Postgres must be `Running` before the backend becomes Ready — the readiness probe checks both the
Kubernetes API and the Bedrock client.

### Tear down

```sh
kubectl delete -k k8s/
```

---

## 4. Port forwarding

Both Services are `ClusterIP`, so nothing is exposed publicly by default.

**Frontend (the chat UI — what you normally want):**

```sh
kubectl -n ai-agent port-forward svc/ai-agent-frontend 3000:3000
```

Open <http://localhost:3000>.

**Backend (the API directly):**

```sh
kubectl -n ai-agent port-forward svc/ai-agent-backend 8000:8000
```

```sh
curl -sS http://localhost:8000/healthz

curl -sS -X POST http://localhost:8000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"list all pods in kube-system"}'
```

The mapping is `LOCAL:REMOTE` — the Services listen on **3000** and **8000**, so
`port-forward ... 8080:8000` is how you'd use a different local port. Keep the command running in
its own terminal; it logs `Handling connection for …` on each request, which is the quickest way to
tell whether your client is reaching it at all.

Other endpoints: `/readyz`, `/metrics`, `/api/usage`, and the flow endpoints `/api/select-issues`,
`/api/decision`, `/api/guidance`, `/api/retry`.

### Exposing it for real

```sh
kubectl -n ai-agent patch svc ai-agent-frontend -p '{"spec":{"type":"LoadBalancer"}}'
```

**Expose the frontend only — never the backend.** The API has no authentication, so anyone who can
reach it can make the agent change your cluster. If you serve the frontend on a real hostname, add
that origin to `ALLOWED_ORIGINS` in `03-configmap.yaml`.

---

## Manifest reference

| File | Purpose |
|---|---|
| `00-namespace.yaml` | `ai-agent` namespace |
| `01-serviceaccount.yaml` | Agent identity (IRSA annotation goes here on EKS) |
| `02-rbac.yaml` | ClusterRole + binding — see boundaries below |
| `03-configmap.yaml` | Region, model IDs, `REQUIRE_APPROVAL`, DB host, `BACKEND_URL`, CORS |
| `04-secret.yaml` | AWS keys, DB passwords, optional price-per-1k values |
| `05-postgres.yaml` | Postgres StatefulSet + 5Gi PVC (checkpoints & history) |
| `06/07` | Backend Deployment + Service |
| `08/09` | Frontend Deployment + Service |
| `10-ingress.yaml` | Optional external access |
| `11-networkpolicy.yaml` | frontend → backend → postgres only |
| `12-servicemonitor.yaml` | Optional Prometheus scrape |

---

## Security boundaries (`02-rbac.yaml` + code guards)

The agent has **broad write access** across API groups, deliberately — it is meant to fix real
problems. What bounds it:

**Enforced by RBAC (hard guarantees):**
- **Secrets are unreadable.** The core group is enumerated with `secrets` left out, so no `get`/
  `list` verb exists. The agent can *create/replace* a Secret, but never read one back.

**Enforced in code** (`k8s_tools.py`) — Kubernetes RBAC has no "deny" rule, so "everything except
X" cannot be expressed there:
- Cannot modify **its own RBAC** — its ServiceAccount, ClusterRole/Binding, anything in its own
  namespace, or *any* binding whose subject is the agent (blocking by name alone is bypassable by
  creating a new binding).
- **System namespaces** (`kube-system`, `kube-public`, `kube-node-lease`, its own) are left alone
  while healthy; writes are allowed once a workload there is genuinely broken.
- **Destructive actions refused:** deleting a standalone Pod (nothing would recreate it), deleting
  a Bound PersistentVolume, deleting a Node, and restarts that cannot possibly help (e.g.
  `ImagePullBackOff` — the replacement pod fails identically).

**The real control is the human-approval gate** in `agents.py`
(`interrupt_before=["select_issues", "apply_remediation", "propose_retry"]`). No write runs without
someone approving it via `POST /api/decision`. Setting `REQUIRE_APPROVAL: "false"` in the ConfigMap
removes that gate entirely — with broad write access that means an LLM changing your cluster
unsupervised. Leave it `"true"` unless you have a specific reason.

---

## Using a managed database

Remove `05-postgres.yaml` from `kustomization.yaml` and update `DATABASE_HOST`/`DATABASE_PORT`/
`DATABASE_NAME`/`DATABASE_USER` in `03-configmap.yaml` plus `DATABASE_PASSWORD` in
`04-secret.yaml`. No code change needed — `checkpointer.py` just reads the assembled `DATABASE_URL`.

## Resource sizing

Backend `250m/256Mi` → `1/512Mi`; frontend `100m/128Mi` → `500m/256Mi`; Postgres `100m/256Mi` →
`500m/512Mi`. Sized for a Python process making Bedrock and Kubernetes API calls, not local
inference. Starting points, not measured values — adjust once you see real usage.

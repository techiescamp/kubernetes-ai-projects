# Deploying KubeCheck

Backend (LangGraph + FastAPI), frontend (Next.js chat UI), and Postgres for durable state.

---

## Prerequisites

- `kubectl` pointed at your cluster, with cluster-admin rights
- A default StorageClass (Postgres requests a 5Gi PVC)
- AWS credentials with `bedrock:InvokeModel`
- Docker and a registry, if you are building your own images

```sh
kubectl cluster-info
kubectl get storageclass          # at least one marked (default)
```

**AWS credentials.** On EKS run `../scripts/setup-pod-identity.sh create` — it creates the IAM role and maps
it to the ServiceAccount, so no keys go in any file. `../scripts/setup-pod-identity.sh cleanup` removes both
again. Edit `CLUSTER_NAME` at the top of the script first.

Anywhere else, uncomment `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the `secretGenerator`
block of `kustomization.yaml`.

On EKS leave them commented. Environment variables beat the Pod Identity endpoint in boto3's
credential chain, so any value there overrides the role and every Bedrock call fails with
`InvalidClientTokenId`.

---

## Build the images

Skip this if you are using the prebuilt `devopscube/*` images.

```sh
export REGISTRY=docker.io/<your-username>
export TAG=v1.0.0

docker build -t $REGISTRY/ai-agent-backend:$TAG  agent-backend/
docker build -t $REGISTRY/ai-agent-frontend:$TAG agent-ui/
docker push $REGISTRY/ai-agent-backend:$TAG
docker push $REGISTRY/ai-agent-frontend:$TAG
```

Point the manifests at them in `kustomization.yaml`, not in the Deployment files:

```yaml
images:
  - name: docker.io/devopscube/ai-agent-backend
    newName: docker.io/<your-username>/ai-agent-backend
    newTag: v1.0.0
```

The frontend needs no build-time config — the browser calls relative `/api/*` paths and a Next.js
Route Handler proxies them server-side, reading `BACKEND_URL` per request.

**Private registry.** Docker Hub repos are private by default and the Deployments reference an
`ai-agent-registry` pull secret:

```sh
kubectl -n ai-agent create secret docker-registry ai-agent-registry \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<username> \
  --docker-password=<access token> \
  --docker-email=<email>
```

If your images are public, delete the `imagePullSecrets` block from the two Deployment files.

---

## Deploy

Set `DATABASE_PASSWORD` and `POSTGRES_PASSWORD` in the `secretGenerator` block of
`kustomization.yaml`. They **must match** — Postgres only reads its password at first `initdb`, so
changing it later on an existing volume also needs an `ALTER USER`.

> Once filled in, `kustomization.yaml` contains real credentials. Keep it out of version control, or
> delete the `secretGenerator` block and create the two Secrets with `kubectl create secret generic`
> instead.

```sh
cd ai-agent

kubectl kustomize k8s/          # render, change nothing
kubectl apply -k k8s/           # create/update everything
kubectl diff -k k8s/            # what would change
kubectl delete -k k8s/          # tear down (then ./scripts/setup-pod-identity.sh cleanup on EKS)
```

Check it came up:

```sh
kubectl -n ai-agent get pods
kubectl -n ai-agent logs deploy/ai-agent-backend -f
```

The backend restarts once or twice on a fresh install — it checks Postgres at startup and exits if
it isn't ready yet, then succeeds once Postgres finishes initializing.

---

## Open the UI

Both Services are `ClusterIP`, so nothing is public by default.

```sh
kubectl -n ai-agent port-forward svc/ai-agent-frontend 3000:3000
```

Then open <http://localhost:3000>.

For the API directly:

```sh
kubectl -n ai-agent port-forward svc/ai-agent-backend 8000:8000
curl -sS http://localhost:8000/healthz
```

**Expose the frontend only, never the backend.** The API has no authentication — anyone who reaches
it can make the agent change your cluster. If you serve the frontend on a real hostname, add that
origin to `ALLOWED_ORIGINS` in `manifests/configmap.yaml`.

---

## What is configurable where

| Where | What |
|---|---|
| `kustomization.yaml` | Region, model IDs, prices, `REQUIRE_APPROVAL`, image tags, replicas, passwords |
| `manifests/configmap.yaml` | Database host/port/name/user, `BACKEND_URL`, CORS |

The manifests live in `k8s/manifests/`; `k8s/kustomization.yaml` sits outside them and lists the
ones that get applied. `manifests/ingress.yaml` and `manifests/servicemonitor.yaml` are commented
out of that list — uncomment them if your cluster has an ingress controller / the Prometheus
Operator.

---

## Security boundaries

The agent has broad write access on purpose — it exists to fix real problems. Three things bound it.

**RBAC makes Secrets unreadable.** The core API group is enumerated with `secrets` left out, so no
`get`/`list` verb exists for them. The agent can create or replace a Secret, never read one back.

**Code guards** in `app/tools/guards.py` cover what RBAC cannot express (there is no deny rule):

- It cannot touch its own RBAC — ServiceAccount, ClusterRole/Binding, anything in its own namespace,
  or any binding whose subject is the agent.
- System namespaces (`kube-system`, `kube-public`, `kube-node-lease`, its own) are left alone while
  healthy, and writable once something there is genuinely broken.
- Destructive actions are refused: deleting a standalone Pod, a Bound PersistentVolume or a Node,
  and restarts that cannot help (an `ImagePullBackOff` pod comes back identical).

**Human approval is the real control.** `interrupt_before` in `app/agent/graph.py` stops the graph before
every write until someone approves it. Setting `REQUIRE_APPROVAL: "false"` removes that gate
entirely — an LLM changing your cluster unsupervised. Leave it `"true"` unless you have a reason.

---

## Managed database

Remove `manifests/postgres.yaml` from `kustomization.yaml`, update `DATABASE_HOST`/`DATABASE_PORT`/
`DATABASE_NAME`/`DATABASE_USER` in `manifests/configmap.yaml`, and set `DATABASE_PASSWORD` in the
`secretGenerator` block. No code change needed.

## Resource sizing

Backend `250m/256Mi` → `1/512Mi`; frontend `100m/128Mi` → `500m/256Mi`; Postgres `100m/256Mi` →
`500m/512Mi`. Starting points, not measured values.

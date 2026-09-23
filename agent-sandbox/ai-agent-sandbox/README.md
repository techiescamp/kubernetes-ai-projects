# KubeCheck on Agent Sandbox (gVisor)

Deploys the KubeCheck agent (from `../ai-agent`) onto the `gvisor-demo` EKS cluster, with the
backend running inside an [agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
`Sandbox` on the `gvisor` RuntimeClass - the part that executes model-directed cluster actions is
the part worth syscall-isolating. Deployed with Kustomize, same pattern as `ai-agent/k8s`.

The backend is provisioned via `SandboxTemplate` -> `SandboxWarmPool` -> `SandboxClaim` rather than
a bare `Sandbox` object directly - `SandboxClaim` allocates an instance from a pre-warmed pool.
`replicas: 1` on the pool means exactly one instance is claimed.

The gVisor `RuntimeClass` object itself is **not** created here - it's assumed to already exist on
the cluster (handler `runsc`, from the nodegroup bootstrap / prerequisites setup).

## What's sandboxed, and what isn't

| Component | Kind | Why |
|---|---|---|
| Backend | `SandboxClaim` from a `SandboxWarmPool` (gVisor) | Executes model-directed writes against the cluster - the actual attack surface |
| Frontend | plain `Deployment` | UI/proxy only, executes nothing untrusted |
| Postgres | plain `StatefulSet` | Trusted internal store; gVisor's syscall interception adds I/O overhead with no security benefit here |

## Two defaults agent-sandbox pods do NOT inherit from a plain PodSpec

Found by actually running this, not by reading docs - a pod created via `SandboxTemplate` (through
a `SandboxWarmPool`/`SandboxClaim`) does **not** get the same implicit defaults a bare `Pod`,
`Deployment`, or even a directly-created `Sandbox` gets. Both had to be set explicitly in
`manifests/05-backend-template.yaml`:

- **`automountServiceAccountToken: true`** - without it, the controller sets `false`, so there's no
  `/var/run/secrets/kubernetes.io/serviceaccount/token` and the app can't reach the Kubernetes API
  at all (`Cannot load in-cluster kubeconfig: Service token file does not exist.`).
- **`dnsPolicy: ClusterFirst`** - without it, the pod gets `dnsPolicy: None` with no `dnsConfig`, so
  cluster-internal names (`ai-agent-postgres`, etc.) don't resolve
  (`Name or service not known`), even though the pod can still reach the outside internet fine.

A bare `Sandbox` (no template involved) did not have either problem - this is specific to
the Template/WarmPool/Claim path.

**A third gotcha, more subtle:** every instance the pool spawns - the claimed one and any idle
spares it keeps warm for future claims - comes from the *same* `SandboxTemplate`, so they all get
identical labels. If the template stamps `app: ai-agent-backend` directly, an idle spare gets
load-balanced traffic right alongside the claimed instance (confirmed live: the backend Service had
2 endpoints instead of 1). The fix: the template carries no app-identifying label at all, and
`SandboxClaim.spec.additionalPodMetadata.labels` stamps one only on the instance actually claimed.
That field enforces its own rules, both found by hitting them: a bare key like `app` is rejected
(labels here must carry a domain prefix), and an arbitrary domain like `ai-agent.io` is *also*
rejected unless it is on an allowlist the controller enforces (`agent-sandbox-config` ConfigMap,
key `allowed-label-domains`). `sandbox.users.io` is the controller's own default allowed domain, so
that is what `manifests/07-backend-claim.yaml` and `manifests/11-networkpolicy.yaml` use, rather
than adding a custom domain to the cluster-wide allowlist.

## Prerequisites

- `kubectl` pointed at `gvisor-demo` (`aws eks update-kubeconfig --region us-west-2 --name gvisor-demo`)
- agent-sandbox CRDs + controller already installed on the cluster (your own prerequisites setup -
  not part of this folder)
- The `gvisor` RuntimeClass (handler `runsc`) already registered, and nodes with gVisor installed
  and labelled `sandbox: gvisor` - both handled by the cluster's own prerequisites setup, not by
  this folder
- A default StorageClass, for Postgres's PVC (`kubectl get storageclass` - mark one default if none is)

## Deploy

Set `DATABASE_PASSWORD` and `POSTGRES_PASSWORD` in the `secretGenerator` block of
`kustomization.yaml` - they must match. Then:

```sh
./scripts/setup-pod-identity.sh create   # first time only - maps the ai-agent ServiceAccount to Bedrock access
kubectl apply -k .
kubectl -n ai-agent delete sandboxclaim ai-agent-backend-claim && kubectl apply -k .   # picks up the credentials
```

**Neither a `Sandbox` nor a claimed instance auto-rolls on spec changes** the way a
Deployment/StatefulSet does - it's a stable identity by design. Any change that affects the backend
(image tag, config, secrets, the template itself) needs an explicit recreate:

```sh
kubectl -n ai-agent delete sandboxclaim ai-agent-backend-claim && kubectl apply -k .
```

## Files

| File | Purpose |
|---|---|
| `kustomization.yaml` | Image tags, ConfigMap/Secret generation - same pattern as `ai-agent/k8s/kustomization.yaml`, plus `disableNameSuffixHash: true` on the backend's ConfigMap/Secret (see below) |
| `manifests/00-namespace.yaml` .. `02-rbac.yaml` | Same as `ai-agent/k8s/manifests` |
| `manifests/03-configmap.yaml` | Static wiring config (DB host/port/name/user, `BACKEND_URL`, CORS) - same split as `ai-agent/k8s/manifests/configmap.yaml`, merged with per-deployment knobs from `kustomization.yaml`'s `configMapGenerator` |
| `manifests/04-postgres.yaml` | Same as `ai-agent/k8s/manifests/postgres.yaml` |
| `manifests/05-backend-template.yaml` | `SandboxTemplate` for the backend - same container spec as the plain Deployment would use, plus `runtimeClassName: gvisor`, `nodeSelector`, and the two explicit defaults above |
| `manifests/06-backend-warmpool.yaml` | `SandboxWarmPool`, `replicas: 1`, references the template |
| `manifests/07-backend-claim.yaml` | `SandboxClaim` that allocates one instance from the pool, stamping `sandbox.users.io/role: backend` onto just that instance |
| `manifests/08-backend-service.yaml` | The backend's own `Service`, selecting on `sandbox.users.io/role: backend` (not the auto-created one - a claimed instance's name is controller-generated, so a static-named Service matched by that claim-scoped label is what gives the frontend a stable address) |
| `manifests/09-frontend-deployment.yaml` | Frontend Deployment, unchanged from `ai-agent/k8s/manifests/frontend-deployment.yaml` |
| `manifests/10-frontend-service.yaml` | Frontend Service, unchanged from `ai-agent/k8s/manifests/frontend-service.yaml` |
| `manifests/11-networkpolicy.yaml` | Same rules as `ai-agent/k8s/manifests/networkpolicy.yaml`, except the backend-selecting rules use `sandbox.users.io/role: backend` instead of `app: ai-agent-backend`, to match the claimed pod's actual label |
| `scripts/setup-pod-identity.sh` | Same script as `ai-agent/k8s/scripts`, pointed at `gvisor-demo`. Reuses the same IAM role name (`ai-agent-bedrock`) as the other cluster - IAM roles are account-global, Pod Identity *associations* are per-cluster |

Kustomize's built-in `images:` transformer does reach into the `SandboxTemplate`'s nested
`podTemplate.spec.containers[].image` field correctly - verified directly before relying on it.
Its `replacements:` transformer does **not** reach into a custom CRD's nested fields the way it
does for built-in kinds (also verified directly) - that's why the backend's ConfigMap/Secret use
`disableNameSuffixHash: true` in `kustomization.yaml` instead of relying on Kustomize's usual
generated-name rewriting.

## Images

Same public images as the rest of the project - no rebuild needed:
`docker.io/devopscube/ai-agent-backend:v1.7.1`, `docker.io/devopscube/ai-agent-frontend:v1.8.2`.

## Tear down

```sh
kubectl delete -k .
kubectl -n ai-agent delete pvc --all
./scripts/setup-pod-identity.sh cleanup
```

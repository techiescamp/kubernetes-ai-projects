# K8s Diagnose & Remediate Agent  

A LangGraph multi-agent that diagnoses and (with human approval) remediates Kubernetes problems
using AWS Bedrock (Amazon Nova Pro for both diagnostics/verification and remediation - see
`agent-backend/app/infra/bedrock.py`). Exposed via a FastAPI backend and a Next.js chat UI, with a
`diagnose -> propose fix -> [human approval] -> apply -> verify -> retry` loop
(`agent-backend/app/agent/`).

## Structure

```
ai-agent/
  agent-backend/    Python/FastAPI agent
  agent-interface/  Next.js chat UI
  k8s/              Kubernetes deployment manifests, RBAC, and deploy instructions
```

## Local development

```sh
cd agent-backend
cp .env.example .env   # fill in AWS_REGION, model IDs, and any other values you want to set
pip install -r requirements.txt
uvicorn app.main:app          # backend on http://127.0.0.1:8000

cd ../agent-interface
npm install
npm run dev              # frontend on http://localhost:3000
```

Requires a working `kubectl` context (`~/.kube/config`) pointing at the cluster you want the agent
to diagnose/remediate, and AWS credentials with `bedrock:InvokeModel` for the two model IDs in
`agent-backend/.env` (resolved via boto3's default credential chain - env vars,
`~/.aws/credentials`, or an IAM role).

## Deploying to Kubernetes

See [`k8s/README.md`](k8s/README.md) for prerequisites, Dockerfiles, manifests (Deployment,
Service, RBAC, NetworkPolicy, Postgres-backed durable memory, health probes), and a phased
step-by-step deploy guide.

## API endpoints

All served by the FastAPI backend (`agent-backend/app/main.py`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Basic health check |
| GET | `/healthz` | Liveness probe (also reports `require_approval`) |
| GET | `/readyz` | Readiness probe - checks Kubernetes API and Bedrock client connectivity |
| GET | `/api/usage` | Token usage/cost tracking |
| POST | `/api/query` | Submit a diagnosis request; starts a new agent session |
| POST | `/api/decision` | Approve or reject a proposed fix |
| POST | `/api/retry` | Retry remediation after a failed verification |
| POST | `/api/select-issues` | Choose which of the diagnosed issues to remediate |
| POST | `/api/guidance` | Give the agent human guidance before its next attempt |
| GET | `/metrics` | Prometheus metrics (mounted sub-app) |

## Key files

| File | Purpose |
|---|---|
| `agent-backend/app/agent/` | LangGraph state machine - the diagnose/propose/apply/verify/retry loop |
| `agent-backend/app/tools/` | Kubernetes API tools the agent calls (read-only diagnostics + write remediation) |
| `agent-backend/app/main.py` | FastAPI backend - `/api/query`, `/api/decision`, `/api/retry`, `/healthz`, `/readyz`, `/metrics` |
| `agent-backend/app/infra/bedrock.py` | AWS Bedrock model client factory |
| `agent-backend/app/infra/checkpointer.py` | LangGraph checkpoint storage - Postgres (connection pool) if `DATABASE_URL` is set, in-memory otherwise |
| `agent-backend/app/infra/usage_tracker.py` | Token usage/cost tracking |
| `agent-backend/Dockerfile` | Backend container image |
| `agent-interface/` | Next.js chat UI (own `Dockerfile`) |
| `k8s/` | Kubernetes deployment manifests, RBAC, and deploy instructions |

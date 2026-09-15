# AIOps for DevOps Engineers

A LangGraph multi-agent that diagnoses and (with human approval) remediates Kubernetes problems
using AWS Bedrock (Amazon Nova Pro for both diagnostics/verification and remediation - see
`backend/bedrock_clients.py`). Exposed via a FastAPI backend and a Next.js chat UI, with a
`diagnose -> propose fix -> [human approval] -> apply -> verify -> retry` loop (`backend/app/agent/`).

## Structure

```
ai-agent/
  backend/    Python/FastAPI agent - see backend/README.md
  frontend/   Next.js chat UI
  k8s/        Kubernetes deployment manifests, RBAC, and deploy instructions
```

## Local development

```sh
cd backend
cp .env.example .env   # fill in AWS_REGION, model IDs, and any other values you want to set
pip install -r requirements.txt
uvicorn app.main:app          # backend on http://127.0.0.1:8000

cd ../frontend
npm install
npm run dev              # frontend on http://localhost:3000
```

Requires a working `kubectl` context (`~/.kube/config`) pointing at the cluster you want the agent
to diagnose/remediate, and AWS credentials with `bedrock:InvokeModel` for the two model IDs in
`backend/.env` (resolved via boto3's default credential chain - env vars, `~/.aws/credentials`, or
an IAM role).

## Deploying to Kubernetes

See [`k8s/README.md`](k8s/README.md) for prerequisites, Dockerfiles, manifests (Deployment,
Service, RBAC, NetworkPolicy, Postgres-backed durable memory, health probes), and a phased
step-by-step deploy guide.

## Key files

| File | Purpose |
|---|---|
| `backend/app/agent/` | LangGraph state machine - the diagnose/propose/apply/verify/retry loop |
| `backend/app/tools/` | Kubernetes API tools the agent calls (read-only diagnostics + write remediation) |
| `backend/app/main.py` | FastAPI backend - `/api/query`, `/api/decision`, `/api/retry`, `/healthz`, `/readyz`, `/metrics` |
| `backend/bedrock_clients.py` | AWS Bedrock model client factory |
| `backend/checkpointer.py` | LangGraph checkpoint storage - Postgres (connection pool) if `DATABASE_URL` is set, in-memory otherwise |
| `backend/usage_tracker.py` | Token usage/cost tracking |
| `backend/Dockerfile` | Backend container image |
| `frontend/` | Next.js chat UI (own `Dockerfile`) |
| `k8s/` | Kubernetes deployment manifests, RBAC, and deploy instructions |
| `SPEC.md` | Living status/spec doc - what's been done, what's left, bugs found along the way |

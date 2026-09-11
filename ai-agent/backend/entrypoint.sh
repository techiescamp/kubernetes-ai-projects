#!/bin/sh
# Fail fast and loud on startup misconfiguration (missing env vars, no Kubernetes API access)
# instead of serving traffic that will only fail on the first real tool call.
set -e

python -c "
import os, sys
required = ['AWS_REGION', 'DIAGNOSTICS_MODEL_ID', 'REMEDIATION_MODEL_ID']
missing = [v for v in required if not os.getenv(v)]
if missing:
    sys.exit(f'Missing required env vars: {missing}')

from kubernetes import config
try:
    config.load_incluster_config()
except Exception as e:
    sys.exit(f'Cannot load in-cluster kubeconfig: {e}')

database_url = os.getenv('DATABASE_URL')
if database_url:
    import psycopg
    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            pass
    except Exception as e:
        sys.exit(f'DATABASE_URL is set but Postgres is unreachable: {e}')
"

exec uvicorn main:app --host 0.0.0.0 --port 8000

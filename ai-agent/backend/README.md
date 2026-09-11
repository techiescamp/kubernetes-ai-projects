# `ai-agent` backend

This file was found misplaced here (the project's main README belongs at `ai-agent/README.md`,
which has been restored). Kept in place, scoped to this directory, since files can't be deleted in
this environment.

FastAPI + LangGraph agent. Run locally with:

```sh
cp .env.example .env   # fill in AWS_REGION, model IDs, DATABASE_URL, etc.
pip install -r requirements.txt
python main.py          # http://127.0.0.1:8000
```

See `../README.md` and `../k8s/README.md` for the full project overview and deployment guide.

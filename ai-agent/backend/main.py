import logging
import os
import uuid

from logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client as k8s_client
from pydantic import BaseModel
from langchain_classic.memory import ConversationBufferMemory
from langchain_core.messages import HumanMessage

from agents import compiled_graph, MAX_ATTEMPTS
from bedrock_clients import bedrock_client
from metrics import metrics_app
from usage_tracker import get_usage_dict

logger = logging.getLogger(__name__)

app = FastAPI(title="Kubernetes Diagnosis & Remediation Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    # Defaults to the Next.js dev server on localhost/127.0.0.1 regardless of port (Next falls
    # back to 3001, 3002, etc. when 3000 is already taken). Set ALLOWED_ORIGINS in-cluster to the
    # real frontend origin.
    allow_origin_regex=os.getenv("ALLOWED_ORIGINS", r"http://(localhost|127\.0\.0\.1):\d+"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Standard Prometheus scrape target.
app.mount("/metrics", metrics_app)

memory = ConversationBufferMemory(return_messages=True)


class QueryRequest(BaseModel):
    query: str


class DecisionRequest(BaseModel):
    session_id: str
    approved: bool


class RetryRequest(BaseModel):
    session_id: str
    retry: bool


def _get_state(session_id: str):
    """
    Looks up a paused graph thread by session_id. The checkpointer (not a local dict)
    is the source of truth for session state; an empty snapshot means the thread was
    never started or already ran to completion.
    """
    config = {"configurable": {"thread_id": session_id}}
    snapshot = compiled_graph.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Unknown or already-completed session_id.")
    return config, snapshot


@app.get("/api/health")
def health_check():
    """Deprecated alias for /healthz, kept for compatibility with the current frontend."""
    return {"status": "ok", "backend": "FastAPI"}


@app.get("/healthz")
def liveness():
    """Liveness probe target: process is up, no external calls. Always fast."""
    return {"status": "ok"}


@app.get("/readyz")
def readiness():
    """
    Readiness probe target: confirms the pod can actually do its job (reach the Kubernetes API
    and has a usable Bedrock client) before Kubernetes routes traffic to it.
    """
    try:
        k8s_client.CoreV1Api().list_namespace(limit=1, _request_timeout=3)
    except Exception as e:
        logger.warning(f"readiness check failed: kubernetes api unreachable: {e}")
        raise HTTPException(status_code=503, detail=f"kubernetes api unreachable: {e}")
    try:
        bedrock_client.meta.region_name
    except Exception as e:
        logger.warning(f"readiness check failed: bedrock client misconfigured: {e}")
        raise HTTPException(status_code=503, detail=f"bedrock client misconfigured: {e}")
    return {"status": "ready"}


@app.get("/api/usage")
def usage():
    return get_usage_dict()


@app.post("/api/query")
def submit_query(request: QueryRequest):
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    session_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": session_id}}

    history = memory.load_memory_variables({}).get("history", [])
    state = {
        "messages": history + [HumanMessage(content=query)],
        "diagnostic_report": "",
        "proposed_fix": "",
        "approval_status": "pending",
        "fix_result": "",
        "verification_result": "",
        "attempt": 0,
    }

    # Runs diagnose -> (propose_remediation ->) and freezes right before apply_remediation
    # if a fix is needed, thanks to interrupt_before in agents.py.
    output_state = compiled_graph.invoke(state, config)
    diag_report = output_state.get("diagnostic_report", "")
    proposed_fix = output_state.get("proposed_fix", "")

    paused = bool(compiled_graph.get_state(config).next)

    if not paused:
        memory.save_context({"input": query}, {"output": diag_report})
        return {
            "remediation_needed": False,
            "diagnostic_report": diag_report,
        }

    return {
        "session_id": session_id,
        "remediation_needed": True,
        "diagnostic_report": diag_report,
        "proposed_fix": proposed_fix,
    }


@app.post("/api/decision")
def submit_decision(request: DecisionRequest):
    config, snapshot = _get_state(request.session_id)
    state = snapshot.values

    if not request.approved:
        memory.save_context(
            {"input": state.get("user_request", "")},
            {"output": (
                f"Diagnostic Report:\n{state.get('diagnostic_report', '')}\n\n"
                f"Proposed Fix:\n{state.get('proposed_fix', '')}\n\n"
                f"User declined to apply the fix."
            )},
        )
        return {"status": "cancelled"}

    # Lift the approval gate and resume: runs apply_remediation -> verify_remediation,
    # then either ends or freezes again right before propose_retry.
    compiled_graph.update_state(config, {"approval_status": "approved"})
    output_state = compiled_graph.invoke(None, config)

    fix_result = output_state.get("fix_result", "")
    verification = output_state.get("verification_result", "")
    goal_achieved = "GOAL_ACHIEVED: YES" in verification
    attempt = output_state.get("attempt", 0)

    paused = bool(compiled_graph.get_state(config).next)

    if not paused:
        memory.save_context(
            {"input": state.get("user_request", "")},
            {"output": (
                f"Diagnostic Report:\n{output_state.get('diagnostic_report', '')}\n\n"
                f"Proposed Fix:\n{output_state.get('proposed_fix', '')}\n\n"
                f"Execution Result:\n{fix_result}\n\n"
                f"Verification:\n{verification}"
            )},
        )
        return {
            "status": "done",
            "fix_result": fix_result,
            "verification": verification,
            "goal_achieved": goal_achieved,
            "attempt": attempt,
            "max_attempts": MAX_ATTEMPTS,
        }

    return {
        "status": "retry_available",
        "fix_result": fix_result,
        "verification": verification,
        "goal_achieved": False,
        "attempt": attempt,
        "max_attempts": MAX_ATTEMPTS,
    }


@app.post("/api/retry")
def submit_retry(request: RetryRequest):
    config, snapshot = _get_state(request.session_id)
    state = snapshot.values

    if not request.retry:
        memory.save_context(
            {"input": state.get("user_request", "")},
            {"output": (
                f"Diagnostic Report:\n{state.get('diagnostic_report', '')}\n\n"
                f"Proposed Fix:\n{state.get('proposed_fix', '')}\n\n"
                f"Execution Result:\n{state.get('fix_result', '')}\n\n"
                f"Verification:\n{state.get('verification_result', '')}\n\n"
                f"User declined to retry after the goal was not confirmed achieved."
            )},
        )
        return {"status": "stopped"}

    # Feed the failed attempt back in as fresh diagnostic evidence so the next
    # proposal is grounded in what actually happened, not a guess.
    updated_report = (
        f"{state.get('diagnostic_report', '')}\n\n"
        f"--- Previous fix attempt {state.get('attempt', 0)} ---\n"
        f"Applied: {state.get('fix_result', '')}\n"
        f"Verification: {state.get('verification_result', '')}\n"
        f"REMEDIATION_NEEDED: YES"
    )
    compiled_graph.update_state(config, {"diagnostic_report": updated_report})

    # Lifts the retry gate and resumes: runs propose_retry, then freezes again right
    # before apply_remediation, waiting for approval of the new proposal via /api/decision.
    output_state = compiled_graph.invoke(None, config)

    return {
        "status": "proposed",
        "proposed_fix": output_state.get("proposed_fix", ""),
        "attempt": output_state.get("attempt", 0),
        "max_attempts": MAX_ATTEMPTS,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("ENVIRONMENT", "production") == "development",
    )

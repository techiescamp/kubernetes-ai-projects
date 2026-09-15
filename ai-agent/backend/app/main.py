import logging
import os
import uuid

from app.infra.logging_config import configure_logging

configure_logging()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client as k8s_client
from pydantic import BaseModel
from langchain_core.messages import HumanMessage

from app.agent import compiled_graph, generate_proposal, MAX_ATTEMPTS, REQUIRE_APPROVAL
from app.infra.conversation_store import ConversationStore
from app.infra.bedrock import bedrock_client
from app.infra.metrics import metrics_app
from app.infra.usage_tracker import get_usage_dict

logger = logging.getLogger(__name__)

app = FastAPI(title="Kubernetes Diagnosis & Remediation Agent API", version="1.0.0")


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=os.getenv("ALLOWED_ORIGINS", r"http://(localhost|127\.0\.0\.1):\d+"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/metrics", metrics_app)

memory = ConversationStore()


class QueryRequest(BaseModel):
    query: str


class DecisionRequest(BaseModel):
    session_id: str
    approved: bool


class RetryRequest(BaseModel):
    session_id: str
    retry: bool


class GuidanceRequest(BaseModel):
    session_id: str
    instruction: str


class IssueSelectionRequest(BaseModel):
    session_id: str
    selected_indices: list[int] = []


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
    return {"status": "ok", "require_approval": REQUIRE_APPROVAL}


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

    history = memory.load_history()
    state = {
        "messages": history + [HumanMessage(content=query)],
        "diagnostic_report": "",
        "remediation_needed": False,
        "goal_achieved": False,
        "issues": [],
        "retry_context": "",
        "proposed_fix": "",
        "approval_status": "pending",
        "fix_result": "",
        "verification_result": "",
        "attempt": 0,
    }

    output_state = compiled_graph.invoke(state, config)
    diag_report = output_state.get("diagnostic_report", "")
    proposed_fix = output_state.get("proposed_fix", "")
    fix_result = output_state.get("fix_result", "")
    verification = output_state.get("verification_result", "")

    next_nodes = compiled_graph.get_state(config).next
    paused = bool(next_nodes)

    if "select_issues" in next_nodes:
        return {
            "session_id": session_id,
            "remediation_needed": True,
            "needs_issue_selection": True,
            "diagnostic_report": diag_report,
            "issues": output_state.get("issues", []),
        }

    if not paused and fix_result:
        goal_achieved = bool(output_state.get("goal_achieved"))
        memory.record(
            query,
            f"Diagnostic Report:\n{diag_report}\n\nProposed Fix:\n{proposed_fix}\n\n"
            f"Execution Result:\n{fix_result}\n\nVerification:\n{verification}",
        )
        return {
            "remediation_needed": True,
            "auto_approved": True,
            "diagnostic_report": diag_report,
            "proposed_fix": proposed_fix,
            "fix_result": fix_result,
            "verification": verification,
            "goal_achieved": goal_achieved,
            "attempt": output_state.get("attempt", 0),
            "max_attempts": MAX_ATTEMPTS,
        }

    if not paused:
        memory.record(query, diag_report)
        return {
            "remediation_needed": False,
            "diagnostic_report": diag_report,
        }

    memory.record(query, diag_report)
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

    if "select_issues" in (compiled_graph.get_state(config).next or ()):
        raise HTTPException(
            status_code=409,
            detail="This session is waiting for you to choose which issue(s) to fix - "
                   "call /api/select-issues first.",
        )

    if not request.approved:
        memory.record(
            state.get("user_request", ""),
            f"Diagnostic Report:\n{state.get('diagnostic_report', '')}\n\n"
            f"Proposed Fix:\n{state.get('proposed_fix', '')}\n\n"
            f"User declined to apply the fix.",
        )
        return {"status": "cancelled"}

    compiled_graph.update_state(config, {"approval_status": "approved"})
    output_state = compiled_graph.invoke(None, config)
    attempt_history = [{
        "attempt": output_state.get("attempt", 0),
        "proposed_fix": output_state.get("proposed_fix", ""),
        "fix_result": output_state.get("fix_result", ""),
        "verification": output_state.get("verification_result", ""),
    }]

    while True:
        next_nodes = compiled_graph.get_state(config).next
        if "propose_retry" in next_nodes:
            prev = compiled_graph.get_state(config).values
            attempt_note = (
                f"--- Attempt {prev.get('attempt', 0)} ---\n"
                f"Applied: {prev.get('fix_result', '')}\n"
                f"Verification: {prev.get('verification_result', '')}"
            )
            compiled_graph.update_state(config, {
                "retry_context": f"{prev.get('retry_context', '')}\n\n{attempt_note}".strip()
            })
            output_state = compiled_graph.invoke(None, config)
            next_nodes = compiled_graph.get_state(config).next
        if "apply_remediation" in next_nodes:
            compiled_graph.update_state(config, {"approval_status": "approved"})
            output_state = compiled_graph.invoke(None, config)
            attempt_history.append({
                "attempt": output_state.get("attempt", 0),
                "proposed_fix": output_state.get("proposed_fix", ""),
                "fix_result": output_state.get("fix_result", ""),
                "verification": output_state.get("verification_result", ""),
            })
        else:
            break

    fix_result = output_state.get("fix_result", "")
    verification = output_state.get("verification_result", "")
    goal_achieved = bool(output_state.get("goal_achieved"))
    attempt = output_state.get("attempt", 0)

    paused = bool(compiled_graph.get_state(config).next)

    if not paused:
        memory.record(
            state.get("user_request", ""),
            f"Diagnostic Report:\n{output_state.get('diagnostic_report', '')}\n\n"
            f"Proposed Fix:\n{output_state.get('proposed_fix', '')}\n\n"
            f"Execution Result:\n{fix_result}\n\n"
            f"Verification:\n{verification}",
        )
        return {
            "status": "done",
            "fix_result": fix_result,
            "verification": verification,
            "goal_achieved": goal_achieved,
            "attempt": attempt,
            "max_attempts": MAX_ATTEMPTS,
            "attempt_history": attempt_history,
        }

    return {
        "status": "retry_available",
        "fix_result": fix_result,
        "verification": verification,
        "attempt_history": attempt_history,
        "goal_achieved": False,
        "attempt": attempt,
        "max_attempts": MAX_ATTEMPTS,
    }


@app.post("/api/retry")
def submit_retry(request: RetryRequest):
    config, snapshot = _get_state(request.session_id)
    state = snapshot.values

    if not request.retry:
        memory.record(
            state.get("user_request", ""),
            f"Diagnostic Report:\n{state.get('diagnostic_report', '')}\n\n"
            f"Proposed Fix:\n{state.get('proposed_fix', '')}\n\n"
            f"Execution Result:\n{state.get('fix_result', '')}\n\n"
            f"Verification:\n{state.get('verification_result', '')}\n\n"
            f"User declined to retry after the goal was not confirmed achieved.",
        )
        return {"status": "stopped"}

    attempt_note = (
        f"--- Attempt {state.get('attempt', 0)} ---\n"
        f"Applied: {state.get('fix_result', '')}\n"
        f"Verification: {state.get('verification_result', '')}"
    )
    compiled_graph.update_state(config, {
        "retry_context": f"{state.get('retry_context', '')}\n\n{attempt_note}".strip()
    })

    output_state = compiled_graph.invoke(None, config)

    return {
        "status": "proposed",
        "proposed_fix": output_state.get("proposed_fix", ""),
        "attempt": output_state.get("attempt", 0),
        "max_attempts": MAX_ATTEMPTS,
    }


@app.post("/api/select-issues")
def submit_issue_selection(request: IssueSelectionRequest):
    """
    Resumes a session paused at select_issues (diagnose_node found more than one distinct
    problem) with the human's choice of which issue(s) to actually act on. Narrows
    diagnostic_report to just the selected issue(s) before letting propose_remediation run, so a
    fix only ever gets proposed/applied for what the human picked - the other issue(s) found are
    left untouched and simply not mentioned again for this session. Empty/omitted
    selected_indices means "fix everything that was found."
    """
    config, snapshot = _get_state(request.session_id)
    state = snapshot.values
    issues = state.get("issues", [])

    indices = request.selected_indices or list(range(len(issues)))
    invalid = [i for i in indices if not (0 <= i < len(issues))]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid issue index/indices: {invalid}")
    selected = [issues[i] for i in indices]

    narrowed_report = (
        f"The user reviewed {len(issues)} issue(s) found during diagnosis and selected the "
        f"following {len(selected)} to fix now - address ONLY these, ignore the rest:\n"
        + "\n".join(f"- {text}" for text in selected)
        + f"\n\nFull original diagnostic context (for reference only):\n{state.get('diagnostic_report', '')}"
    )
    compiled_graph.update_state(config, {"diagnostic_report": narrowed_report})

    output_state = compiled_graph.invoke(None, config)

    return {
        "session_id": request.session_id,
        "remediation_needed": True,
        "diagnostic_report": narrowed_report,
        "proposed_fix": output_state.get("proposed_fix", ""),
    }


@app.post("/api/guidance")
def submit_guidance(request: GuidanceRequest):
    """
    Lets a human redirect the proposed fix with a free-text instruction instead of only
    Approve/Reject - e.g. "no, attach it via envFrom, not an annotation" - covering both the
    very first proposal and a post-verification retry proposal. Always produces a fresh,
    human-reviewable proposal rather than auto-applying the instruction: arbitrary free text
    changing what gets run on the cluster deserves a look before it's approved, same as any
    other proposal.
    """
    instruction = request.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Instruction cannot be empty.")

    config, snapshot = _get_state(request.session_id)
    state = snapshot.values
    next_nodes = compiled_graph.get_state(config).next

    guidance_block = (
        f"\n\n--- Human guidance for the next attempt ---\n{instruction}"
    )

    if "propose_retry" in next_nodes:
        attempt_note = (
            f"--- Attempt {state.get('attempt', 0)} ---\n"
            f"Applied: {state.get('fix_result', '')}\n"
            f"Verification: {state.get('verification_result', '')}"
        )
        compiled_graph.update_state(config, {
            "retry_context": f"{state.get('retry_context', '')}\n\n{attempt_note}{guidance_block}".strip()
        })
        output_state = compiled_graph.invoke(None, config)
        return {
            "status": "proposed",
            "proposed_fix": output_state.get("proposed_fix", ""),
            "attempt": output_state.get("attempt", 0),
            "max_attempts": MAX_ATTEMPTS,
        }

    if "apply_remediation" in next_nodes:
        new_context = f"{state.get('retry_context', '')}{guidance_block}".strip()
        new_fix = generate_proposal(
            state.get("diagnostic_report", ""), state.get("user_request", ""), new_context
        )
        compiled_graph.update_state(config, {"retry_context": new_context, "proposed_fix": new_fix})
        return {
            "status": "proposed",
            "proposed_fix": new_fix,
            "attempt": state.get("attempt", 0),
            "max_attempts": MAX_ATTEMPTS,
        }

    raise HTTPException(status_code=409, detail="Session is not currently awaiting a decision.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("ENVIRONMENT", "production") == "development",
    )

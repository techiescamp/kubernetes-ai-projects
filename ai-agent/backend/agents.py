import json
import logging
import os
import re
from typing import TypedDict, List, Annotated, Dict, Any
import operator
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from checkpointer import build_checkpointer
from bedrock_clients import get_diagnostics_model, get_remediation_model
from usage_tracker import record_usage
from metrics import record_tool_call, GRAPH_NODE_SECONDS, REMEDIATION_OUTCOMES
from k8s_tools import (
    list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod,
    get_cluster_events, list_nodes, describe_node, list_deployments, describe_deployment,
    list_replicasets, list_services, list_ingresses, list_configmaps, list_secrets,
    list_pvcs, list_jobs, list_cronjobs, list_statefulsets, list_daemonsets, list_hpas,
    get_resource_usage, get_resource,
    restart_pod, scale_deployment, apply_kubernetes_yaml, create_namespace, create_pod,
    update_pod_image, patch_deployment_image, patch_deployment, rollout_restart_deployment,
    delete_resource,
)

logger = logging.getLogger(__name__)

# 1. State Definition
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    user_request: str
    diagnostic_report: str
    remediation_needed: bool
    goal_achieved: bool
    issues: List[str]
    proposed_fix: str
    approval_status: str  # "pending", "approved", "rejected"
    fix_result: str
    verification_result: str
    attempt: int

MAX_ATTEMPTS = int(os.getenv("MAX_REMEDIATION_ATTEMPTS", "3"))

# ConfigMap-driven global toggle (REQUIRE_APPROVAL in k8s/03-configmap.yaml). True (default):
# every write action pauses for a human decision via POST /api/decision, exactly as before. False:
# the graph never pauses at all - diagnose -> propose -> apply -> verify -> (retry up to
# MAX_ATTEMPTS) all run in a single compiled_graph.invoke() call with no human in the loop. This
# is read once at process startup (like every other ConfigMap value here), so a change requires a
# pod restart to take effect - `kubectl rollout restart deployment/ai-agent-backend` after editing
# the ConfigMap, same as changing AWS_REGION or a model ID.
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "true").lower() != "false"

# 2. Bind Tools to ChatModels
diag_tools = [
    list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod,
    get_cluster_events, list_nodes, describe_node, list_deployments, describe_deployment,
    list_replicasets, list_services, list_ingresses, list_configmaps, list_secrets,
    list_pvcs, list_jobs, list_cronjobs, list_statefulsets, list_daemonsets, list_hpas,
    get_resource_usage, get_resource,
]
remedy_tools = [
    restart_pod, update_pod_image, patch_deployment_image, patch_deployment,
    rollout_restart_deployment, scale_deployment, apply_kubernetes_yaml, create_namespace,
    create_pod, delete_resource,
]

# Map tool names to tool functions for execution
diag_tool_map = {t.name: t for t in diag_tools}
remedy_tool_map = {t.name: t for t in remedy_tools}

def _timed_node(node_name: str):
    """Decorator that records a LangGraph node's execution duration in GRAPH_NODE_SECONDS."""
    def decorator(func):
        def wrapper(state):
            with GRAPH_NODE_SECONDS.labels(node=node_name).time():
                return func(state)
        return wrapper
    return decorator


def invoke_tool_safely(tool_func, args: dict) -> str:
    """
    Each @tool function in k8s_tools.py catches its OWN internal exceptions and returns an error
    string - but argument VALIDATION (the model omitting a required argument, wrong type, etc.)
    happens in LangChain's tool-invocation layer, BEFORE the function body ever runs, and raises a
    raw pydantic ValidationError straight out of .invoke() uncaught. A real deployed test hit
    exactly this - the model called update_pod_image without its required container_name argument,
    which crashed the whole /api/decision request with an HTTP 500 instead of giving the model a
    chance to see what was wrong and retry. Catching it here and returning a normal error STRING
    (fed back as a tool result, same as every other "you got something wrong" case already handled
    in these loops) lets the model self-correct instead of crashing the request.
    """
    try:
        return tool_func.invoke(args)
    except Exception as e:
        return f"Error: invalid arguments for this tool call - {e}. Check the tool's required arguments and retry."


def _strip_reasoning_markup(text: str) -> str:
    """
    Remove the model's internal monologue from anything a human will read.

    Nova Pro wraps its reasoning in <thinking>...</thinking> (and sometimes <response>...</response>)
    and these were being passed straight through into the diagnostic report, the proposal and the
    verification text shown in the UI - so users saw the model talking to itself before getting the
    actual answer. Strip the thinking blocks entirely and unwrap the response tags. An unterminated
    <thinking> (truncated output) is also dropped rather than leaking a half-thought.
    """
    if not text:
        return text
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?response>", "", text, flags=re.IGNORECASE)
    # Collapse the blank lines the removal leaves behind.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_flag(text: str, flag: str) -> tuple:
    """
    Pull an internal routing flag ('REMEDIATION_NEEDED', 'GOAL_ACHIEVED') out of a model response
    and return (is_yes, text_without_the_flag_line).

    These flags exist purely so the graph can route; they were never meant for humans, but the raw
    "REMEDIATION_NEEDED: NO" line was being shown at the bottom of every single chat message. The
    flag is now parsed into a real state field and stripped from the text the user reads - the UI
    already conveys the same thing through the proposal/approval buttons and the goal badge.
    """
    match = re.search(rf"^\s*{flag}:\s*(YES|NO)\b.*$", text, re.MULTILINE | re.IGNORECASE)
    is_yes = bool(match) and match.group(1).upper() == "YES"
    cleaned = re.sub(rf"^\s*{flag}:\s*(YES|NO)\b.*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
    return is_yes, re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _describe_tool_call(name: str, args: dict) -> str:
    """
    Render a tool call as one readable line for the human-facing report.

    The old format dumped the raw args dict, which for apply_kubernetes_yaml meant an entire
    escaped YAML/JSON manifest (hundreds of characters of \\n noise) inline in the chat. Long
    values are truncated to a short preview; the full detail is still in the logs.
    """
    parts = []
    for key, value in sorted(args.items()):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = " ".join(str(text).split())  # collapse newlines/indentation
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    return f"{name}({', '.join(parts)})"


def _condense_result(result) -> str:
    """
    Shorten a tool result for display. Tool errors are already one-line since _api_error_message,
    but anything unexpectedly long (a full object dump) still shouldn't fill the chat window.
    """
    text = " ".join(str(result).split())
    return text if len(text) <= 300 else text[:297] + "..."


def clean_message_content(content) -> str:
    """Helper to convert complex message content structures (e.g. block lists) to clean raw strings."""
    if isinstance(content, str):
        return _strip_reasoning_markup(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, str):
                parts.append(block)
        return _strip_reasoning_markup("".join(parts))
    return _strip_reasoning_markup(str(content))

# 3. Define Nodes

@_timed_node("diagnose")
def diagnose_node(state: AgentState) -> Dict[str, Any]:
    """
    Nova Pro model node.
    Performs cluster inspections using diagnostic tools to understand the root cause of the issue.
    """
    model = get_diagnostics_model()
    model_with_tools = model.bind_tools(diag_tools)

    # Pull the latest human request out of the conversation so it survives into later
    # nodes even if the model's own report ends up terse (e.g. just the flag line).
    user_request = state.get("user_request", "")
    if not user_request:
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                user_request = clean_message_content(m.content)
                break

    # We construct a message sequence
    messages = [
        SystemMessage(content=(
            "You are a Kubernetes Diagnostics Specialist. Your goal is to satisfy the user's request using the provided tools.\n"
            "Guidelines:\n"
            "1. If the user asks for read-only information (e.g. listing pods, namespaces, logs, events, resources), satisfying their request IS your main goal. Retrieve all necessary data using your tools, filter it as requested (e.g. only listing non-running pods, or logs containing specific patterns), and present the answer clearly.\n"
            "-1. If the user asks about THIS CONVERSATION rather than about the cluster - 'what did we just do', 'what was the last fix', 'what did you change', 'summarise what we've done' - answer from the conversation history you were given above, NOT by calling tools. Cluster events are not a record of what YOU did: they show everything that happened on the cluster from any source, so answering such a question from get_cluster_events is wrong (a real deployed test answered 'the last troubleshooting was scaling ai-agent-frontend' by quoting an unrelated event). If the history does not contain the answer, say plainly that you don't have a record of it in this conversation - do not substitute a guess from events. These questions never need remediation.\n"
            "0. EVENTS ARE HISTORY, NOT CURRENT STATE. Kubernetes keeps events for about an hour AFTER the thing they describe, including after it has been fixed. get_cluster_events/get_pod_events tell you what happened, never what is true right now. You must NEVER conclude that a resource is missing, broken, misconfigured, or unbound from an event alone. Before stating any such thing, read the actual object (get_resource, list_configmaps, list_pvcs, describe_pod, describe_deployment, ...) and base your report on what that returns. A real deployed test had this exact failure: stale events said a ConfigMap was not found, the report repeated it as fact, and the ConfigMap existed the whole time - the 'issue' was already fixed and the agent went on to 'fix' a problem that did not exist. If an event and the live object disagree, the live object is right and the event is stale. Also report the resource's real namespace exactly as the object shows it - do not paraphrase or shorten it (a namespace called 'max-ns' is not 'max').\n"
            "2. If the user asks for the root cause of a crash/failure, you MUST investigate before answering - do not guess or list generic possible causes:\n"
            "   a. Call get_pod_status (or describe_pod) first to see the container's current/last state, exit code, and restart count.\n"
            "   b. If restart_count > 0 or the container is not Running, call get_pod_logs with previous=True to get the crashed container's actual log output - the current container may be empty or mid-startup, so the previous instance has the real error.\n"
            "   c. Call describe_pod for conditions, resource requests/limits, and termination reason/message (e.g. OOMKilled, non-zero exit code, ImagePullBackOff).\n"
            "   d. Call get_pod_events for scheduling/probe/pull failures that don't show up in logs.\n"
            "   e. Your report must state the EXACT error message, exit code, or reason found in the tool output (quote it), not a hypothetical list of possible causes. Only say the cause is uncertain if the tools genuinely returned no useful evidence after trying (a)-(d).\n"
            "   f. If (a)-(d) don't explain the failure (e.g. the pod spec and container state look correct but something outside the pod itself is the actual cause - blocked by a ResourceQuota/LimitRange, a NetworkPolicy preventing required connectivity, a missing/misconfigured RBAC binding, an admission webhook rejection, a taint with no matching toleration, PriorityClass preemption, etc.), don't stop and call it 'uncertain' - reason about which Kubernetes object type would actually explain it and inspect that with get_resource (it covers ANY kind, not just what has a dedicated tool). You are not limited to the specific tools listed by name in this prompt.\n"
            "3. If the user is reporting a fault/issue, requesting a modification/creation/DELETION (e.g. creating a namespace, creating a deployment, deleting resources, restarting resources, scaling), or if you detect a failing resource, describe EXACTLY what needs to be changed, created or deleted, including any resource names/values the user gave you verbatim (e.g. namespace name, pod name, container image, replica count, deployment name). If the user wants a pod/deployment created but did not specify a container image, pick a sensible default (e.g. 'nginx:latest') and state it explicitly so the Remediation Agent doesn't have to guess. YOU personally only run read tools in THIS step - that does NOT mean the request cannot be carried out. A separate Remediation Agent runs immediately after you and DOES have write tools (create, patch, delete, scale, apply YAML); your job is to hand it a precise instruction. Summarize the requested change in your own report and output 'REMEDIATION_NEEDED: YES' so it can apply it.\n"
            "   NEVER refuse an action request, never say you 'cannot execute commands' or that you are 'just an AI', and NEVER tell the user to go run kubectl themselves - this system executes real changes on the cluster and the user is asking it to act, not asking for instructions. Writing out a kubectl command instead of flagging REMEDIATION_NEEDED: YES is a hard failure: it makes the whole agent do nothing. Any request to create, delete, apply, scale, restart, patch or modify ANYTHING is ALWAYS 'REMEDIATION_NEEDED: YES', even when nothing is broken and even when the user is simply asking for a resource to be created or cleaned up. 'REMEDIATION_NEEDED: NO' is only ever correct for a purely read-only question (listing/describing/explaining) where the user asked for information and no cluster change of any kind was requested.\n"
            "   Match the fix to the ROOT CAUSE, don't default to restarting: if the container state/reason is ImagePullBackOff, ErrImagePull, or InvalidImageName, the fix is to correct the image (state the exact correct image string), NOT to restart/delete the pod - the error is baked into the pod spec and deleting it just fails again the same way (or, if describe_pod shows 'standalone_pod: true' i.e. no owner_references, deleting it PERMANENTLY removes it since nothing recreates it). Only recommend a restart for genuinely transient failures (e.g. a one-off crash where the spec itself is correct).\n"
            "   If describe_pod shows the pod IS owned by a Deployment (owner_references contains a ReplicaSet whose own owner is a Deployment), prefer the Deployment-level tools (patch_deployment_image, rollout_restart_deployment) over the pod-level ones (update_pod_image, restart_pod) - a direct pod-level fix gets overwritten the next time the Deployment's controller reconciles, so it doesn't actually stick. State the Deployment's name (not just the pod's) in that case.\n"
            "4. Crucial: At the very end of your final response, append exactly one of the following lines:\n"
            "   - 'REMEDIATION_NEEDED: YES' (if the user requested a modification/creation, or if there is an active failure/configuration error that requires a write action)\n"
            "   - 'REMEDIATION_NEEDED: NO' (if it's a read-only query, or if all resources are healthy and no changes are needed)\n"
            "   If REMEDIATION_NEEDED is YES and you found MORE THAN ONE distinct problem (different resources and/or unrelated root causes - e.g. one Deployment with a bad ConfigMap reference AND a separate Pod's failing readiness probe), list each one on its own line directly above the REMEDIATION_NEEDED line, prefixed exactly 'ISSUE: ' (one per line, self-contained enough to act on independently - name the resource, namespace, and problem), so a human can choose which one(s) to fix rather than getting them bundled into a single fix. If there's only ONE problem, do not use any ISSUE: lines - just describe it normally in your report.\n"
            "   A workload that is NOT in a healthy state right now IS an issue - never describe one and then conclude nothing is wrong. Specifically: a Pod that is Pending, ContainerCreating, Init:*, CrashLoopBackOff, Error, ImagePullBackOff/ErrImagePull, Evicted, stuck Terminating, or Running-but-not-Ready (0/1, 1/2); a Deployment/StatefulSet/DaemonSet with fewer ready replicas than desired; a PVC that is Pending; a Job that has failed. 'Pending' and 'ContainerCreating' are NOT benign - a pod stuck in either for more than a couple of minutes means something is actually wrong (unschedulable, no matching node, an unbound PVC, a missing image/ConfigMap/Secret, or a volume that will not attach) and you must investigate it with describe_pod/get_pod_events and report it, including which namespace it is in. The ONLY case where you say nothing is wrong is when every workload you looked at is genuinely healthy right now.\n"
            "   EVERY 'ISSUE:' line must be a problem you confirmed against the LIVE object, not something you saw in an event. Before writing an ISSUE: line, read that object (get_resource/describe_*/list_*) and check it is actually still unhealthy right now. Drop it if the object is gone (the event is stale), or if the object is currently healthy (Bound/Running/Ready - the problem was already fixed and the event is just left over). get_cluster_events annotates each event with 'age' and a live 'object_status' precisely so you can do this - an event whose object_status is GONE must never become an ISSUE. Reporting an already-fixed or non-existent problem as an issue is a serious error: it sends the user to 'fix' something that isn't broken. If after this check nothing is actually wrong, say so and output REMEDIATION_NEEDED: NO.\n"
            "5. Only call tools that were actually given to you via function-calling - never invent a tool name or argument that wasn't provided (e.g. there is no 'list_pods' or 'all_namespaces' argument on any tool). If a tool call comes back 'not found', do not give up or tell the user the capability doesn't exist - retry using one of your real available tools instead. Most read tools accept an optional 'namespace' argument that returns results across ALL namespaces when omitted/left as None.\n"
            "6. To determine whether a pod (or other resource) was created manually vs. by a controller, use describe_pod's 'owner_references'/'standalone_pod' fields - a pod with no owner_references (standalone_pod: true) was created directly (manually, or by a one-off apply), while a pod owned by a ReplicaSet/Deployment/StatefulSet/DaemonSet/Job was created by that controller, not manually.\n"
        ))
    ] + state["messages"]

    # Run a simple execution loop to allow the agent to call tools
    curr_messages = messages.copy()
    limit = 8  # bumped from 5 to fit the multi-step investigation procedure (status, logs, describe, events)
    for _ in range(limit):
        response = model_with_tools.invoke(curr_messages)
        record_usage("diagnostics", response)
        curr_messages.append(response)

        # If model called tools, execute them and feed results back
        if response.tool_calls:
            for tc in response.tool_calls:
                tool_func = diag_tool_map.get(tc["name"])
                if tool_func:
                    logger.info(f"tool_call diagnostics {tc['name']} args={tc['args']}")
                    result = invoke_tool_safely(tool_func, tc["args"])
                    record_tool_call(tc["name"], result)
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=(
                            f"Error: Tool '{tc['name']}' does not exist - it was never provided to you. "
                            f"Your real available tools are: {sorted(diag_tool_map.keys())}. "
                            f"Retry using one of those instead of giving up."
                        ),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        else:
            # No tool calls, we are finished diagnosing
            break
    else:
        # Loop exhausted `limit` iterations without the model ever stopping to synthesize an
        # answer (it was still mid-investigation, calling tools every turn) - curr_messages[-1] is
        # then raw tool output (e.g. a get_resource JSON dump), not analysis. That silently broke
        # remediation entirely on a real deployed test: no 'REMEDIATION_NEEDED: YES' in the report
        # means route_after_diagnose sends it straight to END, so a genuinely broken deployment got
        # reported back to the user as if nothing needed fixing. Force one last no-tools call so
        # there's always an actual conclusion.
        final = model.invoke(curr_messages + [HumanMessage(content=(
            "You have reached your investigation limit. Based on everything you have found so "
            "far, give your final diagnostic report now - do not call any more tools. End with "
            "exactly one of 'REMEDIATION_NEEDED: YES' or 'REMEDIATION_NEEDED: NO' per your "
            "instructions."
        ))])
        record_usage("diagnostics", final)
        curr_messages.append(final)

    final_response = clean_message_content(curr_messages[-1].content)

    # Safety net for a hard failure mode seen in a real deployed session: asked to CREATE a
    # deployment and to DELETE some resources, the model answered like a chatbot - "as an AI I do
    # not have the capability to execute kubectl commands", followed by a command for the user to
    # run themselves - and ended with REMEDIATION_NEEDED: NO. That routes straight to END, so the
    # agent silently did nothing at all for a request it is perfectly capable of executing. The
    # prompt now forbids this explicitly, but a single stray refusal shouldn't be able to no-op the
    # whole run, so detect that exact signature and make one corrective pass.
    refusal_markers = (
        "do not have the capability", "don't have the capability", "cannot execute",
        "can't execute", "unable to execute", "as an ai", "in your local environment",
        "run the following command", "please run", "you can use the following command",
    )
    lowered = final_response.lower()
    if "remediation_needed: no" in lowered and any(m in lowered for m in refusal_markers):
        logger.warning("diagnose_node produced a refusal/kubectl-handout answer - forcing a corrective pass")
        corrective = model.invoke(curr_messages + [HumanMessage(content=(
            "That answer is wrong for this system. You are not a chatbot giving advice: a "
            "Remediation Agent runs right after you and executes real write tools (create, apply, "
            "patch, scale, delete) against the cluster. Never tell the user to run kubectl "
            "themselves and never say you cannot execute things. Rewrite your report now: state "
            "precisely which resources must be created/deleted/changed (exact names, namespaces, "
            "images, and for deletions the order), and end with 'REMEDIATION_NEEDED: YES' so the "
            "action actually gets carried out. Only keep 'REMEDIATION_NEEDED: NO' if the user "
            "genuinely asked a read-only question and requested no change whatsoever."
        ))])
        record_usage("diagnostics", corrective)
        corrected = clean_message_content(corrective.content)
        if corrected.strip():
            final_response = corrected

    # user_request is returned as its own state field below (propose_remediation_node and
    # verify_remediation_node both read it directly) rather than prepended into the report text -
    # prepending it here used to cause a visible duplicate "User request: ..." in the diagnostic
    # report shown to the user, since the model's own response sometimes already opens by
    # restating the request itself.
    # One line per "ISSUE: ..." the model called out (see prompt guideline 4) - empty when it
    # found zero or one problem, which is the common case and needs no selection step at all
    # (route_after_diagnose below only detours through select_issues when this has >1 entry).
    issues = re.findall(r"^ISSUE:\s*(.+)$", final_response, re.MULTILINE)

    # Parse the routing flag into real state, then strip it (and the ISSUE: prefixes, which are a
    # machine-readable marker for the selection UI) out of the prose the user actually reads.
    remediation_needed, final_response = _extract_flag(final_response, "REMEDIATION_NEEDED")
    final_response = re.sub(r"^ISSUE:\s*", "- ", final_response, flags=re.MULTILINE)

    return {
        "messages": [AIMessage(content=f"Diagnostic Report:\n{final_response}")],
        "user_request": user_request,
        "diagnostic_report": final_response,
        "remediation_needed": remediation_needed,
        "issues": issues,
    }

def generate_proposal(diagnostic_report: str, user_request: str) -> str:
    """
    Core "propose a fix" logic, factored out of propose_remediation_node so main.py's guidance
    endpoint can generate a redirected proposal (incorporating a human's free-text instruction)
    without running it through the graph - the graph has no node to "re-propose before the first
    attempt", only a dedicated retry path for after a failed verification.

    Bound to the read-only diag_tools (not remedy_tools - this step only plans, never executes) so
    the model can double-check a resource's EXACT current spec before finalizing a patch, instead
    of relying entirely on whatever diagnose_node's natural-language report happened to preserve. A
    real deployed test caught the gap this closes: the diagnostic report said a container's
    ConfigMap reference was broken, but didn't spell out the container's exact env/envFrom
    structure - the model (with no tools here, previously) proposed a patch that only ADDED the
    correct envFrom, leaving the pre-existing broken env entry untouched (Kubernetes still refuses
    to start the container while ANY referenced ConfigMap is missing, old or new), and the fix
    silently failed to resolve anything despite being "applied successfully."
    """
    model = get_remediation_model()
    model_with_tools = model.bind_tools(diag_tools)

    prompt = (
        f"You are a Kubernetes Remediation Specialist. Based on the following Diagnostic Report, "
        f"propose a precise fix/remediation action.\n\n"
        f"Original user request: {user_request}\n\n"
        f"Diagnostic Report:\n{diagnostic_report}\n\n"
        f"Instructions:\n"
        f"1. Be concrete and specific to THIS report only - do not write generic/hypothetical Kubernetes advice or invent scenarios that aren't in the report. If a '--- Human guidance for the next attempt ---' section is present above, that instruction OVERRIDES your own judgment about approach - follow it exactly, only filling in specifics (tool arguments) it left unspecified. If a '--- Previous fix attempt ---' section shows an action that was REFUSED (e.g. 'kind is not in the allowed set') or structurally rejected rather than just not-yet-successful, do NOT propose that exact same action again - it will be refused identically every time, no matter how many attempts remain. Either propose a genuinely different approach that reaches the same goal without the disallowed action, or state plainly that this fix is outside your permitted capability and what a human would need to do manually instead - repeating a known-refused call wastes every remaining attempt.\n"
        f"2. Before finalizing any patch that touches a container's env/envFrom/volumes (attaching, replacing, or removing a ConfigMap/Secret reference), call get_resource or describe_deployment yourself to see that container's CURRENT exact env/envFrom - do not assume the diagnostic report already spelled out every existing entry. If the container already has ANY env/envFrom entry referencing a ConfigMap/Secret that's wrong, stale, or now missing, your patch MUST explicitly clear or correct that existing entry (an empty list removes it) - Kubernetes refuses to start a container while ANY referenced ConfigMap/Secret is missing, even one you're not otherwise touching, so adding a new working reference alongside an untouched broken one does NOT fix anything. Only proceed straight to stating the plan (no tool calls needed) for issues that don't involve env/envFrom/volumes.\n"
        f"3. State exactly which tool you will call and with which arguments (e.g. create_namespace(namespace_name='demo-2') or create_pod(pod_name='test-agent', image='nginx:latest', namespace='demo-2')), using any names/values from the report verbatim. If the report already states a default image to use, use that exact image.\n"
        f"   ALWAYS pass the explicit namespace of the resource you are actually fixing, copied exactly from the report (a namespace named 'max-ns' is not 'max'). Never omit it and never substitute 'default': the fix must land in the SAME namespace as the broken resource. A real deployed test omitted the namespace and created a PVC in 'default' while the broken PVC lived in 'max-ns', then reported that as a success.\n"
        f"   If the user EXPLICITLY asked for something to be created, deleted or cleaned up, that request is itself the goal - propose the tool calls that carry it out (delete_resource for each object named, in dependency order; apply_kubernetes_yaml to create). Do not refuse it, do not second-guess it, and never answer with a kubectl command for the user to run: a separate agent executes your plan with real write tools.\n"
        f"   Otherwise, when you are FIXING a fault rather than carrying out an explicit request, deleting is NOT a fix. Never propose delete_resource on a resource that is currently healthy/in use just because something referencing it is broken (e.g. do not delete a Bound PersistentVolume while troubleshooting a PVC). In that fault-fixing case only propose a delete when the object itself is the broken thing AND it must be recreated because the field you need to change is immutable - say so explicitly when you do, and pair it with the recreate.\n"
        f"   When a delete+recreate IS the right move, read the object's CURRENT full spec with get_resource FIRST and state the complete replacement manifest, carrying over every field the object already had (for a PVC: accessModes, resources.requests, storageClassName, and any selector; for a Pod: the whole spec). Deleting first and only then discovering the manifest is incomplete leaves the resource GONE and the cluster worse than before - a real deployed test deleted a PVC and then failed to recreate it twice because the manifest was missing accessModes, wasting an entire attempt with the PVC missing the whole time.\n"
        f"4. Match the tool to the root cause:\n"
        f"   - Bad/unpullable image (ImagePullBackOff, ErrImagePull, InvalidImageName) on a Deployment-owned pod: patch_deployment_image(deployment_name=..., container_name=..., image=..., namespace=...) so the fix survives the next rollout; for a standalone pod use update_pod_image instead. Do NOT use restart_pod/rollout_restart_deployment for a bad image - the error is baked into the spec and a restart alone just fails the same way again.\n"
        f"   - Attaching/mounting a ConfigMap or Secret to a container (as env vars via envFrom, individual values via env, or as a volume), changing env vars, resource requests/limits, labels, or any other Deployment field that isn't the image: patch_deployment(deployment_name=..., namespace=..., patch={{...}}) with a strategic-merge-patch dict targeting spec.template.spec.containers (matched by container name). Putting a ConfigMap/Secret's name into the 'image' field, or into an annotation, does NOT attach it - state the actual envFrom/env/volumes patch, including the full corrected list per instruction 2 above.\n"
        f"   - Deployment-owned pod in a genuinely transient crash loop where the spec is already correct: rollout_restart_deployment rather than restart_pod (restart_pod only deletes one pod; rollout_restart_deployment cleanly recreates all replicas). Only propose restart_pod for a standalone pod's transient failure.\n"
        f"   - A Deployment has an extra/duplicate/wrong container that needs to be REMOVED entirely (not just an image or env fix): patch_deployment_image and patch_deployment can only add or update a container by name, never remove one, so use apply_kubernetes_yaml with the FULL corrected manifest instead (every container that should remain, none that shouldn't) - it replaces the whole containers list rather than merging into it.\n"
        f"   - A PVC is stuck Pending because its StorageClass has provisioner 'kubernetes.io/no-provisioner' (or similar manual-provisioning provisioners): this is BY DESIGN - that StorageClass type never dynamically provisions anything, a matching PersistentVolume must exist. The normal fix is apply_kubernetes_yaml with a PersistentVolume whose storageClassName, accessModes and capacity match the PVC - and, crucially, whose metadata.labels satisfy the PVC's spec.selector if it has one (a PV that matches on size/class but not the selector will just sit Available while the PVC stays Pending), plus nodeAffinity if volumeBindingMode is WaitForFirstConsumer. Read the PVC with get_resource first so you match all of it. Changing the StorageClass instead is possible but heavier: a StorageClass's provisioner is immutable, so it means delete_resource + recreate, and it affects every PVC using that class, not just this one - prefer the PV unless the user explicitly asked to change the class.\n"
        f"5. Keep your FINAL answer short: 2-4 sentences stating the plan. Do not call apply/write tools - only the read-only lookups needed for instruction 2, if any."
    )

    curr_messages = [HumanMessage(content=prompt)]
    limit = 4
    for _ in range(limit):
        response = model_with_tools.invoke(curr_messages)
        record_usage("remediation", response)
        curr_messages.append(response)

        if response.tool_calls:
            for tc in response.tool_calls:
                tool_func = diag_tool_map.get(tc["name"])
                if tool_func:
                    logger.info(f"tool_call propose {tc['name']} args={tc['args']}")
                    result = invoke_tool_safely(tool_func, tc["args"])
                    record_tool_call(tc["name"], result)
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=(
                            f"Error: Tool '{tc['name']}' does not exist - it was never provided to you. "
                            f"Your real available tools are: {sorted(diag_tool_map.keys())}. "
                            f"Retry using one of those instead."
                        ),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        else:
            break
    else:
        final = model.invoke(curr_messages + [HumanMessage(content=(
            "You have reached your lookup limit. Based on everything you have found so far, state "
            "your final plan now (2-4 sentences) - do not call any more tools."
        ))])
        record_usage("remediation", final)
        curr_messages.append(final)

    return clean_message_content(curr_messages[-1].content)


@_timed_node("propose_remediation")
def propose_remediation_node(state: AgentState) -> Dict[str, Any]:
    """
    Remediation model node (Propose Fix).
    Analyzes the diagnostics report and proposes specific remediation actions.
    """
    final_response = generate_proposal(state["diagnostic_report"], state.get("user_request", ""))
    return {
        "messages": [AIMessage(content=f"Proposed Remediation:\n{final_response}")],
        "proposed_fix": final_response
    }

@_timed_node("apply_remediation")
def apply_remediation_node(state: AgentState) -> Dict[str, Any]:
    """
    Remediation model node (Apply Fix).
    Executes the proposed fix after getting user approval - unless REQUIRE_APPROVAL=false, in
    which case there's no external approval step to check (the graph never paused before this
    node - see REQUIRE_APPROVAL/compiled_graph below), so it proceeds unconditionally.
    """
    if REQUIRE_APPROVAL and state.get("approval_status") != "approved":
        return {
            "messages": [AIMessage(content="Remediation was not approved. Skipping fix execution.")],
            "fix_result": "Skipped (not approved)"
        }

    model = get_remediation_model()
    # Both write tools (remedy_tools) AND read tools (diag_tools) are bound here - this node used
    # to only be able to write, blind to the resource's exact current state, and independently
    # repeated the exact mistake propose_remediation_node used to make (see generate_proposal's
    # docstring): it constructed a patch_deployment call from the plan's PROSE alone, adding a
    # correct envFrom but never noticing/clearing a pre-existing broken env entry, because it had
    # no way to actually look at the container's current spec before writing the patch. Giving it
    # read tools too closes that gap at the layer that actually matters - the one building the real
    # tool-call arguments, not just the one describing the plan in English.
    apply_tool_map = {**remedy_tool_map, **diag_tool_map}
    model_with_tools = model.bind_tools(remedy_tools + diag_tools)

    prompt = (
        f"You are a Kubernetes Remediation Specialist. The user has APPROVED the following proposed fix:\n"
        f"{state['proposed_fix']}\n\n"
        f"Diagnostic Report context:\n"
        f"{state['diagnostic_report']}\n\n"
        f"You MUST apply this fix by calling a WRITE tool (restart_pod, update_pod_image, "
        f"patch_deployment_image, patch_deployment, rollout_restart_deployment, scale_deployment, "
        f"apply_kubernetes_yaml, create_namespace, create_pod, delete_resource) with the exact values "
        f"needed. Writing out a kubectl command in text does NOT apply anything - only an actual tool "
        f"call changes the cluster.\n"
        f"KNOW YOUR TARGET BEFORE YOU WRITE. Do not assume what kind of object something is from its "
        f"name: a name ending in '-pod' is often a bare Pod with no Deployment behind it. If you are "
        f"not certain of the object's kind, its exact container names, or whether it is controlled by "
        f"a Deployment, call get_resource/describe_pod FIRST and use what it returns. Pick the pod-level "
        f"tools (update_pod_image, restart_pod) for a standalone Pod and the Deployment-level ones only "
        f"when a Deployment genuinely owns it. If a call comes back 404 'not found', the object does not "
        f"exist under that kind/name - do NOT reissue the same call against the same kind, look up what "
        f"actually exists instead. A real deployed test wasted an entire attempt calling "
        f"patch_deployment twice on a name that was only ever a bare Pod.\n"
        f"When recreating an object from a live one (delete + apply_kubernetes_yaml), write a CLEAN "
        f"manifest: keep apiVersion/kind/metadata.name/metadata.namespace/labels and the spec fields "
        f"that matter, and DROP everything the cluster fills in by itself - status, resourceVersion, "
        f"uid, creationTimestamp, managedFields, nodeName, serviceAccount token volumes/volumeMounts "
        f"(anything named kube-api-access-*), default tolerations, priority, preemptionPolicy, "
        f"enableServiceLinks. Copying a live object's full JSON back in verbatim produces a fragile or "
        f"invalid manifest. Pass it as real YAML, not a single-line JSON blob.\n"
        f"If the fix touches a container's env/envFrom/volumes (attaching, replacing, or clearing a "
        f"ConfigMap/Secret reference) and the plan above doesn't already spell out the container's "
        f"exact current env/envFrom, call get_resource or describe_deployment FIRST (you also have "
        f"read tools available) to see it before constructing the patch - if the container already "
        f"has ANY env/envFrom entry referencing a ConfigMap/Secret that's wrong or missing, your patch "
        f"must explicitly clear/correct that existing entry (an empty list removes it), not just add a "
        f"new one alongside it; Kubernetes refuses to start the container while any referenced "
        f"ConfigMap/Secret is missing, even one you're not otherwise touching. Then call the write tool."
    )

    curr_messages = [
        SystemMessage(content=(
            "You apply fixes to the cluster by calling tools - read tools to check current state if "
            "needed, then a write tool to actually change something. Never describe a shell/kubectl "
            "command as if it were run - if you don't call a write tool, nothing changes. Never claim "
            "success unless you actually called a write tool."
        )),
        HumanMessage(content=prompt),
    ]
    limit = 7  # bumped from 5 - a read lookup or two now often precedes the actual write call
    # (tool_name, args, result) for every tool call that actually executed.
    tool_invocations = []
    # (name, sorted args items) for calls already executed in this turn, so duplicate
    # tool_calls in the same response (some Bedrock models occasionally emit the same call twice)
    # aren't re-run against the cluster.
    executed_signatures = set()
    for _ in range(limit):
        response = model_with_tools.invoke(curr_messages)
        record_usage("remediation", response)
        curr_messages.append(response)

        if response.tool_calls:
            for tc in response.tool_calls:
                # json.dumps (not tuple(sorted(...items()))) - a tool arg can itself be a dict
                # (patch_deployment's 'patch' argument), and a tuple containing a nested dict isn't
                # hashable, which crashed this dedup check with "TypeError: unhashable type: 'dict'"
                # on a real deployed test the moment the model called patch_deployment. A JSON
                # string is always hashable regardless of what the args contain.
                signature = (tc["name"], json.dumps(tc["args"], sort_keys=True, default=str))
                if signature in executed_signatures:
                    curr_messages.append(HumanMessage(
                        content="Skipped: this exact tool call was already executed in this turn.",
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                    continue
                tool_func = apply_tool_map.get(tc["name"])
                if tool_func:
                    logger.info(f"tool_call remediation {tc['name']} args={tc['args']}")
                    result = invoke_tool_safely(tool_func, tc["args"])
                    record_tool_call(tc["name"], result)
                    # Only WRITE calls count toward fix_result/tool_invocations (what actually
                    # changed the cluster) - a read lookup used to check current state first is
                    # real evidence-gathering, not an "action applied", so it shouldn't be reported
                    # as one or affect the SUCCEEDED/FAILED classification below.
                    if tc["name"] in remedy_tool_map:
                        tool_invocations.append((tc["name"], tc["args"], result))
                    executed_signatures.add(signature)
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=(
                            f"Error: Tool '{tc['name']}' does not exist - it was never provided to you. "
                            f"Your real available tools are: {sorted(apply_tool_map.keys())}. "
                            f"Retry using one of those instead."
                        ),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        elif not tool_invocations:
            # Model responded with prose instead of a tool call (Bedrock's Converse tool_choice
            # support varies by model, so it can't always be forced) - nudge it and give it
            # another try rather than trusting whatever text it wrote.
            curr_messages.append(HumanMessage(
                content=(
                    "You did not call a tool, so NOTHING happened - your text output is discarded "
                    "and this counts as a failed attempt. Do not explain, do not apologise, do not "
                    "restate the plan: emit an actual function call to one of your write tools "
                    "right now. If you are unsure of a value (a container name, the object's kind), "
                    "call a read tool such as get_resource or describe_pod first and then make the "
                    "write call."
                )
            ))
        else:
            break

    if not tool_invocations:
        fix_result = (
            "Remediation FAILED: no tool was ever invoked, so nothing was changed on the cluster. "
            "The model only produced text describing what it would run."
        )
    else:
        # Classify every call, not just failures - a real deployed test caught a case where one
        # call failed (case-mismatched kind) and a later call in the same turn succeeded (recreated
        # the pod), but the old logic only ever surfaced the failure list whenever ANY call failed,
        # silently dropping the successful action from the report entirely - which also skipped
        # verify_remediation_node (it only runs unless fix_result starts with "Remediation FAILED"),
        # so a fix that actually worked was reported and treated as a total failure.
        successes, failures = [], []
        for name, args, result in tool_invocations:
            line = f"- {_describe_tool_call(name, args)}\n  {_condense_result(result)}"
            result_lower = str(result).lower()
            # Checks for "error" or "failed" ANYWHERE in the result, not just a leading "Error"
            # prefix - apply_kubernetes_yaml reports its own per-document failures as
            # "Kind/name: FAILED - ...", which a startswith("error") check never catches, so a
            # call whose only outcome was a hard failure still got classified and reported as a
            # success (confirmed via a real deployed test - see SPEC.md). No legitimate success
            # message in this codebase contains either word, so this is safe in that direction.
            is_failure = "error" in result_lower or "failed" in result_lower
            (failures if is_failure else successes).append(line)

        if successes:
            # Deliberately NOT called "Remediation SUCCEEDED" - that wording claims the underlying
            # problem is fixed, but all this confirms is that the Kubernetes API call itself didn't
            # error (e.g. a patch can apply cleanly and still be the wrong patch for the actual
            # root cause). verify_remediation_node re-inspects the cluster afterward and is the
            # only step that actually confirms the original goal was achieved - a real deployed
            # test showed this label being read as "problem solved" even when verification
            # immediately below it said GOAL_ACHIEVED: NO.
            status = "Actions Applied" if not failures else "Actions Partially Applied"
            fix_result = f"{status} (not yet confirmed - see verification below):\n" + "\n".join(successes)
            if failures:
                fix_result += "\n\nActions that failed:\n" + "\n".join(failures)
        else:
            fix_result = "Remediation FAILED for all actions:\n" + "\n".join(failures)

    return {
        "messages": [AIMessage(content=f"Fix Execution Report:\n{fix_result}")],
        "fix_result": fix_result,
        "attempt": state.get("attempt", 0) + 1,
    }

@_timed_node("verify_remediation")
def verify_remediation_node(state: AgentState) -> Dict[str, Any]:
    """
    Nova Pro model node (Verify).
    Re-inspects the cluster after a fix has been applied to confirm the user's original
    goal was actually achieved, rather than trusting that a successful tool call means the
    underlying problem is resolved (e.g. a pod can be restarted successfully via the API
    and still immediately crash-loop again).
    """
    fix_result = state.get("fix_result", "")
    if fix_result.startswith("Remediation FAILED") or fix_result.startswith("Skipped"):
        verification = (
            f"GOAL_ACHIEVED: NO\n"
            f"No verification performed - the remediation itself did not succeed:\n{fix_result}"
        )
        return {
            "messages": [AIMessage(content=f"Verification Report:\n{verification}")],
            "verification_result": verification,
        }

    import time
    # 8s was tuned for fast operations (pod restart, image pull) - a real deployed test caught it
    # being too short for cloud block-storage provisioning (PVC recreate -> Bound -> pod Running
    # took ~30s total), so verification checked while the fix was still settling and reported
    # GOAL_ACHIEVED: NO on a fix that had actually already succeeded moments later. The retry loop
    # doesn't re-diagnose fresh before re-proposing, so a premature "not achieved" here risks the
    # next attempt undoing a fix that was already working. 15s trades a bit of latency on every
    # remediation for meaningfully fewer premature-failure verdicts on slower-settling changes.
    time.sleep(15)

    model = get_diagnostics_model()
    model_with_tools = model.bind_tools(diag_tools)

    prompt = (
        f"You are a Kubernetes Verification Specialist. A fix was just applied to the cluster. "
        f"Your job is to confirm whether the user's ORIGINAL goal was actually achieved - do not "
        f"assume success just because the tool call reported success.\n\n"
        f"Original user request: {state.get('user_request', '')}\n\n"
        f"Diagnostic Report (before fix):\n{state.get('diagnostic_report', '')}\n\n"
        f"Proposed Fix:\n{state.get('proposed_fix', '')}\n\n"
        f"Fix Execution Result:\n{fix_result}\n\n"
        f"Instructions:\n"
        f"1. Use your tools (get_pod_status, describe_pod, get_pod_logs, get_pod_events, list_namespaces, "
        f"list_deployments, describe_deployment, and any other relevant read tool) to re-inspect the exact "
        f"resource(s) that were changed - if a Deployment was patched/restarted, check its rollout status too, "
        f"not just the pods that happen to exist right now.\n"
        f"2. If the original issue was a crash/CrashLoopBackOff, confirm the pod is now Running and Ready, "
        f"and that its restart count/container state doesn't show it crashing again (a pod can look Running "
        f"for a few seconds and then crash again, so check the container state/reason too, not just phase).\n"
        f"3. If the request was to create/scale a resource, confirm the resource now exists with the "
        f"requested spec (e.g. correct replica count, image, namespace).\n"
        f"4. Quote the exact status/state you observed as evidence, including the resource's exact namespace as the object reports it (a namespace called 'max-ns' is not 'max' - do not paraphrase or shorten it; if you looked in the wrong namespace and found nothing, that is NOT evidence the fix worked).\n"
        f"5. Base the verdict ONLY on the live object's current status, never on events - Kubernetes keeps events for about an hour after the fact, so a stale warning does not mean the problem is still there, and the absence of a fresh event does not mean it is fixed. Re-read the actual object. If you cannot find the resource you were supposed to check, say GOAL_ACHIEVED: NO and say you could not find it - never report success for a resource you did not actually observe in a healthy state. A real deployed test had this step report 'the PVC is now Bound' while the live PVC was still Pending and the PV it named had been deleted.\n"
        f"6. At the very end of your response, append exactly one line:\n"
        f"   - 'GOAL_ACHIEVED: YES' if the evidence confirms the original problem is resolved.\n"
        f"   - 'GOAL_ACHIEVED: NO' if the evidence shows it is still failing or you could not confirm it.\n"
    )

    curr_messages = [
        SystemMessage(content=(
            "You verify Kubernetes fixes by re-inspecting the cluster with tools. Never claim "
            "success without fresh tool evidence gathered after the fix was applied."
        )),
        HumanMessage(content=prompt),
    ]
    limit = 6
    for _ in range(limit):
        response = model_with_tools.invoke(curr_messages)
        record_usage("diagnostics", response)
        curr_messages.append(response)

        if response.tool_calls:
            for tc in response.tool_calls:
                tool_func = diag_tool_map.get(tc["name"])
                if tool_func:
                    logger.info(f"tool_call verification {tc['name']} args={tc['args']}")
                    result = invoke_tool_safely(tool_func, tc["args"])
                    record_tool_call(tc["name"], result)
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=(
                            f"Error: Tool '{tc['name']}' does not exist - it was never provided to you. "
                            f"Your real available tools are: {sorted(diag_tool_map.keys())}. "
                            f"Retry using one of those instead."
                        ),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        else:
            break
    else:
        # Same failure mode as diagnose_node's loop (see the comment there): if every iteration
        # up to `limit` used a tool call, curr_messages[-1] is raw tool output with no
        # 'GOAL_ACHIEVED' line, so "GOAL_ACHIEVED: YES" in verification_result defaults to False -
        # fails safe here (reports "not achieved" rather than silently skipping remediation like
        # the diagnose case did), but still isn't a real conclusion. Force one last no-tools call.
        final = model.invoke(curr_messages + [HumanMessage(content=(
            "You have reached your investigation limit. Based on everything you have found so "
            "far, give your final verification verdict now - do not call any more tools. End "
            "with exactly one of 'GOAL_ACHIEVED: YES' or 'GOAL_ACHIEVED: NO' per your instructions."
        ))])
        record_usage("diagnostics", final)
        curr_messages.append(final)

    verification = clean_message_content(curr_messages[-1].content)
    goal_achieved, verification = _extract_flag(verification, "GOAL_ACHIEVED")
    return {
        "messages": [AIMessage(content=f"Verification Report:\n{verification}")],
        "verification_result": verification,
        "goal_achieved": goal_achieved,
    }

def select_issues_node(state: AgentState) -> Dict[str, Any]:
    """
    No-op passthrough - exists purely to give interrupt_before a node name to pause on when
    diagnose_node found multiple distinct issues. main.py's /api/select-issues endpoint does the
    actual work (narrowing diagnostic_report to just what the human picked) via update_state
    before resuming past this node, same pattern as the existing approval/retry pauses.
    """
    return {}

def route_after_diagnose(state: AgentState) -> str:
    """
    Routes to remediation proposal if remediation is flagged as YES, otherwise finishes - via a
    selection detour first when diagnose_node found more than one distinct issue (single/no-issue
    reports skip straight to propose_remediation exactly as before, no extra step added).
    """
    # Reads the parsed boolean, not the raw text - the flag line is stripped out of
    # diagnostic_report before the user ever sees it (see _extract_flag).
    if not state.get("remediation_needed"):
        return END
    if len(state.get("issues", []) or []) > 1:
        return "select_issues"
    return "propose_remediation"

def route_after_verify(state: AgentState) -> str:
    """
    Loops back to propose a new fix if the goal wasn't achieved and attempts remain,
    otherwise finishes.
    """
    if state.get("goal_achieved"):
        REMEDIATION_OUTCOMES.labels(outcome="succeeded").inc()
        return END
    if state.get("attempt", 0) >= MAX_ATTEMPTS:
        REMEDIATION_OUTCOMES.labels(outcome="exhausted").inc()
        return END
    REMEDIATION_OUTCOMES.labels(outcome="retrying").inc()
    return "propose_retry"

# 4. Build StateGraph

workflow = StateGraph(AgentState)

# Add Nodes
# "propose_retry" reuses propose_remediation_node under a distinct name so it can carry
# its own interrupt: the initial proposal (right after diagnose) needs no separate gate,
# but every retry proposal needs a "do you want to try again?" pause of its own.
workflow.add_node("diagnose", diagnose_node)
workflow.add_node("select_issues", select_issues_node)
workflow.add_node("propose_remediation", propose_remediation_node)
workflow.add_node("propose_retry", propose_remediation_node)
workflow.add_node("apply_remediation", apply_remediation_node)
workflow.add_node("verify_remediation", verify_remediation_node)

# Set Entry Point
workflow.set_entry_point("diagnose")

# Add Transitions
workflow.add_conditional_edges(
    "diagnose",
    route_after_diagnose,
    {
        "select_issues": "select_issues",
        "propose_remediation": "propose_remediation",
        END: END
    }
)
workflow.add_edge("select_issues", "propose_remediation")
workflow.add_edge("propose_remediation", "apply_remediation")
workflow.add_edge("propose_retry", "apply_remediation")
workflow.add_edge("apply_remediation", "verify_remediation")
workflow.add_conditional_edges(
    "verify_remediation",
    route_after_verify,
    {
        "propose_retry": "propose_retry",
        END: END
    }
)

# Checkpointer persists AgentState per thread_id, replacing the manual SESSIONS dict in
# main.py. interrupt_before freezes execution right before applying a fix (needs approval),
# right before proposing a retry (needs a "try again?" decision), and right before
# select_issues (needs an issue-selection decision, only reached at all when diagnose_node found
# more than one distinct issue - see route_after_diagnose) - main.py resumes each pause with
# compiled_graph.invoke(None, config) once the human has decided. Skipped entirely when
# REQUIRE_APPROVAL=false: the graph then never pauses (select_issues still runs, just without a
# pause, implicitly treating every issue found as selected), so a single compiled_graph.invoke()
# runs diagnose through verify (and any retries) end to end with no human in the loop - see
# REQUIRE_APPROVAL above and apply_remediation_node's approval check.
# build_checkpointer() uses a Postgres-backed saver when DATABASE_URL is set (required for
# >1 replica / surviving pod restarts), falling back to in-process MemorySaver otherwise.
checkpointer = build_checkpointer()
compiled_graph = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["select_issues", "apply_remediation", "propose_retry"] if REQUIRE_APPROVAL else [],
)

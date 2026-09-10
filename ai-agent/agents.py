from typing import TypedDict, List, Annotated, Dict, Any
import operator
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from bedrock_clients import get_diagnostics_model, get_remediation_model
from usage_tracker import record_usage
from k8s_tools import (
    list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod,
    restart_pod, scale_deployment, apply_kubernetes_yaml, create_namespace, create_pod,
    update_pod_image
)

# 1. State Definition
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    user_request: str
    diagnostic_report: str
    proposed_fix: str
    approval_status: str  # "pending", "approved", "rejected"
    fix_result: str
    verification_result: str
    attempt: int

MAX_ATTEMPTS = 3

# 2. Bind Tools to ChatModels
diag_tools = [list_namespaces, get_pod_status, get_pod_logs, get_pod_events, describe_pod]
remedy_tools = [restart_pod, update_pod_image, scale_deployment, apply_kubernetes_yaml, create_namespace, create_pod]

# Map tool names to tool functions for execution
diag_tool_map = {t.name: t for t in diag_tools}
remedy_tool_map = {t.name: t for t in remedy_tools}

def clean_message_content(content) -> str:
    """Helper to convert complex message content structures (e.g. block lists) to clean raw strings."""
    if isinstance(content, str):
        return content
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
        return "".join(parts).strip()
    return str(content)

# 3. Define Nodes

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
            "2. If the user asks for the root cause of a crash/failure, you MUST investigate before answering - do not guess or list generic possible causes:\n"
            "   a. Call get_pod_status (or describe_pod) first to see the container's current/last state, exit code, and restart count.\n"
            "   b. If restart_count > 0 or the container is not Running, call get_pod_logs with previous=True to get the crashed container's actual log output - the current container may be empty or mid-startup, so the previous instance has the real error.\n"
            "   c. Call describe_pod for conditions, resource requests/limits, and termination reason/message (e.g. OOMKilled, non-zero exit code, ImagePullBackOff).\n"
            "   d. Call get_pod_events for scheduling/probe/pull failures that don't show up in logs.\n"
            "   e. Your report must state the EXACT error message, exit code, or reason found in the tool output (quote it), not a hypothetical list of possible causes. Only say the cause is uncertain if the tools genuinely returned no useful evidence after trying (a)-(d).\n"
            "3. If the user is reporting a fault/issue, requesting a modification/creation (e.g. creating a namespace, creating a pod, restarting resources, scaling), or if you detect a failing resource, describe EXACTLY what needs to be changed or created, including any resource names/values the user gave you verbatim (e.g. namespace name, pod name, container image, replica count, deployment name). If the user wants a pod created but did not specify a container image, pick a sensible default (e.g. 'nginx:latest') and state it explicitly so the Remediation Agent doesn't have to guess. Do not try to apply modifications yourself (as you only have read-only tools). Summarize the requested change in your own report and output 'REMEDIATION_NEEDED: YES' so the Remediation Agent can apply it.\n"
            "   Match the fix to the ROOT CAUSE, don't default to restarting: if the container state/reason is ImagePullBackOff, ErrImagePull, or InvalidImageName, the fix is to correct the image (state the exact correct image string), NOT to restart/delete the pod - the error is baked into the pod spec and deleting it just fails again the same way (or, if describe_pod shows 'standalone_pod: true' i.e. no owner_references, deleting it PERMANENTLY removes it since nothing recreates it). Only recommend a restart for genuinely transient failures (e.g. a one-off crash where the spec itself is correct).\n"
            "4. Crucial: At the very end of your final response, append exactly one of the following lines:\n"
            "   - 'REMEDIATION_NEEDED: YES' (if the user requested a modification/creation, or if there is an active failure/configuration error that requires a write action)\n"
            "   - 'REMEDIATION_NEEDED: NO' (if it's a read-only query, or if all resources are healthy and no changes are needed)\n"
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
                    print(f"\n[Diagnostics Agent] Calling tool '{tc['name']}' with args: {tc['args']}")
                    result = tool_func.invoke(tc["args"])
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=f"Error: Tool '{tc['name']}' not found.",
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        else:
            # No tool calls, we are finished diagnosing
            break

    final_response = clean_message_content(curr_messages[-1].content)
    # Guard against a terse/empty report (e.g. just "REMEDIATION_NEEDED: YES") losing the
    # original ask - always prepend the verbatim user request so downstream nodes have it.
    full_report = f"User request: {user_request}\n\n{final_response}" if user_request else final_response
    return {
        "messages": [AIMessage(content=f"Diagnostic Report:\n{final_response}")],
        "user_request": user_request,
        "diagnostic_report": full_report
    }

def propose_remediation_node(state: AgentState) -> Dict[str, Any]:
    """
    Llama model node (Propose Fix).
    Analyzes the diagnostics report and proposes specific remediation actions.
    """
    model = get_remediation_model()

    prompt = (
        f"You are a Kubernetes Remediation Specialist. Based on the following Diagnostic Report, "
        f"propose a precise fix/remediation action.\n\n"
        f"Diagnostic Report:\n{state['diagnostic_report']}\n\n"
        f"Instructions:\n"
        f"1. Be concrete and specific to THIS report only - do not write generic/hypothetical Kubernetes advice or invent scenarios that aren't in the report.\n"
        f"2. State exactly which tool you will call and with which arguments (e.g. create_namespace(namespace_name='demo-2') or create_pod(pod_name='test-agent', image='nginx:latest', namespace='default')), using any names/values from the report verbatim. If the report already states a default image to use, use that exact image.\n"
        f"3. Match the tool to the root cause: for a bad/unpullable image (ImagePullBackOff, ErrImagePull, InvalidImageName) use update_pod_image(pod_name=..., container_name=..., image=..., namespace=...) with the corrected image - do NOT use restart_pod for this, since restart_pod only deletes the pod and Kubernetes will not recreate it unless it's owned by a Deployment/ReplicaSet/etc. (and even if it is, the new pod would have the exact same bad image and fail again). Only propose restart_pod for genuinely transient failures where the pod spec itself is already correct.\n"
        f"4. Keep it short: 2-4 sentences. Do not run any tools yet, just state the plan."
    )

    response = model.invoke([HumanMessage(content=prompt)])
    record_usage("remediation", response)
    final_response = clean_message_content(response.content)
    return {
        "messages": [AIMessage(content=f"Proposed Remediation:\n{final_response}")],
        "proposed_fix": final_response
    }

def apply_remediation_node(state: AgentState) -> Dict[str, Any]:
    """
    Llama model node (Apply Fix).
    Executes the proposed fix after getting user approval.
    """
    if state.get("approval_status") != "approved":
        return {
            "messages": [AIMessage(content="Remediation was not approved. Skipping fix execution.")],
            "fix_result": "Skipped (not approved)"
        }

    model = get_remediation_model()
    model_with_tools = model.bind_tools(remedy_tools)

    prompt = (
        f"You are a Kubernetes Remediation Specialist. The user has APPROVED the following proposed fix:\n"
        f"{state['proposed_fix']}\n\n"
        f"Diagnostic Report context:\n"
        f"{state['diagnostic_report']}\n\n"
        f"You MUST apply this fix by calling one of your tools (restart_pod, update_pod_image, scale_deployment, "
        f"apply_kubernetes_yaml, create_namespace, create_pod) with the exact values from the plan above. "
        f"Writing out a kubectl command in text does NOT apply anything - only an actual tool call "
        f"changes the cluster. Call the tool now."
    )

    curr_messages = [
        SystemMessage(content=(
            "You apply fixes to the cluster strictly by calling tools. Never describe a shell/kubectl "
            "command as if it were run - if you don't call a tool, nothing happens. Never claim success "
            "unless you actually called a tool."
        )),
        HumanMessage(content=prompt),
    ]
    limit = 5
    # (tool_name, args, result) for every tool call that actually executed.
    tool_invocations = []
    # (name, sorted args items) for calls already executed in this turn, so duplicate
    # tool_calls in the same response (Llama4 on Bedrock sometimes emits the same call twice)
    # aren't re-run against the cluster.
    executed_signatures = set()
    for _ in range(limit):
        response = model_with_tools.invoke(curr_messages)
        record_usage("remediation", response)
        curr_messages.append(response)

        if response.tool_calls:
            for tc in response.tool_calls:
                signature = (tc["name"], tuple(sorted(tc["args"].items())))
                if signature in executed_signatures:
                    curr_messages.append(HumanMessage(
                        content="Skipped: this exact tool call was already executed in this turn.",
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                    continue
                tool_func = remedy_tool_map.get(tc["name"])
                if tool_func:
                    print(f"\n[Remediation Agent] Applying fix via '{tc['name']}' with args: {tc['args']}")
                    result = tool_func.invoke(tc["args"])
                    tool_invocations.append((tc["name"], tc["args"], result))
                    executed_signatures.add(signature)
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=f"Error: Tool '{tc['name']}' not found.",
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        elif not tool_invocations:
            # Llama responded with prose instead of a tool call (Bedrock's Llama4 tool_choice
            # only supports "auto", so it can't be forced) - nudge it and give it another try
            # rather than trusting whatever text it wrote.
            curr_messages.append(HumanMessage(
                content="You did not call a tool. Call the appropriate tool function now - do not just describe the command."
            ))
        else:
            break

    if not tool_invocations:
        fix_result = (
            "Remediation FAILED: no tool was ever invoked, so nothing was changed on the cluster. "
            "The model only produced text describing what it would run."
        )
    else:
        failures = [
            f"{name}({args}) -> {result}"
            for name, args, result in tool_invocations
            if str(result).lower().startswith("error")
        ]
        if failures:
            fix_result = "Remediation FAILED for one or more actions:\n" + "\n".join(failures)
        else:
            applied = "; ".join(f"{name}({args}) -> {result}" for name, args, result in tool_invocations)
            fix_result = f"Remediation SUCCEEDED. Actions applied: {applied}"

    return {
        "messages": [AIMessage(content=f"Fix Execution Report:\n{fix_result}")],
        "fix_result": fix_result,
        "attempt": state.get("attempt", 0) + 1,
    }

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
    time.sleep(8)  # let the cluster settle (pod restart/scheduling) before re-checking state

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
        f"1. Use your tools (get_pod_status, describe_pod, get_pod_logs, get_pod_events, list_namespaces) "
        f"to re-inspect the exact resource(s) that were changed.\n"
        f"2. If the original issue was a crash/CrashLoopBackOff, confirm the pod is now Running and Ready, "
        f"and that its restart count/container state doesn't show it crashing again (a pod can look Running "
        f"for a few seconds and then crash again, so check the container state/reason too, not just phase).\n"
        f"3. If the request was to create/scale a resource, confirm the resource now exists with the "
        f"requested spec (e.g. correct replica count, image, namespace).\n"
        f"4. Quote the exact status/state you observed as evidence.\n"
        f"5. At the very end of your response, append exactly one line:\n"
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
                    print(f"\n[Verification Agent] Calling tool '{tc['name']}' with args: {tc['args']}")
                    result = tool_func.invoke(tc["args"])
                    curr_messages.append(HumanMessage(
                        content=str(result),
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
                else:
                    curr_messages.append(HumanMessage(
                        content=f"Error: Tool '{tc['name']}' not found.",
                        name=tc["name"],
                        additional_kwargs={"tool_call_id": tc["id"]}
                    ))
        else:
            break

    verification = clean_message_content(curr_messages[-1].content)
    return {
        "messages": [AIMessage(content=f"Verification Report:\n{verification}")],
        "verification_result": verification,
    }

def route_after_diagnose(state: AgentState) -> str:
    """
    Routes to remediation proposal if remediation is flagged as YES, otherwise finishes.
    """
    report = state.get("diagnostic_report", "")
    if "REMEDIATION_NEEDED: YES" in report:
        return "propose_remediation"
    return END

def route_after_verify(state: AgentState) -> str:
    """
    Loops back to propose a new fix if the goal wasn't achieved and attempts remain,
    otherwise finishes.
    """
    if "GOAL_ACHIEVED: YES" in state.get("verification_result", ""):
        return END
    if state.get("attempt", 0) >= MAX_ATTEMPTS:
        return END
    return "propose_retry"

# 4. Build StateGraph

workflow = StateGraph(AgentState)

# Add Nodes
# "propose_retry" reuses propose_remediation_node under a distinct name so it can carry
# its own interrupt: the initial proposal (right after diagnose) needs no separate gate,
# but every retry proposal needs a "do you want to try again?" pause of its own.
workflow.add_node("diagnose", diagnose_node)
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
        "propose_remediation": "propose_remediation",
        END: END
    }
)
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
# main.py. interrupt_before freezes execution right before applying a fix (needs approval)
# and right before proposing a retry (needs a "try again?" decision) - main.py resumes
# each pause with compiled_graph.invoke(None, config) once the human has decided.
checkpointer = MemorySaver()
compiled_graph = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["apply_remediation", "propose_retry"],
)

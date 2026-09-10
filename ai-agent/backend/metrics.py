from prometheus_client import Counter, Histogram, make_asgi_app

TOOL_CALLS = Counter(
    "agent_tool_calls_total", "Kubernetes tool calls made by the agent", ["tool", "outcome"]
)
GRAPH_NODE_SECONDS = Histogram(
    "agent_graph_node_seconds", "LangGraph node execution duration", ["node"]
)
LLM_TOKENS = Counter(
    "agent_llm_tokens_total", "Bedrock tokens consumed", ["role", "direction"]
)
REMEDIATION_OUTCOMES = Counter(
    "agent_remediation_outcomes_total", "Remediation attempts by final outcome", ["outcome"]
)

# Mounted at /metrics in main.py - a standard Prometheus scrape target.
metrics_app = make_asgi_app()


def record_tool_call(tool_name: str, result) -> None:
    """Classifies a tool's string result as success/error for the agent_tool_calls_total counter."""
    outcome = "error" if str(result).lower().startswith("error") else "success"
    TOOL_CALLS.labels(tool=tool_name, outcome=outcome).inc()

import os
from collections import defaultdict

from .metrics import LLM_TOKENS

def _price(env_var: str) -> float:
    return float(os.getenv(env_var, "") or "0")

_PRICING = {
    "diagnostics": {
        "input_per_1k": _price("DIAGNOSTICS_MODEL_PRICE_INPUT_PER_1K"),
        "output_per_1k": _price("DIAGNOSTICS_MODEL_PRICE_OUTPUT_PER_1K"),
    },
    "remediation": {
        "input_per_1k": _price("REMEDIATION_MODEL_PRICE_INPUT_PER_1K"),
        "output_per_1k": _price("REMEDIATION_MODEL_PRICE_OUTPUT_PER_1K"),
    },
}

_usage = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0})

def record_usage(role: str, response) -> None:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return
    entry = _usage[role]
    input_tokens = meta.get("input_tokens", 0) or 0
    output_tokens = meta.get("output_tokens", 0) or 0
    entry["input_tokens"] += input_tokens
    entry["output_tokens"] += output_tokens
    entry["calls"] += 1
    LLM_TOKENS.labels(role=role, direction="input").inc(input_tokens)
    LLM_TOKENS.labels(role=role, direction="output").inc(output_tokens)

def get_summary() -> str:
    if not _usage:
        return "Token usage this session: no model calls recorded yet."

    lines = []
    total_cost = 0.0
    cost_available = False
    for role, entry in _usage.items():
        price = _PRICING.get(role, {"input_per_1k": 0, "output_per_1k": 0})
        cost = (
            entry["input_tokens"] / 1000 * price["input_per_1k"]
            + entry["output_tokens"] / 1000 * price["output_per_1k"]
        )
        cost_str = ""
        if price["input_per_1k"] or price["output_per_1k"]:
            cost_available = True
            total_cost += cost
            cost_str = f", ~${cost:.4f}"
        lines.append(
            f"  {role}: {entry['calls']} calls, {entry['input_tokens']} input tokens, "
            f"{entry['output_tokens']} output tokens{cost_str}"
        )

    header = "Token usage this session"
    if cost_available:
        header += f" (~${total_cost:.4f} estimated total)"
    else:
        header += " (set *_MODEL_PRICE_*_PER_1K env vars in .env for a cost estimate)"
    return header + ":\n" + "\n".join(lines)

def get_usage_dict() -> dict:
    roles = {}
    total_cost = 0.0
    cost_available = False
    for role, entry in _usage.items():
        price = _PRICING.get(role, {"input_per_1k": 0, "output_per_1k": 0})
        cost = (
            entry["input_tokens"] / 1000 * price["input_per_1k"]
            + entry["output_tokens"] / 1000 * price["output_per_1k"]
        )
        role_cost = None
        if price["input_per_1k"] or price["output_per_1k"]:
            cost_available = True
            total_cost += cost
            role_cost = round(cost, 4)
        roles[role] = {
            "calls": entry["calls"],
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "cost": role_cost,
        }
    return {
        "roles": roles,
        "total_cost": round(total_cost, 4) if cost_available else None,
    }

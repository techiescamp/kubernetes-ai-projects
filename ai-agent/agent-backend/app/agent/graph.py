from langgraph.graph import END, StateGraph

from ..infra.checkpointer import build_checkpointer
from .nodes import (
    apply_remediation_node, diagnose_node, propose_remediation_node, route_after_diagnose,
    route_after_verify, select_issues_node, verify_remediation_node,
)
from .state import MAX_ATTEMPTS, REQUIRE_APPROVAL, AgentState

workflow = StateGraph(AgentState)

workflow.add_node("diagnose", diagnose_node)
workflow.add_node("select_issues", select_issues_node)
workflow.add_node("propose_remediation", propose_remediation_node)
workflow.add_node("propose_retry", propose_remediation_node)
workflow.add_node("apply_remediation", apply_remediation_node)
workflow.add_node("verify_remediation", verify_remediation_node)

workflow.set_entry_point("diagnose")

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

checkpointer = build_checkpointer()
compiled_graph = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["select_issues", "apply_remediation", "propose_retry"] if REQUIRE_APPROVAL else [],
)

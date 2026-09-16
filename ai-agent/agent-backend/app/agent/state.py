import operator
import os
from typing import Annotated, List, TypedDict

from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    user_request: str
    diagnostic_report: str
    remediation_needed: bool
    goal_achieved: bool
    blocked: bool
    retry_context: str
    issues: List[str]
    proposed_fix: str
    approval_status: str
    fix_result: str
    verification_result: str
    attempt: int

MAX_ATTEMPTS = int(os.getenv("MAX_REMEDIATION_ATTEMPTS", "3"))

REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "true").lower() != "false"

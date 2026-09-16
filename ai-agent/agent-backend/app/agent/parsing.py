import json
import logging
import re
from typing import List

from langchain_core.messages import HumanMessage, SystemMessage

from ..infra.usage_tracker import record_usage

logger = logging.getLogger(__name__)

_TAG_FLAGS = {"remediation_needed": "REMEDIATION_NEEDED", "goal_achieved": "GOAL_ACHIEVED"}

def _normalise_model_markup(text: str) -> str:
    text = re.sub(
        r"<issue>\s*(.*?)\s*</issue>",
        lambda m: "\nISSUE: " + " ".join(m.group(1).split()) + "\n",
        text, flags=re.DOTALL | re.IGNORECASE,
    )
    for tag, flag in _TAG_FLAGS.items():
        text = re.sub(
            rf"<{tag}>\s*(YES|NO)\s*</{tag}>",
            lambda m, f=flag: f"\n{f}: {m.group(1).upper()}\n",
            text, flags=re.IGNORECASE,
        )
    text = re.sub(r"<([a-z][a-z0-9_-]*)>(.*?)</\1>", r"\2", text, flags=re.DOTALL | re.IGNORECASE)
    return text

def _strip_reasoning_markup(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"</?response>", "", text, flags=re.IGNORECASE)
    text = _normalise_model_markup(text)
    for _ in range(3):
        stripped = re.sub(
            r"^\s*(Diagnostic Report|Proposed Fix|Proposed Remediation|Remediation Plan|"
            r"Verification Report|Verification)\s*:\s*",
            "", text, flags=re.IGNORECASE,
        )
        if stripped == text:
            break
        text = stripped
    text = re.split(
        r"\n\s*(?:Proposed\s*\(awaiting approval\)|Proposed Fix|Proposed Remediation)\s*:",
        text, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _extract_flag(text: str, flag: str) -> tuple:
    pattern = rf"\s*{flag}\s*[:=]\s*(YES|NO)\b[^\n]*"
    match = re.search(pattern, text, re.IGNORECASE)
    is_yes = bool(match) and match.group(1).upper() == "YES"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return is_yes, re.sub(r"\n{3,}", "\n\n", cleaned).strip()

def _parse_issues(text: str) -> List[str]:
    marked = [m.strip() for m in re.findall(r"^\s*ISSUE:\s*(.+)$", text, re.MULTILINE)]
    if marked:
        return marked

    heading = re.search(r"^\s*(?:issues?|problems?|issues? found)\s*:\s*$", text,
                        re.MULTILINE | re.IGNORECASE)
    if not heading:
        return []
    bullets = []
    for line in text[heading.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            if bullets:
                break
            continue
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", stripped)
        if not bullet:
            break
        bullets.append(bullet.group(1).strip())
    return bullets if len(bullets) > 1 else []

def _extract_issues_structured(report: str, model) -> List[str]:
    try:
        response = model.invoke([HumanMessage(content=(
            "Read this Kubernetes diagnostic report and list the DISTINCT problems it describes - "
            "separate problems affecting different resources or with unrelated root causes. Two "
            "symptoms of the SAME underlying fault count as one problem.\n\n"
            "Reply with ONLY a JSON array of strings, nothing else. Each string names the resource, "
            "its namespace and what is wrong, and must stand alone. Use [] if nothing is wrong.\n\n"
            f"Report:\n{report}"
        ))])
        record_usage("diagnostics", response)
        raw = clean_message_content(response.content).strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        return [str(i).strip() for i in parsed if str(i).strip()] if isinstance(parsed, list) else []
    except Exception as e:
        logger.warning(f"structured issue extraction failed, continuing without a picker: {e}")
        return []

def _describe_tool_call(name: str, args: dict) -> str:
    parts = []
    for key, value in sorted(args.items()):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = " ".join(str(text).split())
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    return f"{name}({', '.join(parts)})"

def _condense_result(result) -> str:
    text = " ".join(str(result).split())
    return text if len(text) <= 300 else text[:297] + "..."

def clean_message_content(content) -> str:
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

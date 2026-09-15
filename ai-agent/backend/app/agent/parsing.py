"""
Turning raw model output into something a human can read and the graph can route on.

Every rule here exists because a real deployed run produced text the previous rule did not
handle - see the individual docstrings.
"""
import json
import logging
import re
from typing import List

from langchain_core.messages import HumanMessage, SystemMessage

from ..infra.usage_tracker import record_usage

logger = logging.getLogger(__name__)

_TAG_FLAGS = {"remediation_needed": "REMEDIATION_NEEDED", "goal_achieved": "GOAL_ACHIEVED"}


def _normalise_model_markup(text: str) -> str:
    """
    Rewrite the model's XML-ish dialect into the canonical line format the parsers expect.

    The prompt asks for 'ISSUE: ...' lines and a trailing 'REMEDIATION_NEEDED: YES'. Nova Pro
    sometimes answers in tags instead - a real deployed run produced
    '<issue> ... </issue> <remediation_needed> YES </remediation_needed>', which no parser matched:
    the tags were shown to the user verbatim AND the flag read as NO while the model had said YES,
    so the graph would have ended instead of proposing a fix.

    Normalising here rather than teaching each parser about tags keeps one place that knows the
    model's formatting varies. The final rule unwraps any other PAIRED tag the model invents while
    leaving unpaired angle brackets alone, so kubectl output like '<none>' survives intact.
    """
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
    """
    Pull an internal routing flag ('REMEDIATION_NEEDED', 'GOAL_ACHIEVED') out of a model response
    and return (is_yes, text_without_the_flag_line).

    These flags exist purely so the graph can route; they were never meant for humans, but the raw
    "REMEDIATION_NEEDED: NO" line was being shown at the bottom of every single chat message. The
    flag is now parsed into a real state field and stripped from the text the user reads - the UI
    already conveys the same thing through the proposal/approval buttons and the goal badge.
    """
    # Deliberately NOT anchored to the start of a line: the model also appends the flag to the end
    # of its last sentence ("There are 2 nodes in the cluster. REMEDIATION_NEEDED: NO"), and a
    # line-anchored pattern left that visible in the UI while reading the flag as absent.
    pattern = rf"\s*{flag}\s*[:=]\s*(YES|NO)\b[^\n]*"
    match = re.search(pattern, text, re.IGNORECASE)
    is_yes = bool(match) and match.group(1).upper() == "YES"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return is_yes, re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _parse_issues(text: str) -> List[str]:
    """
    Pull the list of distinct problems out of a diagnostic report.

    The prompt asks for lines prefixed exactly 'ISSUE: ', but the model does not always comply -
    a real deployed run wrote an "Issues:" heading followed by ordinary markdown bullets instead,
    which the strict regex matched zero of, so a two-problem report silently skipped the
    issue-selection step and bundled both fixes together. Accept the marker form first, then fall
    back to bullets under an "Issues"-style heading, so the picker doesn't depend on exact
    formatting the model may or may not produce.
    """
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
    """
    Ask the model to enumerate the distinct problems as JSON.

    Regex over the report's prose kept failing because the wording changes every run - 'ISSUE:'
    markers, then markdown bullets under an 'Issues:' heading, then 'There are two issues in the
    cluster:' followed by plain sentences. Each time the picker silently vanished and both fixes
    got bundled into one. A tiny dedicated extraction call is deterministic to parse and does not
    care how the report happens to be phrased.
    """
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
    """
    Render a tool call as one readable line for the human-facing report.

    The old format dumped the raw args dict, which for apply_kubernetes_yaml meant an entire
    escaped YAML/JSON manifest (hundreds of characters of \\n noise) inline in the chat. Long
    values are truncated to a short preview; the full detail is still in the logs.
    """
    parts = []
    for key, value in sorted(args.items()):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = " ".join(str(text).split())
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


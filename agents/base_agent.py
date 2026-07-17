"""
agents/base_agent.py

Base class for all review agents.
Each agent:
  1. Receives the parsed XppClass
  2. Calls Claude with a specialist prompt
  3. Returns a list of Issue dicts

Design notes (v2 -- tool-use instead of free-text JSON):

Earlier versions asked Claude to emit JSON as free text, then parsed it
after the fact with regex + a hand-rolled bracket matcher + a control-
character sanitizer. That entire defensive layer existed because
free-text generation gives the model no structural guarantee -- it can
emit an unescaped newline inside a string, get truncated mid-object,
etc, and none of that is knowable until you try (and fail) to parse it.

This version uses Claude's tool-use API instead: we hand the model a
JSON Schema up front (ISSUE_TOOL) and force it to call that tool via
tool_choice. The API validates the arguments against the schema before
they ever reach us -- response.content contains a tool_use block whose
.input is already a parsed Python dict, not a string we need to parse
ourselves. This doesn't just clean up the code, it removes the bug
class: there is no "raw newline broke json.loads" failure mode left,
because we never call json.loads on model output at all.

What this does NOT fix: truncation. A very long suggested_fix can still
hit max_tokens mid-generation, which can produce an incomplete tool_use
block. We still check response.stop_reason and still raise rather than
silently returning partial/empty data -- the philosophy from v1 (an
empty issue list must always mean "genuinely found nothing," never
"something went wrong and got hidden") is unchanged, only the mechanism
for detecting the problem is simpler now.
"""

import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192

# JSON Schema for a single issue. Used both to build the tool definition
# below and as the source of truth other agents can import if they need
# to reference field names/types programmatically.
ISSUE_PROPERTIES = {
    "severity": {
        "type": "string",
        "enum": ["Critical", "Major", "Minor", "Info"],
        "description": "Severity of the finding.",
    },
    "category": {
        "type": "string",
        "description": "Short category label, e.g. 'SQL Injection', 'N+1 Query'.",
    },
    "method": {
        "type": "string",
        "description": "Method name the issue occurs in, or 'Class-level' if not method-specific.",
    },
    "line_hint": {
        "type": "string",
        "description": "Approximate line number or range, e.g. '45' or '45-52'. Empty string if unknown.",
    },
    "title": {
        "type": "string",
        "description": "Short title, under 80 characters.",
    },
    "description": {
        "type": "string",
        "description": "What the issue is.",
    },
    "consequence": {
        "type": "string",
        "description": "What can go wrong if not fixed.",
    },
    "steps_to_replicate": {
        "type": "string",
        "description": "How to trigger or demonstrate the issue.",
    },
    "suggested_fix": {
        "type": "string",
        "description": "Corrected X++ code snippet or clear instructions.",
    },
}

ISSUE_REQUIRED = [
    "severity", "category", "method", "title",
    "description", "consequence", "steps_to_replicate", "suggested_fix",
]

ISSUE_TOOL = {
    "name": "report_issues",
    "description": (
        "Report the list of code review issues found in the X++ class. "
        "Call this exactly once with every issue found. If no issues were "
        "found, call it with an empty issues array -- do not skip calling it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": ISSUE_PROPERTIES,
                    "required": ISSUE_REQUIRED,
                },
            }
        },
        "required": ["issues"],
    },
}

# Kept for backwards compatibility with any agent system prompt that still
# references it for human-readable field descriptions. No longer needed
# for parsing -- the schema above is enforced by the API directly -- but
# harmless to include in a prompt for extra context on what each field means.
ISSUE_SCHEMA = """
Each issue you report should have:
- severity: Critical, Major, Minor, or Info
- category: short category label
- method: method name, or 'Class-level'
- line_hint: approximate line number or range, e.g. '45' or '45-52'
- title: short title under 80 characters
- description: what the issue is
- consequence: what can go wrong if not fixed
- steps_to_replicate: how to trigger or demonstrate the issue
- suggested_fix: corrected X++ code snippet or clear instructions

Report every issue you find by calling the report_issues tool exactly once.
"""


class AgentCallError(Exception):
    """Raised when a Claude call fails or is truncated before completion.
    Deliberately NOT swallowed -- callers (app.py) already wrap each agent
    invocation in try/except and surface the message, so raising here turns
    a silent 'no issues found' into a visible failure."""
    pass


def call_claude(system_prompt: str, user_content: str) -> list[dict]:
    """
    Calls Claude with report_issues forced via tool_choice and returns the
    parsed issues directly -- no text parsing involved.

    Raises AgentCallError on API failure or truncation before the tool
    call completed.
    """
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=0,
            system=system_prompt,
            tools=[ISSUE_TOOL],
            tool_choice={"type": "tool", "name": "report_issues"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        raise AgentCallError(f"Claude API call failed: {e}") from e

    if response.stop_reason == "max_tokens":
        raise AgentCallError(
            "Response was truncated by max_tokens before the tool call "
            "completed. Increase MAX_TOKENS or reduce the size of the "
            "class being reviewed in one call."
        )

    tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
    if not tool_use_blocks:
        raise AgentCallError(
            f"Expected a report_issues tool call but got none. "
            f"stop_reason={response.stop_reason}"
        )

    # tool_choice forces exactly one call to report_issues, so take the first.
    issues = tool_use_blocks[0].input.get("issues", [])
    return issues
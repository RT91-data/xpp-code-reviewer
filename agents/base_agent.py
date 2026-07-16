"""
agents/base_agent.py

Base class for all review agents.
Each agent:
  1. Receives the parsed XppClass
  2. Calls Claude with a specialist prompt
  3. Returns a list of Issue dicts

Design notes (why this version differs from the naive regex approach):

- JSON extraction is done with a hand-rolled bracket matcher that tracks
  whether we're inside a string literal (respecting backslash escapes).
  A plain greedy brace regex is greedy across the whole response and breaks in two
  ways: (a) if the model's X++ code snippets contain braces, matching
  is still fine since we track string state, and (b) if the response is
  truncated mid-object (no closing brace at all), the old regex simply
  fails to match and the function silently returns [] -- which looks
  identical to "no issues found". That's a dangerous silent failure,
  especially for the security agent. This version raises instead.

- Control characters (raw newline/tab/CR) that a model emits *inside*
  a JSON string value are illegal per the JSON spec and make
  json.loads() throw immediately. We only escape them when we are
  actually inside a string, so we never touch structural whitespace
  between key/value pairs.

- stop_reason is checked explicitly. If Claude stopped because it hit
  max_tokens, we know *why* JSON might be incomplete rather than
  guessing from a parse error alone.
"""

import json
import os
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"

# Bumped from 4096. Suggested_fix snippets for classes with several
# flagged methods can easily exceed the old ceiling. If you still see
# truncation on large classes, the real fix is chunking the class by
# method rather than raising this further indefinitely.
MAX_TOKENS = 8192

ISSUE_SCHEMA = """
Return a JSON object with this exact structure:
{
  "issues": [
    {
      "severity": "Critical|Major|Minor|Info",
      "category": "string",
      "method": "method name or 'Class-level'",
      "line_hint": "approximate line number or range e.g. '45' or '45-52'",
      "title": "short title (under 80 chars)",
      "description": "what the issue is",
      "consequence": "what can go wrong if not fixed",
      "steps_to_replicate": "how to trigger or demonstrate the issue",
      "suggested_fix": "corrected X++ code snippet or clear instructions"
    }
  ]
}

CRITICAL FORMATTING RULE: any code you place inside a JSON string value
(e.g. "suggested_fix") MUST have its newlines escaped as the two
characters backslash-n, NOT a literal line break. The value is a JSON
string, not a code block.

If no issues found, return: {"issues": []}
Return ONLY the JSON object. No preamble, no markdown, no explanation.
"""


class AgentCallError(Exception):
    """Raised when a Claude call fails or returns unparsable/truncated JSON.
    Deliberately NOT swallowed inside call_claude -- callers (app.py) already
    wrap each agent invocation in try/except and surface the message, so
    raising here turns a silent 'no issues found' into a visible failure."""
    pass


def _extract_json_object(text: str) -> str:
    """
    Find the first balanced {...} object in text, tracking string/escape
    state so braces inside string values (e.g. X++ code snippets) don't
    throw off the depth count.

    Returns the substring, or raises AgentCallError if no balanced object
    is found (this is the truncation case -- there's an opening brace but
    the response ran out before the matching close).
    """
    start = text.find("{")
    if start == -1:
        raise AgentCallError("No JSON object found in response (no '{' present).")

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

    # We fell off the end without depth returning to 0 -> truncated response.
    raise AgentCallError(
        "JSON object was not closed before the response ended "
        "(likely truncated by max_tokens)."
    )


def _escape_raw_control_chars_in_strings(text: str) -> str:
    """
    Walk the text and, ONLY while inside a string literal, replace raw
    control characters (newline, carriage return, tab) with their
    escaped JSON equivalents. Structural whitespace outside strings is
    left untouched.
    """
    out = []
    in_string = False
    escape = False

    for ch in text:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)

    return "".join(out)


def call_claude(system_prompt: str, user_content: str) -> list[dict]:
    """
    Calls Claude and parses the structured JSON response.

    Raises AgentCallError on any unrecoverable failure (truncation,
    unparsable JSON, API error) rather than returning [] -- an empty
    issue list must always mean "the model genuinely found nothing",
    never "something went wrong and we hid it."
    """
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        raise AgentCallError(f"Claude API call failed: {e}") from e

    # Concatenate all text blocks -- response.content can contain more
    # than one block; content[0] alone silently drops data if it doesn't.
    raw = "".join(block.text for block in response.content if block.type == "text").strip()

    if response.stop_reason == "max_tokens":
        # Don't try to be clever and salvage a partial object here --
        # surface it. A truncated Security review reporting fewer issues
        # than actually exist is worse than an agent that visibly failed.
        print(f"[agent] WARNING: response truncated (stop_reason=max_tokens). "
              f"Raw length={len(raw)} chars.")

    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw
        raw = raw.replace("json", "", 1).strip() if raw.lower().startswith("json") else raw

    try:
        json_str = _extract_json_object(raw)
    except AgentCallError:
        # Re-raise with the truncation context attached if that's what happened.
        if response.stop_reason == "max_tokens":
            raise AgentCallError(
                "Response was truncated by max_tokens before the JSON object "
                "closed. Increase MAX_TOKENS or reduce the size of the class "
                "being reviewed in one call."
            )
        raise

    # First attempt: parse as-is.
    try:
        data = json.loads(json_str)
        return data.get("issues", [])
    except json.JSONDecodeError:
        pass  # fall through to the cleaner

    # Second attempt: escape raw control chars inside string literals, then retry.
    try:
        cleaned = _escape_raw_control_chars_in_strings(json_str)
        data = json.loads(cleaned)
        return data.get("issues", [])
    except json.JSONDecodeError as e:
        # Log the actual broken text for debugging -- don't swallow it.
        print(f"[agent] JSON parse error even after cleaning: {e}")
        print(f"[agent] First 500 chars of offending JSON: {json_str[:500]}")
        raise AgentCallError(f"Could not parse JSON response even after cleanup: {e}") from e
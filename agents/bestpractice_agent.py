"""
agents/bestpractice_agent.py

Reviews X++ code for best practice violations. Split into two halves,
same reasoning as security_agent.py / performance_agent.py:

DETERMINISTIC (this file, no API call) -- pattern-matchable, zero judgment:
  - Empty catch blocks (the original prompt explicitly called this "must
    always be flagged" -- exactly the kind of rule that should never
    depend on sampling)
  - Missing super() on known framework lifecycle overrides (init, run,
    validate, modifiedField, modifiedFieldValue, close, new)
  - Unbalanced ttsbegin/ttscommit counts within a method
  - Write operations (insert/update/delete) with no ttsbegin anywhere
    in the method
  - Hardcoded UI strings passed to error()/warning()/info()/Box::*()
    instead of a label reference
  - God method (over 150 lines, per the original prompt's own threshold)

LLM (Claude) -- needs actual judgment:
  - Magic numbers/strings (requires knowing which literals are meaningful
    vs. genuinely need a named constant)
  - Deprecated API usage (requires framework version knowledge that's
    risky to hardcode confidently as a regex without false claims)
  - Missing validation return-check (requires understanding whether the
    return value was meant to gate control flow)
  - Improper use of global/class-level variables as output params
    (requires design judgment)
  - Swallowed-exception nuance beyond "empty" -- e.g. catch(retry-loop)
    with no logging: is the retry itself a legitimate substitute for
    logging, or a silent failure? That's a judgment call.
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass, XppMethod, _extract_balanced_braces
from agents.base_agent import call_claude, ISSUE_SCHEMA

WRITE_CALL_PATTERN = re.compile(r'\.(insert|update|delete|doInsert|doUpdate|doDelete)\s*\(')
CATCH_PATTERN = re.compile(r'\bcatch\b\s*(?:\([^)]*\))?\s*\{', re.IGNORECASE)
TTSBEGIN_PATTERN = re.compile(r'\bttsbegin\b', re.IGNORECASE)
TTSCOMMIT_PATTERN = re.compile(r'\bttscommit\b', re.IGNORECASE)
LOGGING_MARKERS = ("error(", "warning(", "throw ", "throw(", "retry", "continue",
                    "ttsabort", ".log(", "logger")
KNOWN_OVERRIDE_METHODS = {"init", "run", "validate", "modifiedField",
                           "modifiedFieldValue", "close", "new"}
HARDCODED_STRING_PATTERN = re.compile(
    r'(?<!::)\b(error|warning|info)\s*\(\s*"([^"@][^"]*)"',
    re.IGNORECASE,
)
BOX_HARDCODED_PATTERN = re.compile(
    r'\bBox::(info|warning|error|okCancel|yesNo)\s*\(\s*"([^"@][^"]*)"',
    re.IGNORECASE,
)
GOD_METHOD_LINE_THRESHOLD = 150


def _make_issue(severity, category, method, line_hint, title, description,
                 consequence, steps_to_replicate, suggested_fix) -> dict:
    return {
        "severity": severity,
        "category": category,
        "method": method,
        "line_hint": str(line_hint) if line_hint else "",
        "title": title,
        "description": description,
        "consequence": consequence,
        "steps_to_replicate": steps_to_replicate,
        "suggested_fix": suggested_fix,
    }


def _find_catch_bodies(body: str) -> list[str]:
    """Return the text inside every catch { ... } block in body, using the
    same balanced-brace matcher xpp_parser uses elsewhere."""
    bodies = []
    for m in CATCH_PATTERN.finditer(body):
        brace_idx = m.end() - 1
        catch_body, _ = _extract_balanced_braces(body, brace_idx)
        bodies.append(catch_body)
    return bodies


def _strip_comments(text: str) -> str:
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return text


def _check_catch_blocks(method: XppMethod) -> list[dict]:
    issues = []
    for catch_body in _find_catch_bodies(method.body):
        stripped = _strip_comments(catch_body).strip().rstrip(";").strip()
        if not stripped:
            issues.append(_make_issue(
                severity="Critical",
                category="Empty Catch Block",
                method=method.name,
                line_hint=method.start_line,
                title=f"Empty catch block in '{method.name}'",
                description=f"'{method.name}' has a catch block with no body "
                             f"(or only comments) -- the exception is silently swallowed.",
                consequence="Real errors disappear in production with no trace, "
                            "making failures extremely hard to diagnose.",
                steps_to_replicate=f"Inspect the catch block in '{method.name}'.",
                suggested_fix="At minimum, log the exception with error() or "
                              "warning(), or rethrow it -- never leave catch empty.",
            ))
        elif not any(marker in catch_body.lower() for marker in LOGGING_MARKERS):
            issues.append(_make_issue(
                severity="Major",
                category="Swallowed Exception",
                method=method.name,
                line_hint=method.start_line,
                title=f"catch block in '{method.name}' has no logging or rethrow",
                description=(
                    f"'{method.name}' has a non-empty catch block, but it doesn't "
                    f"appear to call error()/warning(), rethrow, or retry -- the "
                    f"exception may be silently absorbed. (Heuristic check based "
                    f"on common logging call names -- verify manually if this "
                    f"looks like a false positive.)"
                ),
                consequence="An exception can be caught and effectively discarded, "
                            "hiding failures from logs and monitoring.",
                steps_to_replicate=f"Inspect the catch block body in '{method.name}'.",
                suggested_fix="Add error()/warning() logging, or rethrow if this "
                              "layer isn't the right place to handle it.",
            ))
    return issues


def _check_missing_super(method: XppMethod, xpp: XppClass) -> list[dict]:
    if not xpp.extends:
        return []
    if method.name not in KNOWN_OVERRIDE_METHODS:
        return []
    if "super(" in method.body:
        return []
    return [_make_issue(
        severity="Major",
        category="Missing super() Call",
        method=method.name,
        line_hint=method.start_line,
        title=f"'{method.name}' overrides a framework method without calling super()",
        description=(
            f"'{method.name}' is a known framework lifecycle method and "
            f"'{xpp.name}' extends '{xpp.extends}', but no super() call was "
            f"found in the body."
        ),
        consequence="Skipping super() can silently break base class/framework "
                    "behavior that other code depends on.",
        steps_to_replicate=f"Inspect '{method.name}' for a super() call.",
        suggested_fix=f"Add super() in '{method.name}' (typically as the first "
                      f"statement, though some overrides call it last -- check "
                      f"the base implementation's expectations).",
    )]


def _check_ttsbegin_ttscommit_balance(method: XppMethod) -> list[dict]:
    begins = len(TTSBEGIN_PATTERN.findall(method.body))
    commits = len(TTSCOMMIT_PATTERN.findall(method.body))
    if begins == 0 and commits == 0:
        return []
    if begins == commits:
        return []
    return [_make_issue(
        severity="Critical",
        category="Unbalanced Transaction",
        method=method.name,
        line_hint=method.start_line,
        title=f"Unbalanced ttsbegin/ttscommit in '{method.name}' ({begins} vs {commits})",
        description=f"'{method.name}' has {begins} ttsbegin call(s) but "
                     f"{commits} ttscommit call(s).",
        consequence="Mismatched transaction boundaries can leave a transaction "
                    "open (holding locks) or attempt to commit a transaction "
                    "that was never started, both of which cause runtime errors "
                    "or blocking in production.",
        steps_to_replicate=f"Count ttsbegin/ttscommit occurrences in '{method.name}'.",
        suggested_fix="Ensure every ttsbegin has exactly one matching ttscommit "
                      "(or ttsabort in the failure path), including all branches.",
    )]


def _check_write_without_ttsbegin(method: XppMethod) -> list[dict]:
    if not WRITE_CALL_PATTERN.search(method.body):
        return []
    if TTSBEGIN_PATTERN.search(method.body):
        return []
    return [_make_issue(
        severity="Critical",
        category="Missing Transaction Scope",
        method=method.name,
        line_hint=method.start_line,
        title=f"Write operation in '{method.name}' with no ttsbegin in this method",
        description=(
            f"'{method.name}' calls insert/update/delete but no ttsbegin "
            f"appears anywhere in the method body. (If the transaction is "
            f"opened by a caller of this method, this may be a false "
            f"positive -- verify the calling context.)"
        ),
        consequence="Writes outside a transaction scope can leave partial/"
                    "inconsistent data if a later step in the same logical "
                    "operation fails.",
        steps_to_replicate=f"Inspect '{method.name}' for insert/update/delete "
                            f"calls and confirm whether ttsbegin wraps them "
                            f"(here or in the caller).",
        suggested_fix="Wrap the write in ttsbegin/ttscommit (with ttsabort on "
                      "the failure path), unless intentionally relying on a "
                      "caller-managed transaction.",
    )]


def _check_hardcoded_strings(method: XppMethod) -> list[dict]:
    issues = []
    seen = set()
    for pattern, prefix in ((HARDCODED_STRING_PATTERN, ""), (BOX_HARDCODED_PATTERN, "Box::")):
        for m in pattern.finditer(method.body):
            call_name, literal = m.group(1), m.group(2)
            display_name = f"{prefix}{call_name}"
            key = (display_name.lower(), literal)
            if key in seen or not literal.strip():
                continue
            seen.add(key)
            issues.append(_make_issue(
                severity="Major",
                category="Hardcoded UI String",
                method=method.name,
                line_hint=method.start_line,
                title=f"Hardcoded string passed to {display_name}(): \"{literal[:50]}\"",
                description=f"'{method.name}' passes a literal string to "
                             f"{display_name}() instead of a label reference.",
                consequence="Untranslatable and unmaintainable -- any UI text "
                            "change requires a code deployment instead of a "
                            "label update, and it won't localize for other languages.",
                steps_to_replicate=f"Inspect the {display_name}() call in '{method.name}'.",
                suggested_fix=f'Replace the literal with a label reference, e.g. '
                              f'{display_name}("@MyModule:MyLabelId").',
            ))
    return issues


def _check_god_method(method: XppMethod) -> list[dict]:
    line_count = method.end_line - method.start_line
    if line_count <= GOD_METHOD_LINE_THRESHOLD:
        return []
    return [_make_issue(
        severity="Minor",
        category="God Method",
        method=method.name,
        line_hint=f"{method.start_line}-{method.end_line}",
        title=f"'{method.name}' is {line_count} lines long",
        description=f"'{method.name}' spans {line_count} lines, over the "
                     f"{GOD_METHOD_LINE_THRESHOLD}-line threshold.",
        consequence="Long methods tend to accumulate multiple responsibilities, "
                    "making them harder to test, review, and safely modify.",
        steps_to_replicate=f"Method spans lines {method.start_line}-{method.end_line}.",
        suggested_fix="Consider decomposing into smaller private helper methods "
                      "with clear single responsibilities.",
    )]


def _run_deterministic(xpp: XppClass) -> list[dict]:
    issues = []
    for method in xpp.methods:
        issues.extend(_check_catch_blocks(method))
        issues.extend(_check_missing_super(method, xpp))
        issues.extend(_check_ttsbegin_ttscommit_balance(method))
        issues.extend(_check_write_without_ttsbegin(method))
        issues.extend(_check_hardcoded_strings(method))
        issues.extend(_check_god_method(method))
    return issues


# ── LLM PROMPT: trimmed to only the judgment-call checks ──────────────
SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO technical lead
with deep expertise in X++ best practices, code quality, and maintainability.

Your task: perform a BEST PRACTICES review of the provided X++ class, focused
ONLY on the checks below. A separate deterministic static analysis pass already
covers empty catch blocks, missing super() on framework lifecycle methods,
unbalanced ttsbegin/ttscommit, writes with no ttsbegin, hardcoded UI strings,
and methods over 150 lines -- do NOT re-report those; focus only on what
genuinely requires judgment:

1. MAGIC NUMBERS/STRINGS (judgment required)
   - Numeric or string literals that are genuinely unclear in intent and
     would benefit from a named constant. Don't flag obviously self-evident
     literals (e.g. array index 0, multiplying by 100 for a percentage).

2. DEPRECATED API USAGE (judgment required)
   - Old NumberSeq API patterns, deprecated DimensionAttributeValueSetStorage
     usage, old SecurityPolicy patterns -- requires recognizing the specific
     deprecated call shape, not just a keyword match.

3. MISSING VALIDATION RETURN CHECK (judgment required)
   - A call to validate() or checkMandatory() whose return value is
     genuinely ignored where it should have gated further logic (not
     every such call needs its result checked -- use judgment).

4. IMPROPER USE OF GLOBAL/CLASS-LEVEL VARIABLES (judgment required)
   - Class-level variables used as implicit method output parameters
     where an explicit return value or out-parameter would be clearer.

5. SWALLOWED EXCEPTION NUANCE (judgment required)
   - A catch block that retries or continues without ANY logging: is the
     retry/continue itself a legitimate resilience pattern here, or does
     it silently mask a real failure? This needs reading the surrounding
     logic, not just detecting the absence of a log call.

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run best practices review: deterministic checks + LLM judgment-call checks."""

    deterministic_issues = _run_deterministic(xpp)

    user_content = f"""Review this X++ class for the judgment-call best practice
issues described in your instructions (magic numbers/strings, deprecated API
usage, missing validation return-check, improper use of global/class-level
variables, and swallowed-exception nuance beyond simple emptiness). Do not
re-report empty catch blocks, missing super(), unbalanced ttsbegin/ttscommit,
writes with no ttsbegin, hardcoded UI strings, or methods over 150 lines --
those are already covered by static analysis.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Total lines: {xpp.total_lines}
Methods: {len(xpp.methods)}

Full source code:
```xpp
{xpp.raw_code}
```"""

    llm_issues = call_claude(SYSTEM_PROMPT, user_content)

    all_issues = deterministic_issues + llm_issues
    for issue in all_issues:
        issue["agent"] = "Best Practice"

    return all_issues
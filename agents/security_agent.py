"""
agents/security_agent.py

Reviews X++ code for security vulnerabilities. Split into two halves:

DETERMINISTIC (this file, no API call) -- pattern-matchable, zero judgment:
  - Missing [SysEntryPoint] on public methods
  - SQL injection via string concatenation into executeQuery/executeUpdate
  - Missing checkWrite() before insert/update/delete
  - Hardcoded credentials in string literals

LLM (Claude, via base_agent.call_claude) -- needs actual judgment:
  - Cross-company guard correctness (is changeCompany() used appropriately here?)
  - Unvalidated input (does this data actually flow from an untrusted source?)
  - Privilege escalation intent (is this RunAs/setConnection() actually unauthorized?)

Why split this way: the four deterministic checks above were the ones
observed to be perfectly stable across repeated runs even before this
split (same finding, reworded each time) -- they're structural facts
about the code, not interpretation. The three LLM checks require
understanding intent, which is genuinely ambiguous and will keep
producing some run-to-run variance -- that's expected, not a bug, and
is a separate problem from the reproducibility issue this split fixes.
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass, XppMethod, _extract_balanced_braces
from agents.base_agent import call_claude, ISSUE_SCHEMA

WRITE_CALL_PATTERN = re.compile(r'\.(insert|update|delete|doInsert|doUpdate|doDelete)\s*\(')
CREDENTIAL_NAME_PATTERN = re.compile(
    r'\b(password|pwd|secret|apikey|api_key|connectionstring|conn_string)\s*=\s*'
    r'["\']([^"\']{3,})["\']',
    re.IGNORECASE,
)
SQL_CONCAT_PATTERN = re.compile(
    r'execute(?:Query|Update)\s*\([^)]*\+[^)]*\)',
    re.IGNORECASE,
)


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


def _check_missing_sysentrypoint(method: XppMethod) -> list[dict]:
    is_public = any(mod.lower() == "public" for mod in method.modifiers)
    is_private = any(mod.lower() == "private" for mod in method.modifiers)
    if not is_public or is_private:
        return []
    has_attr = any("sysentrypoint" in a.lower() for a in method.attributes)
    if has_attr:
        return []

    does_write = bool(WRITE_CALL_PATTERN.search(method.body))
    severity = "Critical" if does_write else "Major"

    return [_make_issue(
        severity=severity,
        category="Missing SysEntryPoint",
        method=method.name,
        line_hint=method.start_line,
        title=f"Public method '{method.name}' missing [SysEntryPoint]",
        description=(
            f"'{method.name}' is public but has no [SysEntryPoint, true] attribute. "
            f"Public methods callable from services/external clients need this "
            f"attribute or they're exposed to unauthenticated callers."
        ),
        consequence=(
            "An external caller can invoke this method without going through "
            "the expected entry-point authentication/authorization gate."
            + (" This method also performs a data write, raising the stakes further."
               if does_write else "")
        ),
        steps_to_replicate=f"Inspect method signature: public {method.return_type} "
                            f"{method.name}({method.parameters})",
        suggested_fix=f"Add [SysEntryPoint(true)] above the method signature "
                       f"(or [SysEntryPoint(false)] if it should not be externally callable).",
    )]


def _check_sql_injection(method: XppMethod) -> list[dict]:
    issues = []
    for m in SQL_CONCAT_PATTERN.finditer(method.body):
        issues.append(_make_issue(
            severity="Critical",
            category="SQL Injection",
            method=method.name,
            line_hint=method.start_line,
            title="Query built with string concatenation",
            description=(
                f"Method '{method.name}' passes a concatenated string into "
                f"executeQuery/executeUpdate: {m.group(0)[:120]}"
            ),
            consequence=(
                "If any part of the concatenated string originates from user "
                "input, this is directly exploitable for SQL injection -- "
                "arbitrary query manipulation, data exfiltration, or data loss."
            ),
            steps_to_replicate=f"Inspect the query construction in '{method.name}' "
                                f"around: {m.group(0)[:120]}",
            suggested_fix="Use parameterized queries (SqlStatementExecutePermission "
                          "with bound parameters) instead of string concatenation.",
        ))
    return issues


def _check_missing_checkwrite(method: XppMethod) -> list[dict]:
    if not WRITE_CALL_PATTERN.search(method.body):
        return []
    if "checkWrite(" in method.body or "checkwrite(" in method.body.lower():
        return []
    return [_make_issue(
        severity="Major",
        category="Missing Access Check",
        method=method.name,
        line_hint=method.start_line,
        title=f"'{method.name}' writes data without a checkWrite() call",
        description=(
            f"'{method.name}' calls insert/update/delete but no checkWrite() "
            f"call appears in the method body."
        ),
        consequence=(
            "Bypasses D365 FnO's table-level security framework -- a user "
            "without write permission on this table could still trigger the write."
        ),
        steps_to_replicate=f"Inspect '{method.name}' for insert/update/delete calls "
                            f"and confirm no checkWrite() precedes them.",
        suggested_fix="Call this.checkWrite() (or the relevant buffer's checkWrite()) "
                      "before performing the write.",
    )]


def _check_hardcoded_credentials(xpp: XppClass) -> list[dict]:
    issues = []
    seen = set()
    for method in xpp.methods:
        for m in CREDENTIAL_NAME_PATTERN.finditer(method.body):
            var_name, value = m.group(1), m.group(2)
            key = (method.name, var_name, value)
            if key in seen:
                continue
            seen.add(key)
            issues.append(_make_issue(
                severity="Critical",
                category="Hardcoded Credentials",
                method=method.name,
                line_hint=method.start_line,
                title=f"Hardcoded credential-like value assigned to '{var_name}'",
                description=(
                    f"'{var_name}' is assigned a literal string value in "
                    f"'{method.name}' -- looks like a hardcoded credential."
                ),
                consequence=(
                    "Hardcoded credentials committed to source control are a "
                    "direct compromise if the repo is ever exposed, and can't "
                    "be rotated without a code deployment."
                ),
                steps_to_replicate=f"Inspect '{method.name}' for the literal "
                                    f"assignment to '{var_name}'.",
                suggested_fix=f"Move '{var_name}' to Azure Key Vault or a secure "
                              f"configuration store; never hardcode secrets in X++.",
            ))
    return issues


def _run_deterministic(xpp: XppClass) -> list[dict]:
    issues = []
    for method in xpp.methods:
        issues.extend(_check_missing_sysentrypoint(method))
        issues.extend(_check_sql_injection(method))
        issues.extend(_check_missing_checkwrite(method))
    issues.extend(_check_hardcoded_credentials(xpp))
    return issues


# ── LLM PROMPT: trimmed to only the judgment-call checks ──────────────
SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO / AX security architect
with deep expertise in X++ security patterns and D365 FnO security framework.

Your task: perform a SECURITY review of the provided X++ class, focused ONLY on
the checks below. A separate deterministic static analysis pass already covers
missing [SysEntryPoint], SQL injection via string concatenation, missing
checkWrite() before writes, and hardcoded credentials -- do NOT re-report those;
focus only on what genuinely requires judgment:

1. CROSS-COMPANY GUARD (judgment required)
   - Is changeCompany() used appropriately here, given what this method does?
   - Are company range filters missing on shared tables in a way that's actually
     risky for this specific business logic (not just "no crossCompany keyword")?

2. UNVALIDATED INPUT (judgment required)
   - Does data actually appear to flow from an untrusted/external source into
     a query or file operation without validation? Trace the actual data flow --
     don't flag every parameter, only ones plausibly external-facing.

3. PRIVILEGE ESCALATION (judgment required)
   - RunAs patterns or setConnection() calls: is there evidence the surrounding
     code lacks proper authorization, or is this a legitimate, guarded use?
   - Direct DictTable/DictField manipulation: does it appear to bypass
     field-level security in a way that matters here?

Be specific. Reference exact method names and patterns from the code.
For each issue, provide a concrete X++ code fix.

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run security review: deterministic checks + LLM judgment-call checks."""

    deterministic_issues = _run_deterministic(xpp)

    user_content = f"""Review this X++ class for the judgment-call security issues
described in your instructions (cross-company guard correctness, unvalidated
input data-flow, privilege escalation intent). Do not re-report missing
SysEntryPoint, SQL string concatenation, missing checkWrite, or hardcoded
credentials -- those are already covered by static analysis.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Class Attributes: {', '.join(xpp.attributes) or 'None'}

Full source code:
```xpp
{xpp.raw_code}
```"""

    llm_issues = call_claude(SYSTEM_PROMPT, user_content)

    all_issues = deterministic_issues + llm_issues
    for issue in all_issues:
        issue["agent"] = "Security"

    return all_issues
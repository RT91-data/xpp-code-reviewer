"""
agents/performance_agent.py

Reviews X++ code for performance anti-patterns. Split into two halves,
same reasoning as security_agent.py:

DETERMINISTIC (this file, no API call) -- pattern-matchable, zero judgment:
  - SELECT inside a loop (the #1 D365 performance killer -- always Critical)
  - Unbounded select (no WHERE) on a known-large table
  - select forupdate with no subsequent write call in the method
  - Multiple .insert() calls inside a loop without RecordInsertList

LLM (Claude) -- needs actual judgment:
  - Missing firstOnly (requires knowing whether one record was actually intended)
  - Existence-check anti-pattern (requires understanding intent: was this
    select meant to check existence, or genuinely needs the data?)
  - Missing noFetch (requires knowing whether joined fields are used downstream)
  - Cross-company performance (requires judging whether queries inside
    changeCompany are actually "heavy")
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass, XppMethod, _extract_balanced_braces
from agents.base_agent import call_claude, ISSUE_SCHEMA

LARGE_TABLES = {"CustTrans", "VendTrans", "InventTrans", "LedgerTrans"}
WRITE_CALL_PATTERN = re.compile(r'\.(insert|update|delete|doInsert|doUpdate|doDelete)\s*\(')
LOOP_START_PATTERN = re.compile(r'\b(while|for)\s*\(')
# X++'s idiomatic loop-over-query-results construct: "while select ... { }" --
# no parentheses, the loop body starts at the next '{' after "while select".
# This is the single most common real-world case for select-inside-loop
# (nesting a second select inside a while-select), so missing it would
# defeat the point of this check.
WHILE_SELECT_PATTERN = re.compile(r'\bwhile\s+select\b', re.IGNORECASE)
SELECT_STATEMENT_PATTERN = re.compile(r'\bselect\b.*?;', re.IGNORECASE | re.DOTALL)


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


def _find_loop_bodies(body: str) -> list[str]:
    """Return the text inside every top-level loop in body -- both C-style
    while(...)/for(...) and X++'s while-select construct -- using the same
    balanced-brace matcher xpp_parser uses for class/method bodies. Nested
    loops: outer loop_body naturally includes inner loop text too, which is
    fine here since we only test 'does select/insert appear anywhere inside'."""
    loop_bodies = []

    for m in LOOP_START_PATTERN.finditer(body):
        paren_start = m.end() - 1
        depth = 0
        j = paren_start
        while j < len(body):
            if body[j] == "(":
                depth += 1
            elif body[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        brace_idx = body.find("{", j + 1)
        if brace_idx == -1:
            continue
        loop_body, _ = _extract_balanced_braces(body, brace_idx)
        loop_bodies.append(loop_body)

    for m in WHILE_SELECT_PATTERN.finditer(body):
        # No parens to skip -- the where-clause (if any) runs directly up to
        # the opening brace of the loop body.
        brace_idx = body.find("{", m.end())
        if brace_idx == -1:
            continue
        loop_body, _ = _extract_balanced_braces(body, brace_idx)
        loop_bodies.append(loop_body)

    return loop_bodies


def _check_select_in_loop(method: XppMethod) -> list[dict]:
    issues = []
    for loop_body in _find_loop_bodies(method.body):
        if re.search(r'\bselect\b', loop_body, re.IGNORECASE):
            issues.append(_make_issue(
                severity="Critical",
                category="N+1 Query",
                method=method.name,
                line_hint=method.start_line,
                title=f"select inside a loop in '{method.name}'",
                description=(
                    f"'{method.name}' contains a select statement inside a "
                    f"while/for loop -- one DB round trip per iteration."
                ),
                consequence=(
                    "N+1 query pattern: on a large record set this causes "
                    "excessive AOS-to-SQL round trips, blocking, and potential "
                    "timeouts under production load."
                ),
                steps_to_replicate=f"Inspect the loop body in '{method.name}' "
                                    f"for a select statement.",
                suggested_fix="Collect IDs first, then use a single select with "
                              "a join or exists join instead of selecting per iteration.",
            ))
            break  # one finding per method is enough signal; avoid spamming duplicates
    return issues


def _check_unbounded_select_large_table(method: XppMethod) -> list[dict]:
    issues = []
    for m in SELECT_STATEMENT_PATTERN.finditer(method.body):
        stmt = m.group(0)
        if "where" in stmt.lower():
            continue
        matched_table = next((t for t in LARGE_TABLES if t.lower() in stmt.lower()), None)
        if not matched_table:
            continue
        issues.append(_make_issue(
            severity="Major",
            category="Unbounded Select",
            method=method.name,
            line_hint=method.start_line,
            title=f"Unbounded select on {matched_table} (no WHERE clause)",
            description=(
                f"'{method.name}' selects from {matched_table} with no WHERE "
                f"clause: {stmt[:120]}"
            ),
            consequence=(
                f"{matched_table} is a high-volume transaction table -- an "
                f"unbounded select can return millions of rows and cause AOS "
                f"memory pressure or timeouts."
            ),
            steps_to_replicate=f"Inspect the select statement in '{method.name}': {stmt[:120]}",
            suggested_fix=f"Add a WHERE clause filtering {matched_table} by a "
                          f"selective key (account, date range, etc.) before this runs in production.",
        ))
    return issues


def _check_forupdate_without_write(method: XppMethod) -> list[dict]:
    if not re.search(r'select\s+forupdate', method.body, re.IGNORECASE):
        return []
    if WRITE_CALL_PATTERN.search(method.body):
        return []
    return [_make_issue(
        severity="Minor",
        category="Unnecessary Row Lock",
        method=method.name,
        line_hint=method.start_line,
        title=f"select forupdate with no write detected in '{method.name}'",
        description=(
            f"'{method.name}' uses select forupdate, but no insert/update/delete "
            f"call was found in the method body. (Heuristic check -- if the write "
            f"happens in a method called from here, this may be a false positive; "
            f"verify manually.)"
        ),
        consequence="forupdate takes a row-level lock even when the record is "
                    "only read, unnecessarily increasing blocking risk.",
        steps_to_replicate=f"Inspect '{method.name}' for select forupdate usage.",
        suggested_fix="Remove forupdate if the record is only being read, or "
                      "confirm the write actually happens elsewhere.",
    )]


def _check_missing_bulk_insert(method: XppMethod) -> list[dict]:
    issues = []
    for loop_body in _find_loop_bodies(method.body):
        if re.search(r'\.insert\s*\(', loop_body) and "RecordInsertList" not in method.body:
            issues.append(_make_issue(
                severity="Major",
                category="Missing Bulk Insert",
                method=method.name,
                line_hint=method.start_line,
                title=f"Individual .insert() calls in a loop in '{method.name}'",
                description=(
                    f"'{method.name}' calls .insert() inside a loop without using "
                    f"RecordInsertList anywhere in the method."
                ),
                consequence="Row-by-row inserts are significantly slower than a "
                            "bulk insert (10x+ in typical D365 workloads).",
                steps_to_replicate=f"Inspect the loop body in '{method.name}' for .insert() calls.",
                suggested_fix="Use RecordInsertList to batch inserts instead of "
                              "calling .insert() once per loop iteration.",
            ))
            break
    return issues


def _run_deterministic(xpp: XppClass) -> list[dict]:
    issues = []
    for method in xpp.methods:
        issues.extend(_check_select_in_loop(method))
        issues.extend(_check_unbounded_select_large_table(method))
        issues.extend(_check_forupdate_without_write(method))
        issues.extend(_check_missing_bulk_insert(method))
    return issues


# ── LLM PROMPT: trimmed to only the judgment-call checks ──────────────
SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO performance engineer
with deep expertise in X++ query optimisation and AOS performance patterns.

Your task: perform a PERFORMANCE review of the provided X++ class, focused ONLY on
the checks below. A separate deterministic static analysis pass already covers
select-inside-loop, unbounded selects on known large tables, forupdate without a
write, and missing bulk insert -- do NOT re-report those; focus only on what
genuinely requires judgment:

1. MISSING firstOnly (judgment required)
   - Flag a select without firstOnly ONLY where the code's intent is clearly
     to fetch a single record (e.g. looking up by a unique key). Do not flag
     selects that are deliberately iterating multiple records (while select).

2. EXISTENCE CHECK ANTI-PATTERN (judgment required)
   - select count(*) or fetching a full record only to check if it exists.
   - Requires reading the surrounding code to confirm the result is only used
     as a boolean, not for its data.

3. MISSING noFetch (judgment required)
   - Joined tables whose fields are never referenced afterward in the method.
   - Requires tracing whether the joined table's fields are actually used.

4. CROSS-COMPANY PERFORMANCE (judgment required)
   - changeCompany() blocks containing queries that are genuinely heavy for
     this specific business logic (not just "changeCompany is present").

For each issue: name the method, describe the exact pattern, explain the
D365-specific consequence (blocking, timeouts, AOS crash), and provide fixed
X++ code.

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run performance review: deterministic checks + LLM judgment-call checks."""

    deterministic_issues = _run_deterministic(xpp)

    user_content = f"""Review this X++ class for the judgment-call performance issues
described in your instructions (missing firstOnly, existence-check anti-pattern,
missing noFetch, cross-company performance). Do not re-report select-inside-loop,
unbounded selects on large tables, forupdate without a write, or missing bulk
insert -- those are already covered by static analysis.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Total lines: {xpp.total_lines}

Methods:
{', '.join(m.name for m in xpp.methods)}

Full source code:
```xpp
{xpp.raw_code}
```"""

    llm_issues = call_claude(SYSTEM_PROMPT, user_content)

    all_issues = deterministic_issues + llm_issues
    for issue in all_issues:
        issue["agent"] = "Performance"

    return all_issues
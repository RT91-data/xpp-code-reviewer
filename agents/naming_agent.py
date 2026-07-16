"""
agents/naming_agent.py

Deterministic (non-LLM) naming convention checks for X++ classes.

Why this is no longer an LLM call:
Naming convention violations (parameter prefixes, method casing, etc.)
are syntactic pattern matches against xpp_parser's already-extracted
structure (XppMethod.parameters, XppClass.fields, ...) -- there is no
judgment call being made, so asking an LLM to "find" them introduced
run-to-run sampling variance on findings that should be 100%
reproducible. This module trades some scope (a few checks noted at the
bottom need semantic info the current parser doesn't extract) for a
hard guarantee: same input -> same output, every time, zero API cost.

Interface is unchanged from the LLM version: run(xpp) -> list[dict],
same issue schema, "agent" key set to "Naming" -- app.py and the
ThreadPoolExecutor in the review pipeline need no changes.
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass, XppMethod

BOOLEAN_TYPES = {"boolean"}
BOOLEAN_PREFIXES = ("is", "has", "can", "should")
CONTROL_KEYWORDS = {"if", "while", "for", "switch", "try", "catch", "finally"}


def _split_params(parameters: str) -> list[str]:
    """Split a parameter list string on top-level commas. X++ signatures
    don't nest parens/brackets inside param lists in practice, so a
    straight split is sufficient here."""
    if not parameters.strip():
        return []
    return [p.strip() for p in parameters.split(",") if p.strip()]


def _param_name(param: str) -> str:
    """Given a single parameter declaration like 'CustAccount _custAccount'
    or 'CustAccount _custAccount = ""', return just the variable name."""
    param = param.split("=")[0].strip()          # drop default value
    param = param.rstrip("[]").strip()           # drop array brackets
    tokens = param.split()
    return tokens[-1] if tokens else ""


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


def _check_parameter_prefix(method: XppMethod) -> list[dict]:
    issues = []
    for param in _split_params(method.parameters):
        name = _param_name(param)
        if not name or name.startswith("_"):
            continue
        issues.append(_make_issue(
            severity="Major",
            category="Naming Convention",
            method=method.name,
            line_hint=method.start_line,
            title=f"Parameter '{name}' missing underscore prefix",
            description=(
                f"Parameter '{name}' in method '{method.name}' does not follow "
                f"the D365 FnO convention requiring all method parameters to be "
                f"prefixed with an underscore."
            ),
            consequence=(
                "Inconsistent parameter naming makes it harder to distinguish "
                "parameters from local variables at a glance, and is a standard "
                "item on Microsoft's own code review checklist."
            ),
            steps_to_replicate=f"Inspect method '{method.name}' parameter list: "
                                f"({method.parameters})",
            suggested_fix=f"Rename parameter '{name}' to '_{name}' (and update "
                           f"all references within the method body).",
        ))
    return issues


def _check_method_casing(method: XppMethod) -> list[dict]:
    name = method.name
    if not name or not name[0].isupper():
        return []
    return [_make_issue(
        severity="Minor",
        category="Naming Convention",
        method=method.name,
        line_hint=method.start_line,
        title=f"Method '{name}' should be camelCase",
        description=(
            f"Method '{name}' starts with an uppercase letter (PascalCase). "
            f"D365 FnO convention is camelCase for method names."
        ),
        consequence=(
            "Inconsistent with the rest of the AOT and Microsoft's own base "
            "classes; can visually confuse method names with class names."
        ),
        steps_to_replicate=f"Inspect the method signature: {name}(...)",
        suggested_fix=f"Rename '{name}' to '{name[0].lower()}{name[1:]}'.",
    )]


def _check_boolean_fields(xpp: XppClass) -> list[dict]:
    issues = []
    field_pattern = re.compile(
        r'^\s*(?:(?:public|private|protected|static|server|client)\s+)*'
        r'(\w+(?:\s+\w+)?)\s+(\w+)\s*(?:=.*)?;?\s*$'
    )
    for field_line in xpp.fields:
        m = field_pattern.match(field_line)
        if not m:
            continue
        field_type = m.group(1).strip().lower()
        field_name = m.group(2).strip()
        if field_type in BOOLEAN_TYPES and not field_name.lower().startswith(BOOLEAN_PREFIXES):
            issues.append(_make_issue(
                severity="Minor",
                category="Naming Convention",
                method="Class-level",
                line_hint="",
                title=f"Boolean field '{field_name}' should start with is/has/can/should",
                description=f"Field '{field_name}' is declared as boolean but its "
                             f"name doesn't signal a boolean value.",
                consequence="Readers can't tell from the name alone whether this is "
                             "a boolean flag, slowing review and inviting misuse in conditionals.",
                steps_to_replicate=f"Inspect class-level field declaration: {field_line}",
                suggested_fix=f"Rename '{field_name}' to something like "
                               f"'is{field_name[0].upper()}{field_name[1:]}'.",
            ))
    return issues


def _check_class_naming(xpp: XppClass) -> list[dict]:
    if xpp.class_type == "class" and xpp.name and xpp.name[0].islower():
        return [_make_issue(
            severity="Minor",
            category="Naming Convention",
            method="Class-level",
            line_hint="",
            title=f"Class '{xpp.name}' should be PascalCase",
            description=f"Class '{xpp.name}' starts with a lowercase letter.",
            consequence="Deviates from D365 FnO / .NET class naming conventions "
                        "used throughout the AOT.",
            steps_to_replicate=f"Inspect class declaration: class {xpp.name}",
            suggested_fix=f"Rename to '{xpp.name[0].upper()}{xpp.name[1:]}'.",
        )]
    return []


def _check_extension_suffix(xpp: XppClass) -> list[dict]:
    is_extension = any("extensionof" in attr.lower().replace(" ", "") for attr in xpp.attributes)
    if is_extension and not xpp.name.endswith("_Extension"):
        return [_make_issue(
            severity="Major",
            category="Naming Convention",
            method="Class-level",
            line_hint="",
            title=f"Extension class '{xpp.name}' should end with '_Extension'",
            description=f"Class '{xpp.name}' carries an [ExtensionOf(...)] attribute "
                        f"but its name doesn't follow the '_Extension' suffix "
                        f"convention required for chain-of-command extension classes.",
            consequence="Inconsistent extension naming makes extension classes harder "
                        "to locate/audit across the model, and can trip up tooling "
                        "that expects the suffix.",
            steps_to_replicate=f"Inspect class attributes: {xpp.attributes}",
            suggested_fix=f"Rename class to '{xpp.name}_Extension'.",
        )]
    return []


def run(xpp: XppClass) -> list[dict]:
    """
    Deterministic naming convention review. Same input always produces
    the same output -- no API call, no sampling variance.

    NOT CHECKED (would need semantic/deeper parsing than xpp_parser
    currently provides -- flagged explicitly rather than silently
    dropped; these belong in xpp_parser.py as new extraction targets
    if you want them, not back in an LLM prompt):
      - Constant naming (#define / const)     -- parser doesn't extract macros
      - Query alias naming (select ... from)  -- parser doesn't extract query structure
      - Temporary table suffix ('Tmp')        -- parser doesn't expose TableType
      - Loop variable naming                  -- needs statement-level body parsing
    """
    issues = []

    for method in xpp.methods:
        if method.name.lower() in CONTROL_KEYWORDS:
            continue
        issues.extend(_check_parameter_prefix(method))
        issues.extend(_check_method_casing(method))

    issues.extend(_check_boolean_fields(xpp))
    issues.extend(_check_class_naming(xpp))
    issues.extend(_check_extension_suffix(xpp))

    for issue in issues:
        issue["agent"] = "Naming"

    return issues
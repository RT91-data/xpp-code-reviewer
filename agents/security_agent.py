"""
agents/security_agent.py

Reviews X++ code for security vulnerabilities:
- Direct SQL execution (statement.executeQuery with string concat)
- Missing SysEntryPoint attribute on public methods
- Hardcoded credentials or connection strings
- Missing cross-company guards (changeCompany)
- Unvalidated external inputs
- Missing access checks (checkRead, checkWrite, hasFieldAccess)
- Dangerous use of DictTable/DictField to bypass security
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass
from agents.base_agent import call_claude, ISSUE_SCHEMA


SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO / AX security architect
with deep expertise in X++ security patterns and D365 FnO security framework.

Your task: perform a thorough SECURITY review of the provided X++ class.

Check for ALL of the following:

1. DIRECT SQL INJECTION
   - Statement executeQuery / executeUpdate with string concatenation
   - Dynamic query construction using user input without parameterisation
   Example bad: stmt.executeQuery("SELECT * FROM " + tableName)

2. MISSING SysEntryPoint
   - Public methods callable from services/external clients must have [SysEntryPoint, true]
   - Missing attribute exposes methods to unauthenticated callers

3. MISSING ACCESS CHECKS
   - Table.checkRead() / checkWrite() not called before data access
   - Missing hasFieldAccess() for sensitive field reads
   - SysDatabaseLog bypass patterns

4. CROSS-COMPANY GUARD
   - Multi-company environments: changeCompany() used without proper guard
   - select statements missing crossCompany keyword where appropriate
   - Missing company range filters on shared tables

5. HARDCODED CREDENTIALS
   - Passwords, connection strings, API keys hardcoded as string literals
   - Server names, URLs hardcoded in production paths

6. UNVALIDATED INPUT
   - External parameters used directly in queries or file operations
   - Missing Global::validateString() or similar sanitisation

7. PRIVILEGE ESCALATION
   - RunAs patterns without proper authorisation checks
   - setConnection() calls that bypass security layer
   - Direct DictTable manipulation to bypass field-level security

Be specific. Reference exact method names and patterns from the code.
For each issue, provide a concrete X++ code fix.

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run security review on the parsed X++ class."""

    methods_summary = []
    for m in xpp.methods:
        methods_summary.append(
            f"\n// Method: {m.name} (line {m.start_line}-{m.end_line})\n"
            f"// Modifiers: {', '.join(m.modifiers) or 'none'}\n"
            f"// Attributes: {', '.join(m.attributes) or 'none'}\n"
            f"{m.return_type} {m.name}({m.parameters})\n{{\n{m.body}\n}}"
        )

    user_content = f"""Review this X++ class for security vulnerabilities.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Class Attributes: {', '.join(xpp.attributes) or 'None'}

Full source code:
```xpp
{xpp.raw_code}
```

Identify all security issues. Be thorough — missing security attributes and access checks
are as important as SQL injection."""

    issues = call_claude(SYSTEM_PROMPT, user_content)

    # Tag all issues with agent source
    for issue in issues:
        issue["agent"] = "Security"

    return issues

"""
agents/performance_agent.py

Reviews X++ code for performance anti-patterns:
- SELECT inside loops (the #1 D365 performance killer)
- Missing firstOnly on single-record selects
- Missing exists join (using count() to check existence)
- Unbounded selects (no where clause)
- Missing noFetch when only checking existence
- Large transaction scope (ttsbegin too early)
- RecordInsertList / RecordSortedList not used for bulk operations
- Missing index hints on large table queries
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass
from agents.base_agent import call_claude, ISSUE_SCHEMA


SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO performance engineer
with deep expertise in X++ query optimisation and AOS performance patterns.

Your task: perform a thorough PERFORMANCE review of the provided X++ class.

Check for ALL of the following:

1. SELECT INSIDE LOOP (Critical)
   - Any select statement inside a while, for, or do-while loop
   - This causes N+1 query problems — one DB round trip per iteration
   - Fix: use join, exists join, or collect IDs first then single select

2. MISSING firstOnly
   - select without firstOnly when only one record is needed
   - Every select without firstOnly fetches the full result set
   - Fix: add firstOnly to all single-record selects

3. EXISTENCE CHECK ANTI-PATTERN
   - Using select count(*) or fetching a record just to check if it exists
   - Fix: use exists join or select exists

4. UNBOUNDED SELECT (Major)
   - Select statements with no WHERE clause on large tables
   - Can return millions of rows and cause AOS memory issues
   - Flag if table is known large (CustTrans, VendTrans, InventTrans, LedgerTrans)

5. LARGE TRANSACTION SCOPE
   - ttsbegin placed far before the actual data modification
   - Holding locks for long periods causes blocking
   - Fix: move ttsbegin as close to the actual write as possible

6. MISSING BULK INSERT
   - Multiple individual table.insert() in a loop
   - Fix: use RecordInsertList for bulk inserts (10x+ faster)

7. MISSING noFetch
   - Joining tables that are only used in WHERE conditions
   - Add noFetch on tables whose fields are not needed in output

8. FORUPDATE WITHOUT NEED
   - select forupdate on records that are only read, not modified
   - Causes unnecessary row-level locks

9. CROSS-COMPANY PERFORMANCE
   - changeCompany blocks that contain heavy queries
   - Should be minimised and queries kept lightweight inside changeCompany

For each issue: name the method, describe the exact pattern, explain the D365-specific
consequence (blocking, timeouts, AOS crash), and provide fixed X++ code.

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run performance review on the parsed X++ class."""

    user_content = f"""Review this X++ class for performance issues.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Total lines: {xpp.total_lines}

Methods:
{', '.join(m.name for m in xpp.methods)}

Full source code:
```xpp
{xpp.raw_code}
```

Pay special attention to loops and database access patterns.
A select inside a while loop is ALWAYS a Critical issue in D365."""

    issues = call_claude(SYSTEM_PROMPT, user_content)

    for issue in issues:
        issue["agent"] = "Performance"

    return issues

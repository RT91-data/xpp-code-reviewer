"""
agents/bestpractice_agent.py

Reviews X++ code for best practice violations:
- Empty catch blocks (swallowed exceptions)
- Missing super() calls in overridden methods
- Hardcoded labels (strings instead of @SYS/label references)
- Missing ttsbegin/ttscommit around write operations
- Unbalanced ttsbegin/ttscommit
- Improper exception handling (catch all without logging)
- Missing Positive/Negative test considerations
- God class / method length
- Magic numbers
- Deprecated APIs usage
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass
from agents.base_agent import call_claude, ISSUE_SCHEMA


SYSTEM_PROMPT = f"""You are a senior Microsoft Dynamics 365 FnO technical lead
with deep expertise in X++ best practices, code quality, and maintainability.

Your task: perform a thorough BEST PRACTICES review of the provided X++ class.

Check for ALL of the following:

1. EMPTY CATCH BLOCKS (Critical)
   - catch {{}} or catch that only has a comment
   - Swallowed exceptions hide real errors in production
   - Fix: at minimum log with error(), warning(), or rethrow

2. MISSING super() CALLS
   - Override of init(), run(), validate(), modifiedField() etc. without calling super()
   - Breaks framework functionality silently
   - Fix: add super() at appropriate position (usually first line for init)

3. HARDCODED UI STRINGS (Major)
   - String literals used in error(), warning(), info(), Box::info() etc.
   - Should use label references: "@SYS12345" or "@MyModule:MyLabel"
   - Untranslatable and unmaintainable

4. MISSING ttsbegin/ttscommit (Critical)
   - Table.insert(), update(), delete() outside of transaction scope
   - Can cause partial data corruption
   - Fix: wrap all write operations in ttsbegin/ttscommit

5. UNBALANCED TRANSACTIONS
   - Multiple ttsbegin without matching ttscommit (or vice versa)
   - Nested transactions handled incorrectly
   - Fix: use try/catch with ttsabort in catch block

6. SWALLOWED EXCEPTIONS
   - catch(Exception::Error) with only retry or continue — no logging
   - catch blocks that set a boolean flag but never surface the error

7. MAGIC NUMBERS/STRINGS
   - Numeric literals like 30, 90, 1000 without named constants
   - Should use #define or const variables with meaningful names

8. GOD METHOD
   - Methods longer than 150 lines
   - Too many responsibilities in one method
   - Suggest decomposition into private helper methods

9. DEPRECATED API USAGE
   - NumberSeq old API (should use NumberSeqFormHandler)
   - DimensionAttributeValueSetStorage (if old pattern)
   - SecurityPolicy old patterns

10. MISSING VALIDATION RETURN CHECK
    - Calling validate() but not checking the return value
    - Calling checkMandatory() without acting on result

11. IMPROPER USE OF GLOBAL VARIABLES
    - Class-level variables used as method output parameters
    - Should use return values or output parameters explicitly

{ISSUE_SCHEMA}"""


def run(xpp: XppClass) -> list[dict]:
    """Run best practice review on the parsed X++ class."""

    user_content = f"""Review this X++ class for best practice violations.

Class: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}
Total lines: {xpp.total_lines}
Methods: {len(xpp.methods)}

Full source code:
```xpp
{xpp.raw_code}
```

Be thorough. Empty catch blocks and missing super() calls are very common
in D365 customisations and must always be flagged."""

    issues = call_claude(SYSTEM_PROMPT, user_content)

    for issue in issues:
        issue["agent"] = "Best Practice"

    return issues

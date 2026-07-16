"""
agents/testcase_agent.py

Generates a complete SysTestCase class for the reviewed X++ class.
Covers:
- Positive test cases (happy path)
- Negative test cases (invalid inputs, boundary violations)
- Edge cases (null/empty, max values, concurrent access)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parsers.xpp_parser import XppClass
from agents.base_agent import client, MODEL
import re


SYSTEM_PROMPT = """You are a senior Microsoft Dynamics 365 FnO test engineer
with expertise in X++ unit testing using the SysTestCase framework.

Your task: generate a COMPLETE, COMPILABLE X++ test class for the provided class.

Requirements:
1. Extend SysTestCase
2. Name: <ClassName>_Test
3. Include setUp() and tearDown() methods
4. For each PUBLIC method, write:
   - At least one positive test (happy path)
   - At least one negative test (invalid input, expected failure)
   - At least one edge case (null, empty string, max value, zero, negative number)
5. Use SysTestCase assertion methods:
   - this.assertEquals(expected, actual, "message")
   - this.assertTrue(condition, "message")
   - this.assertFalse(condition, "message")
   - this.assertNotNull(value, "message")
   - this.parmExceptionExpected(Exception::Error) for negative tests
6. Use SysTestSuite for any required test data setup
7. Use ttsbegin/ttsabort pattern in tearDown to roll back test data
8. Add XML doc comments explaining what each test validates

Return ONLY the complete X++ class code. No explanation, no markdown fences."""


def run(xpp: XppClass) -> dict:
    """
    Generate SysTestCase class for the parsed X++ class.
    Returns dict with keys: class_name, code, method_count
    """

    # Build method signatures for context
    method_details = []
    for m in xpp.methods:
        if 'private' not in m.modifiers:  # Focus on public/protected
            method_details.append(
                f"Method: {m.name}\n"
                f"  Return type: {m.return_type}\n"
                f"  Parameters: {m.parameters or 'none'}\n"
                f"  Modifiers: {', '.join(m.modifiers) or 'public'}\n"
                f"  Body preview: {m.body[:200].strip()}..."
            )

    user_content = f"""Generate a complete SysTestCase class for this X++ class.

Class to test: {xpp.name}
Extends: {xpp.extends or 'None'}
Type: {xpp.class_type}

Public/Protected methods to test:
{chr(10).join(method_details) if method_details else 'No public methods found - test class-level behaviour'}

Full source code:
```xpp
{xpp.raw_code}
```

Generate comprehensive tests covering positive, negative, and edge cases.
The test class must be ready to compile and run in D365 FnO without modification.
Use realistic D365 test data patterns (e.g., use SysDataInitRequest or create test records directly).
"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}]
        )
        code = response.content[0].text.strip()

        # Strip any accidental markdown fences
        code = re.sub(r'```(?:xpp|x\+\+|csharp)?\n?', '', code).strip()
        code = code.rstrip('`').strip()

        # Count generated test methods
        test_method_count = len(re.findall(r'\bpublic\s+void\s+test\w+', code, re.IGNORECASE))

        return {
            "class_name": f"{xpp.name}_Test",
            "code":        code,
            "method_count": test_method_count,
        }

    except Exception as e:
        print(f"[testcase_agent] Failed: {e}")
        return {
            "class_name": f"{xpp.name}_Test",
            "code":        f"// Test generation failed: {e}",
            "method_count": 0,
        }

"""
parsers/xpp_parser.py

Parses X++ source files to extract:
- Class name, type, parent class
- Method names, signatures, bodies
- Class-level field declarations
- Attributes (SysEntryPoint, etc.)

No third-party dependencies — pure Python regex.
"""

import re
from dataclasses import dataclass, field


@dataclass
class XppMethod:
    name: str
    return_type: str
    parameters: str
    body: str
    modifiers: list       # public, private, protected, static, server, client
    attributes: list      # [SysEntryPoint, true], etc.
    start_line: int
    end_line: int


@dataclass
class XppClass:
    name: str
    extends: str          # parent class, empty if none
    implements: list      # interfaces
    class_type: str       # class, table, form, report, query, enum
    attributes: list      # class-level attributes
    fields: list          # class-level variable declarations
    methods: list         # list of XppMethod
    raw_code: str         # full source for agents to reference
    total_lines: int


def _extract_balanced_braces(code: str, start: int) -> tuple[str, int]:
    """
    Extract balanced {} block starting at position `start`.
    Returns (content_between_braces, end_position).
    """
    depth = 0
    i = start
    body_start = -1

    while i < len(code):
        ch = code[i]
        if ch == '{':
            depth += 1
            if body_start == -1:
                body_start = i
        elif ch == '}':
            depth -= 1
            if depth == 0 and body_start != -1:
                return code[body_start + 1:i], i
        i += 1

    return "", len(code) - 1


def _get_line_number(code: str, pos: int) -> int:
    return code[:pos].count('\n') + 1


def parse_xpp(source_code: str) -> XppClass:
    """
    Main entry point. Takes raw X++ source, returns XppClass.
    """
    code = source_code
    total_lines = code.count('\n') + 1

    # ── CLASS DECLARATION ─────────────────────────────────────────
    # Matches: [public] class ClassName [extends Parent] [implements IFace]
    class_pattern = re.compile(
        r'(?:public\s+|private\s+|protected\s+|final\s+|abstract\s+)*'
        r'(class|table|interface|enum)\s+(\w+)'
        r'(?:\s+extends\s+(\w+))?'
        r'(?:\s+implements\s+([\w,\s]+?))?'
        r'\s*\{',
        re.IGNORECASE
    )

    class_match = class_pattern.search(code)
    if not class_match:
        # Fallback: try to get any class-like name from the file
        name_match = re.search(r'class\s+(\w+)', code, re.IGNORECASE)
        class_name = name_match.group(1) if name_match else "UnknownClass"
        return XppClass(
            name=class_name, extends="", implements=[],
            class_type="class", attributes=[], fields=[],
            methods=[], raw_code=code, total_lines=total_lines
        )

    class_type  = class_match.group(1).lower()
    class_name  = class_match.group(2)
    extends     = class_match.group(3) or ""
    implements_str = class_match.group(4) or ""
    implements  = [i.strip() for i in implements_str.split(',') if i.strip()]

    # ── CLASS-LEVEL ATTRIBUTES ────────────────────────────────────
    pre_class = code[:class_match.start()]
    attr_pattern = re.compile(r'\[([^\]]+)\]')
    class_attributes = [m.group(1) for m in attr_pattern.finditer(pre_class)]

    # ── CLASS BODY ────────────────────────────────────────────────
    class_body, class_end = _extract_balanced_braces(code, class_match.end() - 1)

    # ── FIELD DECLARATIONS ────────────────────────────────────────
    # Lines that look like type declarations before any method
    field_pattern = re.compile(
        r'^\s*((?:public|private|protected|static|server|client)\s+)*'
        r'(\w+(?:\s+\w+)?)\s+(\w+)\s*(?:=\s*[^;]+)?;',
        re.MULTILINE
    )
    fields = []
    for m in field_pattern.finditer(class_body):
        # Skip if inside a method (rough heuristic: method bodies are handled separately)
        line = m.group(0).strip()
        if line and not line.startswith('//'):
            fields.append(line)

    # ── METHOD EXTRACTION ─────────────────────────────────────────
    # Method signature: [modifiers] returnType methodName([params])
    method_sig_pattern = re.compile(
        r'(?:^|\n)'
        r'((?:\s*(?:\[[^\]]*\]\s*)*)'            # attributes like [SysEntryPoint]
        r'(?:(?:public|private|protected|static|server|client|final|abstract|display|edit|noPrefix)\s+)*)'
        r'(\w+(?:\s*\*)?)\s+'                     # return type
        r'(\w+)\s*'                               # method name
        r'\(([^)]*)\)\s*'                         # parameters
        r'(?:server|client)?\s*'                  # optional server/client keyword
        r'\{',
        re.MULTILINE
    )

    methods = []
    for m in method_sig_pattern.finditer(class_body):
        prefix      = m.group(1).strip()
        return_type = m.group(2).strip()
        method_name = m.group(3).strip()
        parameters  = m.group(4).strip()

        # Skip keywords that look like methods
        if method_name.lower() in {'if', 'while', 'for', 'switch', 'try', 'catch', 'finally'}:
            continue

        # Extract modifiers and attributes from prefix
        modifiers  = re.findall(
            r'\b(public|private|protected|static|server|client|final|abstract|display|edit)\b',
            prefix, re.IGNORECASE
        )
        attributes = re.findall(r'\[([^\]]+)\]', prefix)

        # Get method body
        brace_pos = m.end() - 1
        body, end_pos = _extract_balanced_braces(class_body, brace_pos)

        # Line numbers (within original file)
        abs_start = code.find(class_body) + m.start()
        abs_end   = code.find(class_body) + end_pos
        start_line = _get_line_number(code, abs_start)
        end_line   = _get_line_number(code, abs_end)

        methods.append(XppMethod(
            name=method_name,
            return_type=return_type,
            parameters=parameters,
            body=body,
            modifiers=modifiers,
            attributes=attributes,
            start_line=start_line,
            end_line=end_line,
        ))

    return XppClass(
        name=class_name,
        extends=extends,
        implements=implements,
        class_type=class_type,
        attributes=class_attributes,
        fields=fields,
        methods=methods,
        raw_code=code,
        total_lines=total_lines,
    )


def parser_summary(xpp: XppClass) -> str:
    """Human-readable summary for debugging."""
    lines = [
        f"Class:    {xpp.name}",
        f"Type:     {xpp.class_type}",
        f"Extends:  {xpp.extends or 'None'}",
        f"Methods:  {len(xpp.methods)}",
        f"Fields:   {len(xpp.fields)}",
        f"Lines:    {xpp.total_lines}",
        "",
        "Methods found:",
    ]
    for m in xpp.methods:
        lines.append(f"  [{m.return_type}] {m.name}({m.parameters[:40]}) — line {m.start_line}")
    return "\n".join(lines)

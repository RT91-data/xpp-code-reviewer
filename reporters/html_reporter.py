"""
reporters/html_reporter.py

Generates a self-contained SonarQube-style HTML report.
No external dependencies — all CSS and JS inline.
"""

from datetime import datetime


SEVERITY_ORDER = {"Critical": 0, "Major": 1, "Minor": 2, "Info": 3}
SEVERITY_COLOR = {
    "Critical": "#e53e3e",
    "Major":    "#dd6b20",
    "Minor":    "#d69e2e",
    "Info":     "#3182ce",
}
SEVERITY_BG = {
    "Critical": "#fff5f5",
    "Major":    "#fffaf0",
    "Minor":    "#fffff0",
    "Info":     "#ebf8ff",
}


def _badge(severity: str) -> str:
    color = SEVERITY_COLOR.get(severity, "#718096")
    return (f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:12px;font-size:12px;font-weight:600;">{severity}</span>')


def _escape(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def generate(
    class_name: str,
    issues: list[dict],
    test_case: dict,
    raw_code: str,
    filename: str = "",
) -> str:
    """
    Generate the full HTML report as a string.

    Args:
        class_name:  Name of the reviewed X++ class
        issues:      List of issue dicts from all agents
        test_case:   Dict with keys: class_name, code, method_count
        raw_code:    Original X++ source code
        filename:    Original filename (optional)
    """

    # Sort issues by severity
    sorted_issues = sorted(
        issues,
        key=lambda x: SEVERITY_ORDER.get(x.get("severity", "Info"), 99)
    )

    # Counts
    counts = {"Critical": 0, "Major": 0, "Minor": 0, "Info": 0}
    for issue in sorted_issues:
        sev = issue.get("severity", "Info")
        counts[sev] = counts.get(sev, 0) + 1

    total = len(sorted_issues)
    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    # ── ISSUE CARDS ───────────────────────────────────────────────
    issue_cards = []
    for i, issue in enumerate(sorted_issues, 1):
        sev      = issue.get("severity", "Info")
        bg       = SEVERITY_BG.get(sev, "#f7fafc")
        border   = SEVERITY_COLOR.get(sev, "#718096")
        agent    = issue.get("agent", "")
        category = issue.get("category", "")
        method   = issue.get("method", "")
        line     = issue.get("line_hint", "")

        card = f"""
        <div class="issue-card" data-severity="{_escape(sev)}" data-agent="{_escape(agent)}"
             style="border-left:4px solid {border};background:{bg};
                    margin-bottom:16px;border-radius:6px;padding:16px 20px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
            <div>
              {_badge(sev)}
              <span style="margin-left:8px;font-size:12px;color:#718096;background:#edf2f7;
                           padding:2px 8px;border-radius:10px;">{_escape(agent)}</span>
              <span style="margin-left:6px;font-size:12px;color:#718096;background:#edf2f7;
                           padding:2px 8px;border-radius:10px;">{_escape(category)}</span>
            </div>
            <span style="font-size:12px;color:#a0aec0;">#{i}</span>
          </div>

          <h3 style="margin:0 0 8px 0;font-size:15px;color:#2d3748;">{_escape(issue.get('title',''))}</h3>

          {'<div style="font-size:12px;color:#718096;margin-bottom:10px;">📍 Method: <strong>' + _escape(method) + '</strong>' + (f'  |  Line: {_escape(line)}' if line else '') + '</div>' if method else ''}

          <div style="margin-bottom:10px;">
            <strong style="font-size:13px;color:#4a5568;">Description</strong>
            <p style="margin:4px 0 0 0;color:#4a5568;font-size:14px;">{_escape(issue.get('description',''))}</p>
          </div>

          <div style="margin-bottom:10px;">
            <strong style="font-size:13px;color:#c53030;">Consequence</strong>
            <p style="margin:4px 0 0 0;color:#4a5568;font-size:14px;">{_escape(issue.get('consequence',''))}</p>
          </div>

          <div style="margin-bottom:10px;">
            <strong style="font-size:13px;color:#744210;">Steps to Replicate</strong>
            <p style="margin:4px 0 0 0;color:#4a5568;font-size:14px;white-space:pre-wrap;">{_escape(issue.get('steps_to_replicate',''))}</p>
          </div>

          <div>
            <strong style="font-size:13px;color:#276749;">Suggested Fix</strong>
            <pre style="margin:6px 0 0 0;background:#1a202c;color:#68d391;padding:12px;
                        border-radius:4px;font-size:12px;overflow-x:auto;white-space:pre-wrap;">{_escape(issue.get('suggested_fix',''))}</pre>
          </div>
        </div>"""
        issue_cards.append(card)

    issues_html = "\n".join(issue_cards) if issue_cards else \
        '<p style="color:#48bb78;font-size:16px;">✅ No issues found.</p>'

    # ── SUMMARY CARDS ─────────────────────────────────────────────
    summary_cards = ""
    for sev in ["Critical", "Major", "Minor", "Info"]:
        count = counts.get(sev, 0)
        color = SEVERITY_COLOR[sev]
        summary_cards += f"""
        <div style="background:white;border-radius:8px;padding:20px;text-align:center;
                    box-shadow:0 1px 3px rgba(0,0,0,0.1);border-top:4px solid {color};">
          <div style="font-size:36px;font-weight:700;color:{color};">{count}</div>
          <div style="font-size:14px;color:#718096;margin-top:4px;">{sev}</div>
        </div>"""

    # ── TEST CASE SECTION ─────────────────────────────────────────
    # Only rendered when test_case actually has generated code. When
    # test generation is disabled (see app.py ENABLE_TEST_GENERATION),
    # test_case comes in as {"code": "", ...} and this section is
    # omitted entirely -- an empty "Generated Test Class" box with a
    # non-functional Copy button would look broken, not just unused.
    test_code    = test_case.get("code", "") if test_case else ""
    test_class   = test_case.get("class_name", "") if test_case else ""
    test_methods = test_case.get("method_count", 0) if test_case else 0

    if test_code.strip():
        test_section_html = f"""
  <!-- TEST CASES -->
  <div class="section">
    <div class="section-title">🧪 Generated Test Class</div>
    <div style="margin-bottom:14px;">
      <span style="font-size:14px;color:#4a5568;">Class: <strong>{_escape(test_class)}</strong></span>
      <span style="margin-left:16px;font-size:14px;color:#4a5568;">
        Test methods: <strong>{test_methods}</strong></span>
    </div>
    <div style="display:flex;justify-content:flex-end;margin-bottom:10px;">
      <button onclick="copyTestCode()" style="padding:6px 16px;background:#2d3748;color:white;
              border:none;border-radius:4px;cursor:pointer;font-size:13px;">
        📋 Copy Code
      </button>
    </div>
    <pre id="testCode" style="background:#1a202c;color:#e2e8f0;padding:20px;border-radius:6px;
         font-size:12px;overflow-x:auto;white-space:pre-wrap;max-height:600px;overflow-y:auto;">{_escape(test_code)}</pre>
  </div>"""
    else:
        test_section_html = ""

    # ── FULL HTML ─────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Code Review — {_escape(class_name)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f7fafc; color: #2d3748; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
  .header {{ background: linear-gradient(135deg, #1a202c 0%, #2d3748 100%);
             color: white; padding: 32px; border-radius: 10px; margin-bottom: 28px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr);
                   gap: 16px; margin-bottom: 28px; }}
  .section {{ background: white; border-radius: 8px; padding: 24px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }}
  .section-title {{ font-size: 18px; font-weight: 700; color: #2d3748; margin-bottom: 18px;
                    padding-bottom: 10px; border-bottom: 2px solid #e2e8f0; }}
  .filter-bar {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }}
  .filter-btn {{ padding: 5px 14px; border-radius: 20px; border: 1px solid #e2e8f0;
                 background: white; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
  .filter-btn.active {{ background: #2d3748; color: white; border-color: #2d3748; }}
  .filter-btn:hover {{ background: #edf2f7; }}
  code {{ font-family: 'Fira Code', 'Cascadia Code', Consolas, monospace; }}
  pre {{ font-family: 'Fira Code', 'Cascadia Code', Consolas, monospace; }}
  @media (max-width: 640px) {{ .summary-grid {{ grid-template-columns: repeat(2,1fr); }} }}
</style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <div style="font-size:13px;color:#a0aec0;margin-bottom:6px;">X++ Source Code Review</div>
    <h1 style="font-size:28px;font-weight:700;margin-bottom:6px;">{_escape(class_name)}</h1>
    {'<div style="font-size:14px;color:#a0aec0;">File: ' + _escape(filename) + '</div>' if filename else ''}
    <div style="font-size:13px;color:#718096;margin-top:8px;">Generated {generated_at}</div>
  </div>

  <!-- SUMMARY -->
  <div class="summary-grid">
    {summary_cards}
  </div>
  <div style="text-align:right;font-size:13px;color:#718096;margin:-16px 0 24px 0;">
    Total issues: <strong>{total}</strong>
  </div>

  <!-- ISSUES -->
  <div class="section">
    <div class="section-title">📋 Issues</div>

    <div class="filter-bar" id="filterBar">
      <button class="filter-btn active" onclick="filterIssues('all')">All ({total})</button>
      <button class="filter-btn" onclick="filterIssues('Critical')" style="color:{SEVERITY_COLOR['Critical']}">
        Critical ({counts['Critical']})</button>
      <button class="filter-btn" onclick="filterIssues('Major')" style="color:{SEVERITY_COLOR['Major']}">
        Major ({counts['Major']})</button>
      <button class="filter-btn" onclick="filterIssues('Minor')" style="color:{SEVERITY_COLOR['Minor']}">
        Minor ({counts['Minor']})</button>
      <button class="filter-btn" onclick="filterIssues('Info')" style="color:{SEVERITY_COLOR['Info']}">
        Info ({counts['Info']})</button>
    </div>

    <div id="issueContainer">
      {issues_html}
    </div>
  </div>

  {test_section_html}

  <!-- ORIGINAL CODE -->
  <div class="section">
    <div class="section-title">📄 Source Code</div>
    <pre style="background:#1a202c;color:#e2e8f0;padding:20px;border-radius:6px;
         font-size:12px;overflow-x:auto;white-space:pre-wrap;max-height:500px;overflow-y:auto;">{_escape(raw_code)}</pre>
  </div>

</div>

<script>
function filterIssues(severity) {{
  const cards = document.querySelectorAll('.issue-card');
  cards.forEach(card => {{
    if (severity === 'all' || card.dataset.severity === severity) {{
      card.style.display = 'block';
    }} else {{
      card.style.display = 'none';
    }}
  }});
  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
  event.target.classList.add('active');
}}

function copyTestCode() {{
  const code = document.getElementById('testCode').innerText;
  navigator.clipboard.writeText(code).then(() => {{
    alert('Test code copied to clipboard!');
  }});
}}
</script>
</body>
</html>"""

    return html
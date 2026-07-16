"""
app.py — X++ Source Code Review Agent (Streamlit)
"""

import streamlit as st
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

# Add project root to path so imports work
sys.path.insert(0, str(Path(__file__).parent))

from parsers.xpp_parser import parse_xpp, parser_summary
from agents.security_agent     import run as security_review
from agents.performance_agent  import run as performance_review
from agents.bestpractice_agent import run as bestpractice_review
from agents.naming_agent       import run as naming_review
from agents.testcase_agent     import run as generate_tests
from agents.base_agent         import AgentCallError
from reporters.html_reporter   import generate as generate_report

# ── TEST GENERATION: DISABLED ──────────────────────────────────────
# Reasoning (2026-07-16): testcase_agent generates SysTestCase code as
# text but nothing executes it -- there's no AOS/compiler in this
# pipeline, and X++ can't be run standalone against a class file since
# SysTestCase needs the real AOT (tables, EDTs, extensions) to compile
# and run at all. Shipping "test cases" in the report next to verified
# static-analysis findings implied a pass/fail that never actually ran.
# Re-enable once there's a real FnO dev/sandbox environment to run
# these against -- flip this back to True, nothing else needs to change.
ENABLE_TEST_GENERATION = False

# ── CONFIG ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="X++ Code Reviewer",
    page_icon="🔍",
    layout="wide"
)

# ── SESSION STATE DEFAULTS ─────────────────────────────────────────
# Used so the Clear button can wipe results without losing the widget
# tree, and so re-running doesn't require re-uploading the file.
if "review_ran" not in st.session_state:
    st.session_state.review_ran = False
if "all_issues" not in st.session_state:
    st.session_state.all_issues = []
if "test_result" not in st.session_state:
    st.session_state.test_result = None
if "agent_errors" not in st.session_state:
    st.session_state.agent_errors = {}
if "source_code" not in st.session_state:
    st.session_state.source_code = ""
if "filename" not in st.session_state:
    st.session_state.filename = ""
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

# ── HEADER ────────────────────────────────────────────────────────
header_col1, header_col2 = st.columns([5, 1])
with header_col1:
    st.title("🔍 X++ Source Code Review Agent")
    st.caption("Powered by Claude · Security · Performance · Best Practices · Naming · Test Generation")
with header_col2:
    st.write("")  # vertical spacer to align button with title
    if st.button("🗑️ Clear", use_container_width=True):
        st.session_state.review_ran = False
        st.session_state.all_issues = []
        st.session_state.test_result = None
        st.session_state.agent_errors = {}
        st.session_state.source_code = ""
        st.session_state.filename = ""
        # Bumping this key forces the file_uploader widget to reset,
        # since Streamlit widgets are keyed by identity, not just value.
        st.session_state.uploader_key += 1
        st.rerun()

if not os.environ.get("ANTHROPIC_API_KEY"):
    st.error("ANTHROPIC_API_KEY not set in .env")
    st.stop()

# ── SIDEBAR ───────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Review Options")

    run_security     = st.checkbox("🛡️ Security Review",      value=True)
    run_performance  = st.checkbox("⚡ Performance Review",    value=True)
    run_bestpractice = st.checkbox("✅ Best Practices",        value=True)
    run_naming       = st.checkbox("📝 Naming Conventions",    value=True)

    if ENABLE_TEST_GENERATION:
        run_tests = st.checkbox("🧪 Generate Test Cases", value=True)
    else:
        run_tests = False
        st.checkbox(
            "🧪 Generate Test Cases (disabled)",
            value=False,
            disabled=True,
            help="Off until there's a real FnO env to actually run these "
                 "against — generated-but-unexecuted test code next to "
                 "verified findings was misleading. See ENABLE_TEST_GENERATION.",
        )

    st.divider()
    st.caption("**Severity guide:**")
    st.caption("🔴 Critical — fix before release")
    st.caption("🟠 Major — fix in current sprint")
    st.caption("🟡 Minor — fix when touching code")
    st.caption("🔵 Info — consider improving")

# ── FILE UPLOAD ───────────────────────────────────────────────────
st.subheader("Upload X++ File")

col1, col2 = st.columns([2, 1])

with col1:
    uploaded_file = st.file_uploader(
        "Upload .xpp file",
        type=["xpp", "txt"],
        help="Upload a single X++ class file",
        key=f"uploader_{st.session_state.uploader_key}",
    )

with col2:
    st.markdown("**Or paste code directly:**")
    use_paste = st.checkbox("Paste code instead")

source_code = ""
filename    = ""

if use_paste:
    source_code = st.text_area(
        "Paste X++ code here",
        height=300,
        placeholder="class MyClass extends RunBase\n{\n    // ...\n}"
    )
    filename = "pasted_code.xpp"
elif uploaded_file:
    source_code = uploaded_file.read().decode("utf-8", errors="replace")
    filename    = uploaded_file.name

# ── REVIEW BUTTON ─────────────────────────────────────────────────
if source_code.strip():
    st.divider()

    if st.button("🚀 Run Code Review", type="primary", use_container_width=True):

        # Parse the X++ file
        with st.spinner("Parsing X++ code..."):
            xpp = parse_xpp(source_code)

        st.success(f"✅ Parsed: **{xpp.name}** — {len(xpp.methods)} methods, {xpp.total_lines} lines")

        with st.expander("📊 Parser details"):
            st.code(parser_summary(xpp))

        # ── BUILD AGENT LIST ────────────────────────────────────
        agents_to_run = []
        if run_security:     agents_to_run.append(("🛡️ Security",       security_review))
        if run_performance:  agents_to_run.append(("⚡ Performance",     performance_review))
        if run_bestpractice: agents_to_run.append(("✅ Best Practices",  bestpractice_review))
        if run_naming:       agents_to_run.append(("📝 Naming",          naming_review))

        all_issues   = []
        agent_errors = {}
        test_result  = {"class_name": f"{xpp.name}_Test", "code": "", "method_count": 0}

        total_steps = len(agents_to_run) + (1 if run_tests else 0)
        progress = st.progress(0)
        status   = st.empty()
        step     = 0

        # ── RUN REVIEW AGENTS IN PARALLEL ───────────────────────
        # These are network-bound Claude API calls, not CPU-bound work,
        # so a ThreadPoolExecutor gets real concurrency despite the GIL
        # (the GIL is released while waiting on the socket).
        # Widget mutation (progress/status) happens on the MAIN thread
        # only, via the results of as_completed -- Streamlit's
        # session/script-run model isn't safe to mutate from worker
        # threads directly.
        if agents_to_run:
            status.info(f"Running {len(agents_to_run)} agents in parallel...")
            with ThreadPoolExecutor(max_workers=len(agents_to_run)) as executor:
                future_to_name = {
                    executor.submit(agent_fn, xpp): agent_name
                    for agent_name, agent_fn in agents_to_run
                }
                for future in as_completed(future_to_name):
                    agent_name = future_to_name[future]
                    try:
                        issues = future.result()
                        all_issues.extend(issues)
                        status.success(f"{agent_name}: {len(issues)} issue(s) found")
                    except AgentCallError as e:
                        agent_errors[agent_name] = str(e)
                        status.error(f"{agent_name} failed: {e}")
                    except Exception as e:
                        agent_errors[agent_name] = str(e)
                        status.error(f"{agent_name} failed unexpectedly: {e}")
                    step += 1
                    progress.progress(step / total_steps)

        # ── TEST GENERATION (kept sequential — depends on nothing else,
        # but no strong reason to parallelize a single call) ─────────
        # Gated by ENABLE_TEST_GENERATION (see import block above) —
        # will not run until that's flipped to True.
        if run_tests:
            status.info("🧪 Generating test cases...")
            t0 = time.time()
            try:
                test_result = generate_tests(xpp)
                elapsed = round(time.time() - t0, 1)
                status.success(
                    f"🧪 Test class generated: "
                    f"{test_result['method_count']} test methods in {elapsed}s"
                )
            except AgentCallError as e:
                agent_errors["🧪 Test Generation"] = str(e)
                status.error(f"Test generation failed: {e}")
            except Exception as e:
                agent_errors["🧪 Test Generation"] = str(e)
                status.error(f"Test generation failed unexpectedly: {e}")
            step += 1
            progress.progress(1.0)

        status.empty()
        progress.empty()

        # Persist results to session_state so they survive reruns
        # (e.g. widget interactions) until Clear is pressed.
        st.session_state.review_ran   = True
        st.session_state.all_issues   = all_issues
        st.session_state.test_result  = test_result
        st.session_state.agent_errors = agent_errors
        st.session_state.source_code  = source_code
        st.session_state.filename     = filename

# ── RESULTS (rendered from session_state so Clear / rerun works) ──
if st.session_state.review_ran:
    all_issues  = st.session_state.all_issues
    test_result = st.session_state.test_result
    filename    = st.session_state.filename
    source_code = st.session_state.source_code

    st.divider()

    if st.session_state.agent_errors:
        with st.expander("⚠️ Some agents failed — click for details", expanded=True):
            for agent_name, err in st.session_state.agent_errors.items():
                st.error(f"**{agent_name}**: {err}")
            st.caption(
                "A failed agent means its issues are NOT included below — "
                "this is not the same as 'no issues found'. Re-run if the "
                "failure looks transient (e.g. truncation)."
            )

    st.subheader("📋 Review Summary")

    counts = {"Critical": 0, "Major": 0, "Minor": 0, "Info": 0}
    for issue in all_issues:
        sev = issue.get("severity", "Info")
        counts[sev] = counts.get(sev, 0) + 1

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Issues",  len(all_issues))
    c2.metric("🔴 Critical",   counts["Critical"])
    c3.metric("🟠 Major",      counts["Major"])
    c4.metric("🟡 Minor",      counts["Minor"])
    c5.metric("🔵 Info",       counts["Info"])

    # ── ISSUE TABS ────────────────────────────────────────────
    if all_issues:
        st.subheader("Issues by Category")

        agent_groups = {}
        for issue in all_issues:
            agent = issue.get("agent", "Other")
            agent_groups.setdefault(agent, []).append(issue)

        tabs = st.tabs(list(agent_groups.keys()))
        for tab, (agent_name, issues) in zip(tabs, agent_groups.items()):
            with tab:
                for issue in sorted(
                    issues,
                    key=lambda x: {"Critical":0,"Major":1,"Minor":2,"Info":3}.get(
                        x.get("severity","Info"), 99)
                ):
                    sev    = issue.get("severity", "Info")
                    colors = {"Critical":"🔴","Major":"🟠","Minor":"🟡","Info":"🔵"}
                    icon   = colors.get(sev, "⚪")

                    with st.expander(
                        f"{icon} [{sev}] {issue.get('title','')[:80]} "
                        f"— {issue.get('method','')}"
                    ):
                        st.markdown(f"**Category:** {issue.get('category','')}")
                        if issue.get('line_hint'):
                            st.markdown(f"**Line:** {issue.get('line_hint')}")
                        st.markdown(f"**Description:** {issue.get('description','')}")
                        st.markdown(f"**Consequence:** {issue.get('consequence','')}")
                        st.markdown(f"**Steps to replicate:** {issue.get('steps_to_replicate','')}")
                        if issue.get('suggested_fix'):
                            st.code(issue.get('suggested_fix',''), language="text")

    # ── TEST CASES ────────────────────────────────────────────
    if test_result and test_result.get("code"):
        st.subheader(f"🧪 Generated Test Class: {test_result['class_name']}")
        st.caption(f"{test_result['method_count']} test methods")
        st.code(test_result["code"], language="text")

    # ── GENERATE HTML REPORT ──────────────────────────────────
    st.divider()
    st.subheader("📥 Download Report")

    with st.spinner("Generating HTML report..."):
        html_report = generate_report(
            class_name=filename.rsplit(".", 1)[0] if filename else "review",
            issues=all_issues,
            test_case=test_result,
            raw_code=source_code,
            filename=filename,
        )

    st.download_button(
        label="⬇️ Download HTML Report",
        data=html_report,
        file_name=f"review_{time.strftime('%Y%m%d_%H%M')}.html",
        mime="text/html",
        type="primary",
        use_container_width=True,
    )
    st.caption("Open the downloaded HTML file in any browser — no server needed.")

elif not source_code.strip():
    st.info("👆 Upload an X++ file or paste code above to begin.")

    with st.expander("💡 What this tool reviews"):
        st.markdown("""
**Security**
- SQL injection via string concatenation
- Missing SysEntryPoint attributes
- Missing access checks (checkRead/checkWrite)
- Cross-company guard violations
- Hardcoded credentials

**Performance**
- SELECT inside loops (N+1 query problem)
- Missing firstOnly on single-record selects
- Unbounded selects on large tables
- Missing bulk insert patterns

**Best Practices**
- Empty catch blocks
- Missing super() calls
- Hardcoded strings instead of labels
- Unbalanced ttsbegin/ttscommit

**Naming Conventions**
- Parameter prefix (_paramName)
- Method and class naming
- Extension class suffix (_Extension)

**Test Generation**
- Complete SysTestCase class
- Positive, negative, and edge cases
- Ready to compile in D365 FnO
        """)
# X++ Code Review Agent

An AI-assisted static review tool for Microsoft Dynamics 365 Finance & Operations (X++) source files. Runs specialist review agents in parallel over a class and produces a filterable HTML report.

Built as a hybrid: **deterministic parsing for pattern-matchable rules, Claude for semantic/judgment-based review** — not a pure LLM pipeline. See [Known Limitations](#known-limitations) for why, and [Architecture note](#architecture-note) for how the split works and how it's verified.

## What it checks

Every agent below is a **hybrid**: a deterministic Python pass (zero API cost, reproducible by construction) plus a trimmed Claude call for findings that genuinely need judgment.

| Agent | Deterministic checks (no LLM) | LLM checks (judgment required) |
|---|---|---|
| 🛡️ Security | Missing `[SysEntryPoint]`, SQL injection via string concat into `executeQuery`/`executeUpdate`, missing `checkWrite()` before writes, hardcoded credentials | Cross-company guard correctness, unvalidated input data-flow, privilege escalation intent |
| ⚡ Performance | `select` inside a loop (incl. X++'s `while select` construct), unbounded select on known large tables, `forupdate` with no write, missing bulk insert | Missing `firstOnly` (intent-dependent), existence-check anti-pattern, missing `noFetch`, cross-company query weight |
| ✅ Best Practices | Empty catch blocks, missing `super()` on framework lifecycle methods, unbalanced `ttsbegin`/`ttscommit`, writes with no `ttsbegin`, hardcoded UI strings, god methods (>150 lines) | Magic numbers/strings, deprecated API usage, missing validation-return-check, improper class-level variable use, swallowed-exception nuance |
| 📝 Naming Conventions | Parameter prefix (`_paramName`), method/class casing, boolean field naming, extension class suffix | — fully deterministic, no LLM call at all |
| 🧪 Test Generation | **Disabled** | See below |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file (never commit this — it's gitignored):

```
ANTHROPIC_API_KEY=your_key_here
```

Run it:

```bash
streamlit run app.py
```

Upload a `.xpp` file or paste code, select which review agents to run, and click **Run Code Review**. Agents run concurrently via `ThreadPoolExecutor`. Download the result as a self-contained HTML report (no server needed to view it).

## Known limitations

This is a working tool, not a certified security scanner. Be direct about these when discussing it:

- **LLM-judgment findings are not fully deterministic; static findings are.** `temperature=0` is set, and all four agents' deterministic checks (see table above) are provably reproducible — verified by running the same file repeatedly and diffing output, not just asserted. Critical/Major severity findings that come from static analysis have been stable across every repeated real-file test run so far. What still varies is confined to the LLM-only judgment checks, and that's expected: the underlying question ("does this need a business guardrail," "is this actually exploitable") is genuinely ambiguous, not a bug to engineer away.
- **One specific known-flaky check, not yet moved to deterministic:** Security's "container of IDs used for a bulk write with no visible ownership/authorization check" finding is still LLM-only and has been observed missing entirely in roughly 1 of 3 runs on the same file, while the write itself always gets flagged for other reasons (missing `checkWrite`, missing `SysEntryPoint`). This is a real gap, not a rounding error — if this tool is ever used for anything beyond personal review, either fix the catch rate (candidate: move to deterministic, similar shape to the existing `checkWrite` check) or explicitly document it as "reviewed but not guaranteed" before relying on it.
- **No ground-truth validation.** LLM-judgment findings are Claude's assessment, not verified against a compiler or a labeled vulnerability dataset. Deterministic findings are the exception — they're provable by reading the check itself, since they're plain Python logic against `xpp_parser.py`'s parsed structure.
- **Test case generation is currently disabled** (`ENABLE_TEST_GENERATION = False` in `app.py`). The agent can *write* a `SysTestCase` class as text, but nothing executes it — there's no AOS/compiler in this pipeline, and `SysTestCase` requires the real AOT (tables, EDTs, extensions) to compile and run at all. Shipping generated-but-unverified test code next to verified findings was misleading, so it's off until there's a real FnO dev/sandbox environment to actually run against. Flip the flag back to `True` once that's available — no other code changes needed.
- **Not tested against prompt injection via code comments.** A malicious comment instructing the reviewer to suppress findings hasn't been tested against this pipeline. Treat input files as untrusted if this is ever exposed beyond personal/local use.
- **X++ parsing is regex-based**, not a real AST/compiler front-end. It handles the common class/method shapes well but can miss or misparse unusual formatting, nested generic-like syntax, or macro-heavy code. The deterministic checks inherit this limitation directly, since they walk the same parsed structure.
- **Deterministic checks are heuristics too, not proofs.** A few explicitly say so in their own finding text (e.g. `forupdate`-without-write assumes the write isn't happening in a method called from here; missing-`ttsbegin` assumes the transaction isn't opened by the caller). "Deterministic" here means *reproducible*, not *infallible* — same input always gives the same output, which is a different (and weaker) guarantee than "always correct."

## Architecture note

**All four review agents (Security, Performance, Best Practice, Naming) follow the same hybrid pattern**, applied incrementally after observing run-to-run variance in reported issue counts on identical input:

1. **Deterministic checks first, in the agent's own file, no API call.** Each `_check_*` function walks structure already extracted by `parsers/xpp_parser.py` (method bodies, modifiers, attributes, fields) using plain regex/string logic — same category of code as `naming_agent.py`, which was the first to move.
2. **The LLM prompt is trimmed to explicitly exclude what the deterministic pass already covers**, and the system prompt says so directly ("do NOT re-report X — that's covered by static analysis"), so the two halves don't produce duplicate findings for the same root cause.
3. **`run(xpp)` merges both halves** and returns a single `list[dict]` — `app.py`'s `ThreadPoolExecutor` orchestration and `html_reporter.py` don't know or care which finding came from which half.

This mirrors the standard architecture used by production AI code review tools (CodeRabbit, Amazon CodeGuru, etc.): deterministic static analysis for anything pattern-matchable, LLM reserved for genuine semantic/business-logic judgment.

**How the split was validated, not just asserted:** each deterministic check set was run against a synthetic X++ file constructed to hit every rule at least once (both the true-positive and a true-negative case), and the same file was run through `_run_deterministic()` twice to confirm byte-identical output. This process caught two real bugs before they shipped — the select-in-loop check initially missed X++'s `while select` construct (only checked C-style `while(...)`), and the hardcoded-string check double-counted `Box::info(...)` calls (the plain-call regex had no guard against matching inside the `Box::`-prefixed form). Both were fixed and re-verified. The lesson generalizes: a deterministic check is not correct just because it's not an LLM — it needs the same adversarial testing discipline before being trusted.

**`base_agent.py` also moved from free-text JSON parsing to Claude's tool-use API** with an enforced JSON Schema (`tools=[...]`, `tool_choice={"type": "tool", ...}`). This removed an entire bug class rather than patching around it: earlier versions asked Claude to emit JSON as text and then parsed it with regex + a hand-rolled bracket matcher + a control-character sanitizer, because free-text generation gave no structural guarantee (a raw newline inside a string, or truncation mid-object, would break `json.loads()`). With tool-use, the API validates the arguments against the schema before they reach the code at all — `response.content` contains a `tool_use` block whose `.input` is already a parsed dict.

## Project structure

```
app.py                       # Streamlit UI, agent orchestration
agents/
  base_agent.py               # Claude API wrapper -- tool-use with enforced JSON Schema
  security_agent.py            # Hybrid: deterministic checks + LLM judgment calls
  performance_agent.py         # Hybrid: deterministic checks + LLM judgment calls
  bestpractice_agent.py        # Hybrid: deterministic checks + LLM judgment calls
  naming_agent.py               # Fully deterministic, no LLM call
  testcase_agent.py            # Disabled by default
parsers/
  xpp_parser.py                # Regex-based X++ structure extraction
reporters/
  html_reporter.py             # Self-contained HTML report generator
```
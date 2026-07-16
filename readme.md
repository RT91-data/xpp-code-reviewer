# X++ Code Review Agent

An AI-assisted static review tool for Microsoft Dynamics 365 Finance & Operations (X++) source files. Runs specialist review agents in parallel over a class and produces a filterable HTML report.

Built as a hybrid: **deterministic parsing for pattern-matchable rules, Claude for semantic/judgment-based review** — not a pure LLM pipeline. See [Known Limitations](#known-limitations) for why.

## What it checks

| Agent | Type | What it covers |
|---|---|---|
| 🛡️ Security | Claude (LLM) | SQL injection, missing `checkRead()`/`checkWrite()`, missing `[SysEntryPoint]`, cross-company guard violations, hardcoded credentials |
| ⚡ Performance | Claude (LLM) | `select` inside loops (N+1), missing `firstOnly`, unbounded selects, missing bulk insert patterns |
| ✅ Best Practices | Claude (LLM) | Empty catch blocks, missing `super()` calls, hardcoded strings, unbalanced `ttsbegin`/`ttscommit` |
| 📝 Naming Conventions | **Deterministic (Python, no LLM)** | Parameter prefix (`_paramName`), method/class casing, boolean field naming, extension class suffix |
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

- **LLM-based findings are not fully deterministic.** `temperature=0` is set, but exact output can still vary slightly run-to-run due to floating-point non-associativity in batched backend inference — this is true of any LLM API, not specific to this tool. Structural findings (missing `checkWrite`, `select` in a loop) are stable in practice; genuinely interpretive findings (e.g. "does this need a business guardrail") can vary because the underlying judgment call is itself ambiguous, not because of a bug.
- **No ground-truth validation yet.** Security/Performance/Best Practice findings are Claude's assessment, not verified against a compiler or a labeled vulnerability dataset. Naming Conventions is the exception — it's deterministic Python logic against `xpp_parser.py`'s parsed structure, so it's reproducible and provable by inspection.
- **Test case generation is currently disabled** (`ENABLE_TEST_GENERATION = False` in `app.py`). The agent can *write* a `SysTestCase` class as text, but nothing executes it — there's no AOS/compiler in this pipeline, and `SysTestCase` requires the real AOT (tables, EDTs, extensions) to compile and run at all. Shipping generated-but-unverified test code next to verified findings was misleading, so it's off until there's a real FnO dev/sandbox environment to actually run against. Flip the flag back to `True` once that's available — no other code changes needed.
- **Not tested against prompt injection via code comments.** A malicious comment instructing the reviewer to suppress findings hasn't been tested against this pipeline. Treat input files as untrusted if this is ever exposed beyond personal/local use.
- **X++ parsing is regex-based**, not a real AST/compiler front-end. It handles the common class/method shapes well but can miss or misparse unusual formatting, nested generic-like syntax, or macro-heavy code.

## Architecture note

Naming Conventions was moved from an LLM prompt to deterministic Python (`agents/naming_agent.py`, using structure already extracted by `parsers/xpp_parser.py`) after observing run-to-run variance in reported issue counts. This mirrors the standard architecture used by production AI code review tools (CodeRabbit, Amazon CodeGuru, etc.): deterministic static analysis for anything pattern-matchable, LLM reserved for genuine semantic/business-logic judgment. Security/Performance/Best Practice still have some pattern-matchable rules that could move the same way — a natural next iteration.

## Project structure

```
app.py                       # Streamlit UI, agent orchestration
agents/
  base_agent.py               # Claude API wrapper, JSON extraction/repair
  security_agent.py
  performance_agent.py
  bestpractice_agent.py
  naming_agent.py              # Deterministic, no LLM call
  testcase_agent.py            # Disabled by default
parsers/
  xpp_parser.py                # Regex-based X++ structure extraction
reporters/
  html_reporter.py             # Self-contained HTML report generator
```
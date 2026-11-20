# Software engineering Control Agent

`apps/dev_agent` implements the software-development direction of the Control
Agent: read code, change code, review it, test it, debug it, and prepare a
deployment. It is a library plus a CLI, packaged as `ai-agent-stack` and exposed
as the `dev-agent` console script.

The design is deterministic by default. Every capability below is real code
with tests; no capability is a mocked model response. Remote model planning is
an optional adapter that must be explicitly configured and can only propose
schema-validated tool steps.

## Capability mapping

| Requirement | Implementation | State |
| --- | --- | --- |
| Parse project structure and module dependencies | `scanner.scan_project`, AST only, never imports target code | implemented |
| Read functions, classes and execution flow | `scanner.module_outline`, `scanner.call_chain` | implemented |
| Generate new code / complete functions | `patching.TextEdit`, `plan`, `apply` with exact-match validation | implemented |
| Cross-file consistent changes | `patching.plan` supports a list of edits and produces one unified diff | implemented |
| Review logic, boundaries and style | `reviewer.review_paths` AST rules: bare/broad except, eval/exec, shell injection, SQL injection, mutable defaults, missing timeouts, hardcoded secrets, asserts, TODO markers | implemented |
| Review output with diffs | Auto-fixable findings carry a unified `diff`; all findings carry a suggestion | implemented |
| Generate and run tests | `testing.detect_test_plan` (pytest, npm, go), `run_tests`, coverage JSON parsing | implemented for Python; npm/go execution supported, coverage parsing is Python-only |
| Auto-repair until green | `testing.repair_loop` with bounded attempts, stops on a no-op repair | implemented framework; the repair callback is supplied by the caller |
| Coverage analysis | `coverage run` + `coverage json` when the tool is installed | implemented |
| Parse errors and stacks | `debugging.analyse_traceback`, `parse_gdb_backtrace`, root-cause hints | implemented |
| Re-run after a fix | `repair_loop`, CLI `test` | implemented |
| Conventional commits | `gitops.commit_message`, type and scope inference | implemented |
| Branches and merge conflicts | `gitops.create_branch`, `conflicted_files`, `has_conflict_markers` | implemented; conflict resolution remains manual |
| CHANGELOG | `gitops.changelog_entry`, `append_changelog` | implemented |
| Dependency and secret security | `security.scan_secrets`, `parse_pip_audit`, `parse_npm_audit` | implemented; audit execution uses the sandbox allowlist |
| SQL injection / XSS | SQL sinks in `reviewer`; XSS is left to template-specific rules | partial |
| Performance analysis | `performance.analyse_performance`: cubic loop nests, N+1 queries, serial HTTP in loops, blocking sleep in async code, whole-file reads in loops, quadratic string/list building, `SELECT *` | implemented |
| Real profiling | `profiling.profile_pytest` runs the suite under `cProfile`, persists `.dev-agent/profiles/pytest.prof` and reports cumulative hotspots; `profile_callable` profiles a library call in-process | implemented |
| Refactor / complexity advice | Loop-nest and query-shape findings from `performance` | partial |
| OpenAPI ingestion and clients | `openapi.load_spec`, Python and TypeScript generators, mock examples | implemented |
| Deployment artefacts | `deployment.generate_dockerfile`, Kubernetes manifests, CI YAML | implemented |
| Pre-flight health checks | `deployment.preflight` over node readiness, taints and allocatable capacity | implemented |
| Rollback and recovery | `rollback_steps` always returns a human-reviewed plan; the agent never executes it | implemented as guidance |
| Docs generation | `docs.render_readme`, `render_architecture` (Mermaid), `render_api_docs` | implemented |
| Automatic comments | `docs.docstring_edits` proposes signature-block insertions, applied only through the approval token | implemented |
| Pull request automation | `.github/workflows/dev-agent-review.yml` runs `tools/dev_agent_pr_review.py`, which posts or updates one review comment on changed Python files | implemented |
| Project memory | `.dev-agent/memory.json` plus a generated `AGENTS.md` | implemented |
| Tool calling | `roles` are deterministic tools; `planner.OpenAICompatiblePlanner` turns a model reply into a bounded, validated tool plan that the caller executes | implemented |
| Model planning | `planner.py`: OpenAI-compatible chat completions, retry on 429/5xx, allowlisted tools, max 8 steps, JSON Schema validation | implemented |
| Multi-agent collaboration | `collaboration.run_roles`: analyst, reviewer, security, performance and tester run concurrently; a failed role becomes data instead of aborting the run | implemented |
| Sandbox isolation | Allowlisted, no shell, secret-scrubbed environment, timeout, output truncation; optional Docker runner | implemented |
| Permission control | Root containment, protected paths, one-time approval token bound to the exact diff | implemented |
| Checkpoint and resume | `checkpoint.TaskStore` journal with phases and resume | implemented |
| Human approval for push/deploy | No push or deploy command exists; sandbox rejects them | implemented as denial |

## Usage

```bash
dev-agent scan
dev-agent scan --module pkg.core
dev-agent scan --call-chain pkg.core.load
dev-agent review
dev-agent test --coverage
dev-agent security
dev-agent performance
dev-agent profile --top 15
dev-agent collaborate
dev-agent plan --goal "stabilise the failing test suite"
dev-agent plan-comments --limit 20
dev-agent memory
dev-agent git-status
dev-agent git-commit-message
dev-agent debug crash.log
dev-agent docs --output docs/generated
dev-agent manifests --name sample --image registry/sample:1
dev-agent openapi-client --spec openapi.yaml --language python --output client.py
dev-agent discover "stabilise the failing test suite"
dev-agent task list
```

Edits are two-step:

```bash
cat > edits.json <<'JSON'
[{"path": "pkg/core.py", "old": "return read(path)", "new": "return read(path).strip()"}]
JSON
dev-agent plan-patch edits.json
dev-agent apply-patch edits.json --token <printed-token>
```

`plan-patch` validates every old fragment is unique and inside the project root,
prints a unified diff, and stores a one-time approval bound to the diff digest.
`apply-patch` consumes that token exactly once; a reused or mismatched token is
rejected.

## Safety model

- Static analysis never imports or executes the target repository.
- The command runner rejects `git push`, force resets, deploy/destroy tokens and
  any program outside the allowlist. It never uses `shell=True`.
- The environment passed to commands contains only `PATH`, `HOME`, locale,
  `TMPDIR` and `PYTHONPATH`; application secrets are not inherited.
- Output is truncated at 20,000 characters to stop token or memory blow-ups.
- Files outside the root, `.git`, `.env`, private keys and certificate bundles
  are unreadable and unwritable.
- Generated deployment artefacts are written under `.dev-agent/drafts`; publishing
  them is a separate human decision.

## Layout

| Module | Responsibility |
| --- | --- |
| `scanner.py` | AST project index, outlines, call chains |
| `context.py` | project memory and `AGENTS.md` |
| `policy.py` | path policy and one-time approvals |
| `patching.py` | multi-file edits, diffs and approved application |
| `reviewer.py` | static review findings |
| `testing.py` | discovery, execution, coverage, repair loop |
| `debugging.py` | traceback and gdb analysis |
| `gitops.py` | git inspection, commit messages, changelog |
| `security.py` | secret scan and audit parsers |
| `openapi.py` | spec parsing, client generation |
| `docs.py` | README, architecture, API documents |
| `performance.py` | static performance rules |
| `profiling.py` | cProfile run, persistence and hotspot report |
| `planner.py` | OpenAI-compatible planning adapter |
| `collaboration.py` | concurrent role execution |
| `deployment.py` | Dockerfile, Kubernetes, CI, pre-flight, rollback |
| `sandbox.py` | allowlisted command execution |
| `checkpoint.py` | durable task journal |
| `model.py` | JSON-only model output parsing and schema validation |
| `roles.py` | specialised review/test/security/deploy/analyst roles |
| `agent.py` | orchestrator |
| `cli.py` | command-line interface |

## Known limits

- Without `DEV_AGENT_LLM_BASE_URL` and `DEV_AGENT_LLM_MODEL`, no remote call is
  made. When set, planning sends the goal and a capped workspace outline to that
  gateway; setting these variables is an explicit decision to share that context.
  The API key is read from `DEV_AGENT_LLM_API_KEY` and never placed in the
  sandbox environment.
- A model plan is advisory. It cannot execute, cannot push or deploy, and every
  patch still needs a one-time approval token.
- `npm` and `go` test execution is supported, but only pytest summary and
  coverage output are parsed.
- The in-process sandbox confines commands by allowlist and environment, not by
  kernel isolation. Use the Docker runner when process-level isolation is required.
- Conflict resolution only detects markers and lists conflicted files; merging
  remains a human action.
- Static performance findings are heuristics; `dev-agent profile` provides the
  measured hotspot list before a large rewrite.
- Profiling only accepts `python -m cProfile -o <file> -m pytest -q`; the sandbox
  rejects cProfile used as a generic script runner.
- Deployment generation produces drafts, not applied manifests. Pre-flight consumes
  reported node facts; it does not query a cluster itself.
- Real hardware/BMC/Metal3 validation is out of scope for this package.

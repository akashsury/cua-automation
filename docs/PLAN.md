# Plan — Computer-Use Automation System (interface.ai Assignment A)

## Context
Take-home from interface.ai. Build a small, real end-to-end system: an LLM drives a UI that has no
API (discovery), the successful run is saved as a typed, versioned "capability" artifact, and the
artifact is replayed deterministically without the LLM, with explicit error taxonomy, guardrails,
evidence, and a real human-handoff on the same live session. Deliver as a public GitHub repo with
README.md, REPORT.md (7 fixed headings) and /evidence/.

Decisions made: **local mock credit-union back-office app** as target; **Python + Playwright +
Claude API**.

## Repo location & layout
New standalone repo at `~/git/cua-automation/` (`~/git` is a plain folder holding other projects;
it sits inside the home-dir repo that points to PythonHackathon, so this project gets its own
`git init` and its own public GitHub remote). First implementation step: copy this plan to
`~/git/cua-automation/docs/PLAN.md` for later reference.

```
cua-automation/
  README.md  REPORT.md  pyproject.toml  .env.example  config/policy.yaml
  mockbank/            # target app (FastAPI + Jinja, deliberately "legacy": tables, frameset/iframe,
                       #   no test IDs). Flow: login → member search → member detail → open
                       #   sub-account form → confirm dialog → confirmation page.
                       #   Fault injection via env/query flag: not_found, validation_error,
                       #   session_timeout, surprise_interstitial, slow_load, server_error.
  cua/
    surface/           # Surface protocol (observe/act/snapshot) — the seam for legacy/desktop
      base.py          #   Surface ABC: observe() -> Observation, act(Action), screenshot()
      playwright_web.py#   Web impl: accessibility-tree snapshot (role+name) + screenshot
    agent/loop.py      # observe→decide→act with Claude tool-use. Stop reasons: goal_met,
                       #   max_steps, timeout, stuck (same screen/action repeated N times),
                       #   policy_blocked, needs_human (risky step). Unplanned human input on the
                       #   live window while control=AGENT → pause, switch to HUMAN, record it,
                       #   re-observe on resume (not a failure).
    agent/recorder.py  # turns the successful trace into an artifact (parameterises inputs/outputs)
    artifact/schema.py # Pydantic models: Capability, Param, Output, Step, Locator, Checkpoint,
                       #   KnownCondition (business_outcome | recoverable | hard_failure)
    replay/engine.py   # deterministic executor: ordered locator fallbacks, explicit waits,
                       #   per-step postconditions, condition matcher, structured ReplayResult
    policy/guard.py    # allowlist (domains/routes, action types), risk classes, redaction
    handoff/           # control-transfer state machine + operator surface
      controller.py    #   ControlState: AGENT | PAUSED_AWAITING_HUMAN | HUMAN | RESUMING
      operator.py      #   minimal CLI operator: headed browser on SAME context, records human
                       #   actions via Playwright event listeners, "resume" signal
    evidence/logger.py # JSONL log of EVERY step (discovery + replay, success + failure): action,
                       #   locator used, timing, result, model reasoning (discovery). Screenshots at
                       #   checkpoint + on any failure/handoff; Playwright trace on failure. Redacted.
    cli.py             # `cua discover`, `cua replay`, `cua serve-mockbank`
  artifacts/           # one folder per capability, one immutable file per semver version,
                       #   e.g. lookup_member_balance/1.0.0.yaml, 1.1.0.yaml (+ schema_version field)
  evidence/            # discovery + replay logs, screenshots, failing-replay example
  tests/               # schema, redaction, policy, replay error-taxonomy against mockbank
```

## Key design choices (to defend in REPORT.md)
- **Perception:** accessibility tree (role + accessible name) as primary, screenshot as secondary
  signal to the model — works without a clean DOM and maps to desktop a11y APIs (UIA/AX).
- **Locators in artifact:** ordered list per step, e.g. `role+name` → `label text` → `nearby text
  anchor` → CSS/XPath last; recorded with a "why" note. Frame path captured for iframes.
- **Artifact schema:** `id, version, app{vendor, product, version_range}, surface_type,
  inputs (typed, sensitive flag), outputs (type + extractor), preconditions, steps[], checkpoint,
  known_conditions[], risk_level, tenant_overrides{}`, status (draft/approved). Values from the run
  are replaced with `{{param}}` placeholders; no raw PII stored.
- **Replay result contract:** `status ∈ {success, business_outcome, failed}` + `outputs`,
  `outcome_code` (e.g. MEMBER_NOT_FOUND), `recoveries_applied[]`, and on failure
  `{step_index, expected, observed, evidence_paths}`.
- **Safety:** risky steps (submit/confirm/transfer) require explicit approval → triggers handoff
  ("human decides"); anything outside allowlist is blocked before the action executes. Redactor
  masks params flagged sensitive + regex for account numbers/SSN/tokens in logs and artifacts.
- **Handoff:** same Playwright browser context is kept alive; automation pauses, headed window is
  surfaced to the operator, human actions are captured, operator types `resume`, the run re-
  observes state and continues. Intervention request written as JSON with goal, step, reason,
  screenshot.
- **Human steps vs artifact:** Discovery — human actions are captured and become artifact steps
  tagged `source: human` (or a `human_approval` gate for risky decisions); artifact stays `draft`
  until reviewed. Replay — artifacts are immutable; human actions go only into that run's evidence
  log + replay result (`status: completed_with_human`). Recurring handoffs → reviewer publishes a
  new version (new known_condition/steps).
- **Claude:** use the claude-api skill during implementation for current model id / tool-use
  pattern (Claude Sonnet/Opus 5.5).
- **Stretch (one only, if time):** agent-facing capability catalog (list + invoke by name).

## Build order
1. Mock bank app with fault injection.
2. Surface abstraction + Playwright impl.
3. Artifact schema (Pydantic) + example hand-written artifact to validate shape.
4. Replay engine + error taxonomy + tests (can be built/tested without LLM).
5. Policy guard + redaction.
6. Agent loop + recorder (needs ANTHROPIC_API_KEY from user).
7. Handoff controller + CLI operator.
8. Evidence capture; run real discovery; run replays (success, not-found, injected timeout).
9. README.md, REPORT.md (7 headings), push to public GitHub.

## Verification
- `pytest` — schema, redaction, policy, replay against mockbank for each fault mode.
- `cua serve-mockbank` then `cua discover --goal "look up member 12345 and read savings balance"
  --target http://localhost:8000` → artifact in `artifacts/`, log in `evidence/`.
- `cua replay artifacts/lookup_balance.v1.yaml --param member_id=12345` → success + balance.
- `--param member_id=99999` → `business_outcome: MEMBER_NOT_FOUND`.
- Fault `session_timeout` → recoverable re-login; `server_error` → hard failure with evidence.
- Risky "open sub-account" flow → pauses, operator takes over same window, resume completes.
- grep evidence/ and artifacts/ for test PII/password values → none found.

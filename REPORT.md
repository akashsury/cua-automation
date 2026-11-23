# REPORT — Computer-Use Automation System

## Architecture

```
 goal+params ─► DiscoveryAgent (Claude, observe→decide→act) ─trace─► Recorder ─► Artifact store (immutable semver YAML)
                         │                                                         │  + overrides/<tenant>.yaml
                         │                         ref+params ─► ReplayEngine (no LLM) ─► ReplayResult
                         ▼                                         ▼
          Guard (allowlist, irreversible gate) · Redactor · HandoffController (owner agent|human) ◄─► Operator
                         ▼
          Surface interface (observe / resolve / act / read / screenshot / human-event capture)
              └─ WebSurface (Playwright)   [DesktopSurface plugs in here]
          RunLog ─► evidence/runs/<id>/ events.jsonl · shots/ · dom/ · interventions/
```

**Discovery** (`cua/agent/loop.py`) first replays any `requires` capabilities (e.g. sign-on). Then
it loops:

1. *Observe.* The surface lists actionable elements across all frames, with role, accessible name,
   table-row label, `name` attribute and CSS path. The model gets this as a numbered text view.
2. *Decide.* Claude (`claude-opus-5-5`, effort medium, with a server-side model fallback) returns
   exactly one tool call: `click`, `fill`, `type_secret`, `select`, `extract`, `navigate`,
   `request_human` or `finish`.
3. *Act.* The guard checks the action, then the surface executes it.

The loop stops on any of these:
- `goal_met`: `finish`, with the checkpoint text verified on screen
- `max_steps` (30) or `timeout` (300 s)
- refusal, or turns with no action
- **stuck**: the same action on the same screen 3×, 3 tool errors, or 3 policy blocks. With an
  operator present, this escalates instead of failing.

If a human touches the window unannounced, the loop pauses rather than fails. Those actions are
recorded as steps marked `source: human`.

The **recorder** turns the trace into a `draft` artifact:
- Literal values matching a parameter become `{{param.x}}`. Credentials become `{{secret.x}}`; the
  model types them by *name* and never sees their values.
- Every navigating step gets a postcondition, the final screen becomes the checkpoint, and
  vendor-level known conditions are attached.
- A reviewer then runs `cua show` and `cua approve`.

**Replay** (`cua/replay/engine.py`) is plain Python + Playwright. The recorded discoveries used
4–10 model calls each; the 17 replays used **zero**.

**Key decisions and trade-offs**

- **The model sees a text list of elements, not a screenshot.** Each element is described by
  role, name, row label and frame.
  - *For:* cheap, exact, and the element number maps straight to a recordable locator.
  - *Against:* it needs an accessibility or DOM layer. Surfaces with no such layer would need a
    vision locator kind (see Heterogeneity).
- **A local mock core app as the target.** It is deliberately hostile: an iframe work area, table
  layouts, no labels, no test ids. A public site can't safely inject session expiry, server errors
  or permission denials. We control the app, so every error class is reproducible.
- **One action per model turn**, each checked by the guard before it runs.
  - *For:* every action is auditable and can be stopped.
  - *Against:* more model calls than batching actions would need.
- **Single process, synchronous, with artifacts as files in git.** Easy to review in a pull
  request and easy to diff between versions. A registry and a worker pool are deferred (Cuts).
- **The surface interface is the only DOM-aware code**, so the artifact, engine, guard and
  handoff carry no browser assumptions.

## Artifact schema

Pydantic v2 with `extra="forbid"` (`cua/artifact/schema.py`). Example:
`artifacts/mockcore.lookup_member_balance/1.0.0.yaml`. The **contract** comes first:
- `id`, semver `version`, `status` (draft/approved/deprecated)
- `app` (vendor, product, version range, surface), `entry_url`, `requires`
- `risk` (read_only/reversible/irreversible)
- typed `inputs` (type, `pattern`, `sensitive`)
- `secrets`: provider references only
- typed `outputs`

The **implementation** follows. Each step has:
- `action`
- `target`: an **ordered list of locators**
- `value`: a placeholder
- `expect`: a postcondition
- `risk`, `source` (agent/human), `timeout_ms`
- `why`: a reviewer note on locator choice

Then come `checkpoint`, `known_conditions[]` and `provenance` (run id, model, tenant, number of
human steps).

Locators, most semantic first:
- for actions: `role` (role + accessible name) → `label` → `row_label` (the input in the table row
  whose label cell reads "Member Number:", the usual legacy case with no accessible name) → `text`
  → `css`
- for reads: `table_cell` (column header + row text) and `anchor_next` (the value cell beside a
  label)

Conditions (`text_visible`, `url_matches`, `title_is`, `element_present`) are surface-neutral and
frame-scoped.

Validation rejects these at load time:
- undeclared placeholders and undeclared outputs
- duplicate step ids
- an irreversible step in a capability not declared irreversible
- a recovery missing on a recoverable condition, or present on a non-recoverable one

**Versioning.** Each version is its own immutable file; `save()` refuses to overwrite. Only the
lifecycle `status` may change in place. Callers pin a version (`id@1.0.0`) or get the latest
*approved*. `schema_version` is versioned separately.

## Determinism & error handling

Replay runs in a fixed order:

1. Validate inputs against their types and patterns. `12AB` is rejected before the UI is touched.
2. Resolve secrets.
3. Replay dependencies in the same session.
4. For each step:
   - Policy check.
   - Locator resolution. Take the **first locator that matches exactly one element**. A later one
     winning gives a `locator_degraded` warning; 0 matches is `locator_not_found`, more than one is
     `locator_ambiguous`. Never guess.
   - The action.
   - Poll the postcondition and all known conditions until the step timeout. No sleeps.
5. Verify the checkpoint.
6. Coerce outputs, e.g. `"$1,520.75"` → decimal `"1520.75"`.

| Kind | Meaning | Demo runs | Result |
|---|---|---|---|
| `business_outcome` | A correct answer the caller must handle | MEMBER_NOT_FOUND (12), PERMISSION_DENIED (22), INVALID_AMOUNT validation error (24) | `business_outcome` + `outcome.code`, exit 2 |
| `recoverable` | Transient; declared recovery with an attempt limit | SYSTEM_NOTICE → `dismiss` (14), SESSION_EXPIRED → `relogin_restart` (15) | Continues; logged in `recoveries[]` |
| `hard_failure` | Known bad state | CORE_SYSTEM_ERROR (17) | `failed / known_hard_failure` |
| *(unknown)* | Postcondition not met, no known condition matches | SUPERVISOR OVERRIDE page (18) | Escalates if an operator exists (19), else `failed / expectation_timeout` |

Every failure has:
- a `category` (one of 14)
- the step id and index
- `expected` (locators or conditions)
- `observed` (redacted text of every frame)
- `locator_attempts` (match counts)
- screenshot and DOM paths

`relogin_restart` restarts at most twice. It is refused after an irreversible step has executed
(`unsafe_to_recover`), so recovery can never double-submit.

The demo also found a genuine limitation. After a member gained a second SAVINGS share,
`table_cell(row_match="SAVINGS")` matched two rows, so replay failed `locator_ambiguous` instead of
returning one of them (run 23). The fix is a reviewed new version, e.g. one with a share-number
parameter.

## Heterogeneity & multi-tenant

**Legacy and desktop.** Only `Surface` (`cua/surface/base.py`) knows about the DOM. Steps,
conditions, the engine, the guard and the handoff deal only in role, name, label, text and frame:
- A `DesktopSurface` would back `role` locators with UI Automation (Windows) or AX (macOS), which
  expose the same role + name model.
- `row_label` / `anchor_next` would become "control to the right of, or below, the label".
- `frame` would become a window or pane path.
- Surfaces without an accessibility tree (Citrix, canvas, terminal emulators) would get extra
  locator kinds *at the end* of the list: a vision locator (OCR or template match around anchor
  text) and a field-position locator for 3270 screens. Replay stays structural where it can and
  is flagged as degraded when it can't.

**Multi-tenant.** Many credit unions run the same vendor product with different labels. Reuse
happens in three layers:
1. **App profile** (`config/apps/mockcore.yaml`): known conditions for the vendor product and
   version range, attached to every capability.
2. **Base artifact**: recorded once.
3. **Tenant override** (`overrides/<tenant>.yaml`): patches steps by id. `prepend_target` adds
   tenant-specific locators ahead of the base ones, and overrides can add conditions.

Tenant B renames "Member Number:" → "Acct/Member #:" and "Inquire" → "Search":
- The base artifact still worked on B, but through CSS fallbacks, with 2 `locator_degraded`
  warnings (run 30). That warning is the trigger to write an override.
- With `--tenant b` it ran with 0 warnings (run 31).

Credentials, policy and base URL are runtime configuration for each tenant, never stored in the
artifact.

## Escalation & handoff

`HandoffController` owns one fact: who controls the session, `agent` or `human`. Escalation can be
triggered by:
- the model's `request_human`
- stuck detection
- an irreversible action needing approval
- an unknown state during replay
- unplanned human input

`escalate()`, in order:
1. Captures a screenshot and DOM.
2. Writes `interventions/<id>.request.json`: kind, capability, reason, step, URL, redacted screen,
   evidence paths, allowed decisions, `assigned_to`.
3. Logs `control_transfer → human`.
4. Blocks until an operator decides.

The human works in **the same live Playwright session**. Their clicks, inputs and selects are
captured by an injected listener. An `agent_acting` flag excludes the agent's own events, so the
system always knows who did what.

The resolution (approve / deny / resume / abort, notes, captured actions, duration) is saved next to
the request. Control then returns to the agent, which **re-observes** the screen and doesn't assume
it is unchanged.

- **Replay 19:** the unknown screen escalated, the operator clicked *Acknowledge* in the live
  window, and replay re-checked the postcondition and succeeded.
- **Discovery 03:** Claude reached *Confirm*; the guard escalated for approval, and the operator
  approved.

In discovery, human actions become `source: human` steps in a draft for review. In replay, the
artifact is immutable, so they go only to evidence and `interventions[]`. A handoff that keeps
recurring prompts a new version.

Operators:
- `CliOperator`: a real person uses the headed window and types a decision.
- `ScriptedOperator`: a simulated human, labelled as such, used for reproducible evidence.

In production, the request JSON would go to a queue (ticket, chat or console) with a link to a live
view of the session.

## Safety

- **Allowlist** (`config/policy.yaml`), checked before *every* navigation and action, in discovery
  and replay:
  - origins and path regexes, and blocked paths (the app's `/__faults` test hook, `/app/admin`)
  - action types
  - `max_steps` and `timeout_s`

  A violation is never executed. The model sees only tool results and can't change the policy.
- **Irreversible actions** are identified by control text (confirm, transfer, delete, submit
  payment, close account), by configurable paths, or by `risk: irreversible`. They need operator
  approval or an explicit `--approve-irreversible`; otherwise the result is
  `failed / approval_required` and nothing is submitted (run 20). Drafts replay with a warning;
  `--require-approved` refuses them.

  *Why require approval, rather than block or flag:*
  - **Blocking** would make the most valuable capabilities, the ones that change something,
    unusable.
  - **Flagging** only records the action after it has already happened, which can't be undone in
    a bank system.
  - **Requiring approval** keeps those actions possible and puts a named decision before them.
    The approval is captured in the evidence.
- **Redaction** is applied in the logger *and* in the result builder, so stdout, calling agents and
  files all get the same protection:
  1. exact secret values → `[SECRET:name]`
  2. sensitive parameters and outputs → `[REDACTED:label]`
  3. patterns: SSN, Luhn-checked card numbers, 10–17-digit account numbers, emails

  Screenshots mask password inputs and SSN-shaped text, including in iframes. Playwright traces are
  disabled because they store raw input. A test scans evidence for the password and SSN, and the
  repo is scanned for keys and PII before commit.

The first demo pass found two redaction defects, both now fixed (`evidence/README.md`):
- failure details on stdout were unredacted
- the card pattern over-matched run ids

**Limits.** Pattern redaction doesn't detect names or addresses, and screenshots aren't
OCR-checked. Production needs field classification and encrypted evidence with retention limits.

## Cuts

- **Desktop / terminal surfaces:** designed, not built. Only `WebSurface` exists.
- **Operator console and queue:** the CLI and scripted operators stand in for a ticketing
  integration and a remote live view.
- **Self-repair:** on `locator_not_found`, re-discover from the failing step and propose a new
  version for review. Today a human re-records it.
- **Input inference** comes from one example (`^\d{5}$` from "12345") and is flagged in provenance;
  multiple examples would generalise it.
- **Scale:** a single process and session. There's no worker pool, session pooling or rate limiting
  against the core system, and the store is files, not a signed registry with access control.
- **Approvals** are a flag or an operator decision, not four-eyes approval tied to an identity.
- **Stretch goals:**
  - **Done: cross-tenant reuse with per-variant overrides.** One base artifact replayed on
    tenant B: degraded without the override (run 30), clean with it (run 31). Routes and values
    are canonicalised into parameters, e.g. `^/app/member/{{param.member_id}}$`.
  - **Partly done: approval gating.** draft → approved, with `--require-approved` for unattended
    runs. There's no reliability score yet.
  - **Partly done: agent-facing catalog.** `cua list` / `cua catalog-tools` render capabilities as
    LLM tool definitions, but no agent calls them end to end.

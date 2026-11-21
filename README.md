# cua-automation

A computer-use automation system for UIs that have no API:

1. **Discovery.** An LLM (Claude) drives a real browser UI towards a natural-language goal using an
   observe → decide → act loop.
2. **Artifact.** The successful run becomes a typed, versioned, reviewable **capability**
   (`artifacts/<id>/<semver>.yaml`) with inputs, outputs, steps, robust locators, a checkpoint and
   known error conditions.
3. **Replay.** The capability is replayed **deterministically, without any LLM**. Every run returns
   a structured result: `success`, `business_outcome` (a legitimate answer, e.g. member not found)
   or `failed` (a typed failure category, with screenshot and DOM evidence).
4. **Handoff.** When automation is stuck, meets an unknown screen, or reaches an irreversible
   action, it escalates to a human. The human works in the **same live browser session** and then
   hands control back.
5. **Guardrails.** An origin/path/action allowlist, approval for irreversible actions, and redaction
   of credentials and PII in every log, artifact and screenshot.

The target is `mockbank/`, a deliberately "legacy" credit-union core back-office app. It uses
table layouts, an iframe work area, inputs with no labels or accessible names and opaque field
names, and has fault injection. The design write-up is in **[REPORT.md](REPORT.md)**, and the
recorded runs are in **[evidence/](evidence/README.md)**.

## Setup

Requires Python ≥ 3.11 (tested on 3.13, macOS).

```bash
git clone <this repo> && cd cua-automation
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env
```

### Keys and credentials

`.env` is git-ignored. It holds:

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | **live discovery only** | Replay, tests and offline discovery never call a model. |
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | sign-on to the mock app | Fictitious training credentials. They're read through the policy's secret allowlist (`config/policy.yaml`), and only ever appear in artifacts as `{{secret.*}}`. |

## Run the tests (no key needed)

```bash
pytest -q          # 26 tests: schema/store, policy + redaction, replay error taxonomy, offline discovery
```

The tests start their own mock app on port 8799 and write to temporary directories.

## Run it yourself

Start the target app in one terminal and leave it running:

```bash
cua mockbank                 # http://127.0.0.1:8765  (tenant "a"; use --tenant b for the relabelled tenant)
```

### Replay the committed artifacts (offline, no key)

```bash
cua list                                                           # agent-facing catalog
cua show mockcore.lookup_member_balance                            # human review of an artifact
cua replay mockcore.lookup_member_balance --param member_id=12345  # -> success, savings_balance 1520.75
cua replay mockcore.lookup_member_balance --param member_id=99999  # -> business_outcome MEMBER_NOT_FOUND
cua replay mockcore.lookup_member_balance --param member_id=12345 --inject-fault server_error
                                                                   # -> failed known_hard_failure + evidence
cua replay mockcore.lookup_member_balance --param member_id=12345 --inject-fault session_timeout
                                                                   # -> recovered (relogin_restart), success
cua replay mockcore.open_share --param member_id=12345 --param share_type=SAVINGS \
    --param nickname=TRAVEL --param deposit=40.00                  # -> failed approval_required
```

Exit codes: `0` success, `2` business_outcome, `1` failed. Each run prints its result JSON and writes
`evidence/runs/<run_id>/`.

Other fault modes for `--inject-fault`: `interstitial`, `slow`, `permission_denied` and
`supervisor_prompt`. The last one is an unknown screen that escalates to a human.

### Human takeover in the live session

```bash
cua replay mockcore.lookup_member_balance --param member_id=12345 \
    --inject-fault supervisor_prompt --operator cli --headed
```

When replay hits the unknown *SUPERVISOR OVERRIDE* page, it pauses and prints an intervention
request. Then:
1. Click **Acknowledge** in the browser window that opened.
2. Type `resume` in the terminal.

Replay re-checks the screen and finishes. Your click is recorded in
`interventions/*.resolution.json`. To simulate the human instead, use
`--operator scripted:examples/operator/acknowledge.json`.

### Discovery

**Live (needs `ANTHROPIC_API_KEY`).** These are the exact commands behind the recorded evidence:

```bash
cua discover --id mockcore.sign_on --entry /login \
  --goal "Sign on to the core system with the service account and reach the Member Inquiry screen"
cua approve mockcore.sign_on

cua discover --id mockcore.lookup_member_balance --requires mockcore.sign_on \
  --param member_id=12345 --outputs "savings_balance (decimal)" \
  --goal "Look up member 12345 and read their current savings balance"
```

- Re-running a discovery against an existing id writes the **next version** (`1.1.0`). Artifacts
  are immutable.
- Add `--headed --operator cli` to watch the run, and to approve irreversible steps yourself.
- If you click in the window during discovery, the run pauses. Your actions are recorded as
  `source: human` steps.

**Offline (no key).** A scripted model replays recorded tool calls through the same loop, policy
and recorder:

```bash
export CUA_ARTIFACTS_DIR=/tmp/cua-artifacts CUA_EVIDENCE_DIR=/tmp/cua-evidence   # keep the repo clean
cua discover --id mockcore.sign_on --entry /login --model scripted:examples/offline/sign_on.model.json \
  --goal "Sign on and reach the Member Inquiry screen"
```

### Full demo (regenerates `evidence/`)

```bash
./scripts/demo.sh                    # starts the mock app; 3 live discoveries + 16 replays (~3 min, < $1)
SKIP_DISCOVERY=1 ./scripts/demo.sh   # replays only: no key, no cost
```

## Layout

```
mockbank/            target app (FastAPI) with tenants a/b and fault injection (/__faults: blocked by policy)
cua/agent/           discovery loop (loop.py), Claude client (model.py), trace -> artifact (recorder.py)
cua/artifact/        schema (Pydantic) and immutable versioned YAML store with tenant overrides
cua/replay/          deterministic engine + condition matcher
cua/surface/         Surface interface + Playwright web implementation (the seam for desktop surfaces)
cua/policy/          allowlist / irreversible-action guard, redaction
cua/handoff/         control-transfer controller, CLI and scripted operators
cua/evidence/        structured run logger (JSONL, screenshots, DOM)
config/              policy.yaml, app profile with vendor-level known conditions
artifacts/           saved capabilities (+ overrides/<tenant>.yaml)
evidence/            recorded demo runs, see evidence/README.md
docs/PLAN.md         the original build plan
```

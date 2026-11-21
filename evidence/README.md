# Evidence

Everything here was produced by `scripts/demo.sh` on 2026-11-20 against the local mock core app
(`mockbank/`). **Discovery runs 01–03 are genuine Claude runs** (`claude-opus-5-5`, effort `medium`).
**Replays never call a model.**

```
evidence/
  artifacts/        copies of the saved capability artifacts (same files as /artifacts)
  demo/NN-*.json    stdout of each CLI command (the caller-facing result contract)
  runs/<run_id>/    one folder per run:
      events.jsonl    structured log, one JSON event per line (every step, policy check, model turn)
      result.json     final result
      shots/*.png     screenshots: checkpoint, every failure, every handoff (password + SSN masked)
      dom/*.html      redacted DOM snapshot next to every failure/handoff screenshot
      interventions/  *.request.json / *.resolution.json for every human handoff
      artifact.yaml   (discovery only) the artifact this run produced
```

## Index

| Run | Status | Outcome / failure | Outputs | Evidence |
|---|---|---|---|---|
| 01-discover-sign-on | `success` | 4 model calls, 13.5k tokens | — | [discovery-20261120-164351-acec](runs/discovery-20261120-164351-acec/) |
| 02-discover-lookup | `success` | 4 model calls, 13.7k tokens | — | [discovery-20261120-164407-ab85](runs/discovery-20261120-164407-ab85/) |
| 03-discover-open-share | `success` | 10 model calls, 51.3k tokens; **paused for human approval before `Confirm`** | — | [discovery-20261120-164418-d854](runs/discovery-20261120-164418-d854/) |
| 10-replay-success | `success` | | `{"savings_balance": "1520.75"}` | [replay-20261120-165320-ebf7](runs/replay-20261120-165320-ebf7/) |
| 11-replay-other-member | `success` | | `{"savings_balance": "88.00"}` | [replay-20261120-165323-06cd](runs/replay-20261120-165323-06cd/) |
| 12-replay-not-found | `business_outcome` | MEMBER_NOT_FOUND | | [replay-20261120-165324-d378](runs/replay-20261120-165324-d378/) |
| 13-replay-invalid-input | `failed` | invalid_input (rejected before touching the UI) | | [replay-20261120-165326-345d](runs/replay-20261120-165326-345d/) |
| 14-replay-interstitial | `success` | recovered: SYSTEM_NOTICE → dismiss | `{"savings_balance": "1520.75"}` | [replay-20261120-165326-dc92](runs/replay-20261120-165326-dc92/) |
| 15-replay-session-expired | `success` | recovered: SESSION_EXPIRED → relogin_restart | `{"savings_balance": "1520.75"}` | [replay-20261120-165329-91f8](runs/replay-20261120-165329-91f8/) |
| 16-replay-slow | `success` | explicit waits absorbed a 3 s page | `{"savings_balance": "1520.75"}` | [replay-20261120-165331-bf5a](runs/replay-20261120-165331-bf5a/) |
| **17-replay-server-error** | `failed` | **known_hard_failure CORE_SYSTEM_ERROR** at `click_inquire`, with screenshot + DOM | | [replay-20261120-165336-80db](runs/replay-20261120-165336-80db/) |
| **18-replay-unknown-no-operator** | `failed` | **expectation_timeout**: unknown SUPERVISOR OVERRIDE page, no operator available | | [replay-20261120-165337-2b57](runs/replay-20261120-165337-2b57/) |
| 19-replay-human-takeover | `success` | same unknown page → escalated → human clicked *Acknowledge* in the live session → resumed | `{"savings_balance": "1520.75"}` | [replay-20261120-165349-7cc8](runs/replay-20261120-165349-7cc8/) |
| 20-replay-open-share-no-approval | `failed` | approval_required (irreversible `Confirm` not executed) | | [replay-20261120-165402-4733](runs/replay-20261120-165402-4733/) |
| 21-replay-open-share-approved | `success` | `--approve-irreversible` | `{"confirmation_no": "C-70001"}` | [replay-20261120-165405-5088](runs/replay-20261120-165405-5088/) |
| 22-replay-open-share-denied-perm | `business_outcome` | PERMISSION_DENIED | | [replay-20261120-165408-2a89](runs/replay-20261120-165408-2a89/) |
| **23-replay-ambiguous-after-change** | `failed` | **locator_ambiguous**: member now has two SAVINGS rows; replay refuses to guess | | [replay-20261120-165409-6bf5](runs/replay-20261120-165409-6bf5/) |
| 30-tenant-b-base-artifact | `success` | tenant B labels differ → 2 × `locator_degraded` (CSS fallback used) | `{"savings_balance": "1520.75"}` | [replay-20261120-165423-b0f5](runs/replay-20261120-165423-b0f5/) |
| 31-tenant-b-with-override | `success` | `--tenant b` override → 0 warnings | `{"savings_balance": "1520.75"}` | [replay-20261120-165424-bca1](runs/replay-20261120-165424-bca1/) |

Good places to start:
- `runs/discovery-20261120-164418-d854/events.jsonl` shows the model loop, the policy gate
  and the approval handoff.
- `runs/replay-20261120-165336-80db/` is a hard failure with its screenshot and DOM.
- `runs/replay-20261120-165349-7cc8/interventions/` shows a human takeover.

## Notes

- All member data is fictitious. The mock "SSN" values are redacted as `[REDACTED:ssn]` in
  logs and DOM, and masked in screenshots. Credentials only ever appear as `{{secret.*}}`
  or `[SECRET:*]`.
- The first demo pass found two redaction defects, and both were fixed before these replays
  were recorded:
  - CLI stdout printed unredacted failure diagnostics.
  - The card-number pattern also matched timestamp-style run ids.

  The discovery runs (01–03) were deliberately not re-recorded, so as not to repeat paid model
  calls. As a result, their logs still show a few run ids over-redacted as `[REDACTED:card]`.
  That is a false positive, not a leak.
- The first pass also showed, unplanned, the failure that replay 23 now shows on purpose:
  discovery 03 really opened a second SAVINGS share for member 23456, so a later "savings
  balance" lookup was ambiguous. The demo now resets the core data between discovery and replay.
- One post-hoc edit was made to the discovery logs. In the `artifact_written` events, the
  absolute artifact path, which included the author's home directory, was rewritten to be
  repo-relative. The code now logs relative paths. Nothing else in `evidence/` was edited by hand.

"""Replay against the live mock app (headless browser, no LLM)."""
import json
from pathlib import Path

import pytest

from cua import runtime

from .conftest import load_fixture

SIGN = load_fixture("sign_on.yaml", known=False)
DEPS = {SIGN.id: SIGN}


def run(server, policy_path, cap, params, **kw):
    return runtime.replay(cap, params, DEPS, base_url=server, policy_path=policy_path, verbose=False, **kw)


def test_success_with_degraded_locator_warning(reset, policy_path):
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    assert r.status == "success" and r.outputs == {"savings_balance": "1520.75"}
    assert any("locator_degraded" in w for w in r.warnings)  # role locator has no name on legacy input


def test_not_found_is_business_outcome(reset, policy_path):
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "99999"})
    assert r.status == "business_outcome" and r.outcome.code == "MEMBER_NOT_FOUND" and r.failure is None


def test_invalid_input_rejected_before_touching_ui(reset, policy_path):
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12"})
    assert r.status == "failed" and r.failure.category == "invalid_input" and r.steps_completed == 0


@pytest.mark.parametrize("fault,code,action", [("interstitial", "SYSTEM_NOTICE", "dismiss"),
                                               ("session_timeout", "SESSION_EXPIRED", "relogin_restart")])
def test_recoverable_conditions(reset, policy_path, fault, code, action):
    runtime.inject_fault(reset, fault)
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    assert r.status == "success" and r.outputs["savings_balance"] == "1520.75"
    assert r.recoveries[0]["code"] == code and r.recoveries[0]["action"] == action


def test_hard_failure_has_debuggable_evidence(reset, policy_path):
    runtime.inject_fault(reset, "server_error")
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    f = r.failure
    assert r.status == "failed" and f.category == "known_hard_failure" and f.code == "CORE_SYSTEM_ERROR"
    assert f.step_id == "inquire" and f.observed and f.url
    root = Path(runtime.os.environ["CUA_EVIDENCE_DIR"]) if hasattr(runtime, "os") else None
    run_dir = next(Path(p) for p in Path(__import__("os").environ["CUA_EVIDENCE_DIR"]).glob(f"{r.run_id}"))
    assert list(run_dir.glob("shots/*.png")) and list(run_dir.glob("dom/*.html"))


def test_irreversible_step_needs_approval(reset, policy_path):
    cap = load_fixture("open_share.yaml")
    params = {"member_id": "12345", "share_type": "SAVINGS", "nickname": "vacation", "deposit": "25"}
    r = run(reset, policy_path, cap, params)
    assert r.status == "failed" and r.failure.category == "approval_required" and r.failure.step_id == "confirm"
    r = run(reset, policy_path, cap, params, approve_irreversible=True)
    assert r.status == "success" and r.outputs["confirmation_no"].startswith("C-")


def test_permission_denied_is_business_outcome(reset, policy_path):
    runtime.inject_fault(reset, "permission_denied")
    params = {"member_id": "12345", "share_type": "SAVINGS", "nickname": "x", "deposit": "25"}
    r = run(reset, policy_path, load_fixture("open_share.yaml"), params, approve_irreversible=True)
    assert r.status == "business_outcome" and r.outcome.code == "PERMISSION_DENIED"


def test_operator_approval_handoff(reset, policy_path, tmp_path):
    script = tmp_path / "op.json"
    script.write_text(json.dumps([{"kind": "approval", "decision": "approve", "notes": "ok"}]))
    params = {"member_id": "23456", "share_type": "CERTIFICATE", "nickname": "cd", "deposit": "100"}
    r = run(reset, policy_path, load_fixture("open_share.yaml"), params, operator=f"scripted:{script}")
    assert r.status == "success" and r.interventions[0]["decision"] == "approve"


def test_unknown_state_escalates_and_human_takes_over_live_session(reset, policy_path, tmp_path):
    runtime.inject_fault(reset, "supervisor_prompt")
    script = tmp_path / "op.json"
    script.write_text(json.dumps([{"kind": "replay_failure", "decision": "resume",
                                   "actions": [{"click": [{"by": "role", "role": "button", "name": "Acknowledge"}]}]}]))
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"}, operator=f"scripted:{script}")
    assert r.status == "success" and r.outputs["savings_balance"] == "1520.75"
    iv = r.interventions[0]
    assert iv["decision"] == "resume" and iv["human_actions"][0]["element"]["name"] == "Acknowledge"


def test_unknown_state_without_operator_fails_with_detail(reset, policy_path):
    runtime.inject_fault(reset, "supervisor_prompt")
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    assert r.status == "failed" and r.failure.category == "expectation_timeout"
    assert "SUPERVISOR OVERRIDE" in json.dumps(r.failure.observed)


def test_evidence_contains_no_secrets_or_ssn(reset, policy_path):
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    run_dir = Path(__import__("os").environ["CUA_EVIDENCE_DIR"]) / r.run_id
    blob = "".join(p.read_text(errors="ignore") for p in run_dir.rglob("*") if p.suffix in {".jsonl", ".json", ".html"})
    assert "training-only-pw" not in blob and "900-12-3456" not in blob


def test_ambiguous_read_refuses_to_guess_and_caller_result_is_redacted(reset, policy_path):
    # Give member 12345 a second SAVINGS share, then ask for "the" savings balance.
    opened = run(reset, policy_path, load_fixture("open_share.yaml"),
                 {"member_id": "12345", "share_type": "SAVINGS", "nickname": "TRAVEL", "deposit": "40.00"},
                 approve_irreversible=True)
    assert opened.status == "success"
    r = run(reset, policy_path, load_fixture("lookup.yaml"), {"member_id": "12345"})
    assert r.status == "failed" and r.failure.category == "locator_ambiguous" and r.outputs == {}
    blob = r.model_dump_json()  # what a calling agent / CLI stdout receives
    assert "MEMBER DETAIL" in blob and "900-12-3456" not in blob and "[REDACTED:ssn]" in blob

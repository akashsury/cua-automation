"""Discovery loop -> recorder -> artifact -> replay, with a scripted model (no network)."""
import json
import os
from pathlib import Path

from cua import runtime
from cua.agent.loop import DiscoveryAgent
from cua.agent.model import ScriptedModel
from cua.agent.recorder import build_capability
from cua.artifact import store
from cua.replay.engine import ReplayEngine

EX = Path(__file__).parents[1] / "examples" / "offline"


def discover(server, policy_path, model_file, goal, cap_id, params, entry="/", requires=(), operator=None):
    rt = runtime.build("discovery", goal, base_url=server, policy_path=policy_path, verbose=False, operator=operator)
    try:
        model = ScriptedModel.from_file(EX / model_file) if isinstance(model_file, str) else ScriptedModel(model_file)
        res = DiscoveryAgent(rt, model, goal, params, entry_url=entry, requires=list(requires)).run()
        assert res.status == "success", res.stop_reason
        cap = build_capability(cap_id=cap_id, version=store.next_version(cap_id), goal=goal, result=res, params=params,
                               policy=rt.guard.policy, app_profile=runtime.load_app_profile(),
                               requires=[c.id for c in requires], run_id=rt.log.run_id, model="scripted")
        store.save(cap)
        return cap, rt.log.dir
    finally:
        rt.close()


def test_discover_record_replay_roundtrip(reset, policy_path):
    sign, _ = discover(reset, policy_path, "sign_on.model.json", "Sign on", "mockcore.sign_on", {}, entry="/login")
    assert set(sign.secrets) == {"mockbank_user", "mockbank_password"}
    look, run_dir = discover(reset, policy_path, "lookup.model.json", "Look up member 12345 savings balance",
                             "mockcore.lookup_member_balance", {"member_id": "12345"}, requires=[sign])
    text = store.to_yaml(look)
    assert "{{param.member_id}}" in text and "12345" not in text.replace("{{param.member_id}}", "")
    fill = look.steps[0]
    assert fill.target[0].by == "row_label" and fill.value == "{{param.member_id}}"
    assert look.steps[1].expect is not None
    # the secret value never reaches artifact or evidence
    sign_yaml = store.to_yaml(sign)
    assert "training-only-pw" not in sign_yaml
    blob = "".join(p.read_text(errors="ignore") for p in Path(os.environ["CUA_EVIDENCE_DIR"]).rglob("*.jsonl"))
    assert "training-only-pw" not in blob
    # replay the freshly recorded capability with a different input
    res = runtime.replay(look, {"member_id": "23456"}, {sign.id: sign}, base_url=reset, policy_path=policy_path, verbose=False)
    assert res.status == "success" and res.outputs == {"savings_balance": "88.00"}


def test_human_steps_from_discovery_handoff_are_recorded(reset, policy_path, tmp_path):
    """Agent asks for help; the operator performs the inquiry click in the live session;
    the recorded artifact contains that step with source=human and replays."""
    sign, _ = discover(reset, policy_path, "sign_on.model.json", "Sign on", "mockcore.sign_on", {}, entry="/login")
    op = tmp_path / "op.json"
    op.write_text(json.dumps([{"kind": "agent_requested", "decision": "resume",
                               "actions": [{"click": [{"by": "role", "role": "button", "name": "Inquire", "frame": "workarea"}]}]}]))
    calls = [
        {"tool": "fill", "input": {"element": 1, "param": "member_id", "reason": "member"}},
        {"tool": "request_human", "input": {"reason": "not sure which button runs the inquiry"}},
        {"tool": "extract", "input": {"output": "savings_balance", "type": "decimal", "frame": "workarea",
                                      "locator": {"kind": "table_cell", "column": "BALANCE", "row_match": "SAVINGS"}, "reason": "read"}},
        {"tool": "finish", "input": {"status": "success", "summary": "done", "checkpoint_text": "MEMBER DETAIL"}},
    ]
    cap, _ = discover(reset, policy_path, calls, "Look up member balance", "mockcore.lookup_h", {"member_id": "12345"},
                      requires=[sign], operator=f"scripted:{op}")
    human = [s for s in cap.steps if s.source == "human"]
    assert len(human) == 1 and human[0].action == "click" and cap.provenance.human_steps == 1
    assert human[0].expect is not None  # post-state captured at resume
    res = runtime.replay(cap, {"member_id": "23456"}, {sign.id: sign}, base_url=reset, policy_path=policy_path, verbose=False)
    assert res.status == "success" and res.outputs == {"savings_balance": "88.00"}

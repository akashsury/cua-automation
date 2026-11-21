"""Command line entry point.

  cua mockbank                       run the proxy target app on :8765
  cua discover --goal ... --id ...   LLM-driven discovery -> capability artifact
  cua replay <ref> --param k=v       deterministic replay (no LLM) -> structured result
  cua show <ref>                     human-readable review of an artifact
  cua approve <ref>                  draft -> approved
  cua list                           catalog of capabilities (the agent-facing view)
  cua catalog-tools                  capabilities rendered as LLM tool definitions
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua import runtime
from cua.artifact import store

ROOT = Path(__file__).resolve().parents[1]


def _params(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"--param expects key=value, got {it!r}")
        out[k] = v
    return out


def cmd_mockbank(a) -> int:
    import os

    import uvicorn

    os.environ["MOCKBANK_TENANT"] = a.tenant
    uvicorn.run("mockbank.app:app", host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def cmd_discover(a) -> int:
    from cua.agent.loop import DiscoveryAgent
    from cua.agent.model import AnthropicModel, ScriptedModel
    from cua.agent.recorder import build_capability

    params = _params(a.param)
    requires, direct = [], []
    for ref in a.requires or []:
        cap, deps = runtime.load_with_deps(ref, tenant=a.tenant)
        requires += [d for d in deps.values() if d not in requires] + [cap]
        direct.append(cap.id)
    model = ScriptedModel.from_file(a.model.split(":", 1)[1]) if a.model.startswith("scripted:") else AnthropicModel(a.model, a.effort)
    rt = runtime.build("discovery", a.goal, base_url=a.base_url, headed=a.headed, operator=a.operator)
    if a.inject_fault:
        runtime.inject_fault(a.base_url, a.inject_fault)
    try:
        agent = DiscoveryAgent(rt, model, a.goal, params, entry_url=a.entry, requires=requires,
                               outputs_hint=a.outputs or "", max_steps=a.max_steps)
        result = agent.run()
        summary = {"run_id": rt.log.run_id, "status": result.status, "stop_reason": result.stop_reason,
                   "steps": result.steps_taken, "usage": result.usage, "evidence_dir": rt.log.rel(rt.log.dir)}
        if result.status == "success":
            version = store.next_version(a.id)
            cap = build_capability(cap_id=a.id, version=version, goal=a.goal, result=result, params=params,
                                   policy=rt.guard.policy, app_profile=runtime.load_app_profile(),
                                   requires=direct,
                                   run_id=rt.log.run_id, model=model.name, tenant=a.tenant, title=a.title)
            path = store.save(cap)
            shutil.copy(path, rt.log.dir / "artifact.yaml")
            summary["artifact"] = str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
            rt.log.event("artifact_written", ref=cap.ref, path=summary["artifact"])  # no absolute/home paths in logs
        rt.log.write_json("result.json", summary)
    finally:
        rt.close()
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "success" else 1


def cmd_replay(a) -> int:
    cap, deps = runtime.load_with_deps(a.ref, tenant=a.tenant)
    if a.require_approved and cap.status != "approved":
        print(json.dumps({"status": "failed", "failure": {"category": "policy_violation",
                                                          "message": f"{cap.ref} is {cap.status}"}}), file=sys.stdout)
        return 1
    if a.inject_fault:
        runtime.inject_fault(a.base_url, a.inject_fault, a.fault_count)
    res = runtime.replay(cap, _params(a.param), deps, approve_irreversible=a.approve_irreversible,
                         base_url=a.base_url, headed=a.headed, operator=a.operator, verbose=not a.quiet)
    print(res.model_dump_json(indent=2, exclude_none=True))
    return {"success": 0, "business_outcome": 2}.get(res.status, 1)


def cmd_show(a) -> int:
    cap, path = store.load(a.ref, tenant=a.tenant)
    print(f"{cap.ref}  [{cap.status}]  risk={cap.risk}  surface={cap.app.surface}\n  {cap.title}\n  {cap.description}")
    print(f"  file: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"  app : {cap.app.vendor} {cap.app.product} {cap.app.version_range}   entry: {cap.entry_url}   requires: {cap.requires}")
    print("  inputs :", {k: v.model_dump(exclude_none=True, exclude_defaults=True) or v.type for k, v in cap.inputs.items()})
    print("  outputs:", {k: v.type for k, v in cap.outputs.items()})
    print("  secrets:", list(cap.secrets))
    print("  steps:")
    for i, s in enumerate(cap.steps, 1):
        flag = (" !IRREVERSIBLE" if s.risk == "irreversible" else "") + (" (human)" if s.source == "human" else "")
        print(f"   {i}. {s.action:<7} {s.id:<34} value={s.value or s.output or ''}{flag}")
        for j, t in enumerate(s.target, 1):
            print(f"        locator#{j}: {t.model_dump(exclude_none=True)}")
        if s.expect:
            print(f"        expect: {[c.model_dump(exclude_none=True) for c in s.expect.all_of]}")
    print("  checkpoint:", [c.model_dump(exclude_none=True) for c in cap.checkpoint.all_of])
    print("  known conditions:")
    for k in cap.known_conditions:
        print(f"   - {k.code:<22} {k.kind:<17} {k.recovery.action if k.recovery else ''}")
    print(f"  provenance: {cap.provenance.model_dump(exclude_none=True)}")
    return 0


def cmd_approve(a) -> int:
    path = store.resolve_path(a.ref)
    store.set_status(path, "approved")
    print(f"approved {path}")
    return 0


def _catalog() -> list[dict]:
    out = []
    for d in sorted(p for p in store.artifacts_root().iterdir() if p.is_dir()):
        try:
            cap, path = store.load(d.name)
        except FileNotFoundError:
            continue
        out.append({"capability": cap.ref, "status": cap.status, "risk": cap.risk, "title": cap.title,
                    "inputs": {k: v.type for k, v in cap.inputs.items()},
                    "outputs": {k: v.type for k, v in cap.outputs.items()}, "requires": cap.requires})
    return out


def cmd_list(a) -> int:
    print(json.dumps(_catalog(), indent=2))
    return 0


def cmd_catalog_tools(a) -> int:
    """Render capabilities as tool definitions an AI agent could call (stretch: agent-facing interface)."""
    tools = []
    for entry in _catalog():
        cap, _ = store.load(entry["capability"])
        props = {k: {"type": "string", "description": v.description + (f" (pattern {v.pattern})" if v.pattern else "")}
                 for k, v in cap.inputs.items()}
        tools.append({"name": cap.id.replace(".", "__"), "description":
                      f"{cap.title}. Returns {list(cap.outputs) or 'nothing'}; risk={cap.risk}. "
                      f"Business outcomes: {[k.code for k in cap.known_conditions if k.kind == 'business_outcome']}",
                      "input_schema": {"type": "object", "properties": props, "required": list(props),
                                       "additionalProperties": False}})
    print(json.dumps(tools, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mockbank", help="run the proxy target app")
    m.add_argument("--port", type=int, default=8765)
    m.add_argument("--tenant", default="a", choices=["a", "b"])
    m.set_defaults(fn=cmd_mockbank)

    def common(sp):
        sp.add_argument("--base-url", default=runtime.DEFAULT_BASE_URL)
        sp.add_argument("--tenant", default=None, help="apply artifacts/<id>/overrides/<tenant>.yaml")
        sp.add_argument("--operator", default="none", help="none | cli | scripted:<file.json>")
        sp.add_argument("--headed", action="store_true", help="show the browser (needed for a real human handoff)")
        sp.add_argument("--inject-fault", default=None,
                        choices=["session_timeout", "interstitial", "slow", "server_error", "permission_denied", "supervisor_prompt"],
                        help="test hook on the mock app (fires on the next matching request)")

    d = sub.add_parser("discover", help="LLM-driven discovery run")
    d.add_argument("--goal", required=True)
    d.add_argument("--id", required=True, help="capability id, e.g. mockcore.lookup_member_balance")
    d.add_argument("--title")
    d.add_argument("--param", action="append", help="input parameter key=value (repeatable)")
    d.add_argument("--requires", action="append", help="capability ref to replay first (e.g. sign-on)")
    d.add_argument("--entry", default="/")
    d.add_argument("--outputs", help='hint, e.g. "savings_balance (decimal)"')
    d.add_argument("--model", default="claude-opus-5-5", help="model id, or scripted:<file.json> for offline runs")
    d.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    d.add_argument("--max-steps", type=int, default=None)
    common(d)
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of a capability")
    r.add_argument("ref", help="path, id or id@version")
    r.add_argument("--param", action="append")
    r.add_argument("--approve-irreversible", action="store_true",
                   help="caller pre-authorises irreversible steps for this invocation")
    r.add_argument("--require-approved", action="store_true", help="refuse draft artifacts")
    r.add_argument("--fault-count", type=int, default=1)
    r.add_argument("--quiet", action="store_true")
    common(r)
    r.set_defaults(fn=cmd_replay)

    for name, fn in [("show", cmd_show), ("approve", cmd_approve)]:
        sp = sub.add_parser(name)
        sp.add_argument("ref")
        if name == "show":
            sp.add_argument("--tenant", default=None)
        sp.set_defaults(fn=fn)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("catalog-tools").set_defaults(fn=cmd_catalog_tools)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

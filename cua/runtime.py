"""Wiring: builds surface + guard + redactor + evidence log + handoff for one run."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from cua.artifact import store
from cua.artifact.schema import Capability, KnownCondition
from cua.evidence.logger import DEFAULT_ROOT as EVIDENCE_ROOT
from cua.evidence.logger import RunLog
from cua.handoff.controller import HandoffController, Operator
from cua.handoff.operator import CliOperator, ScriptedOperator
from cua.policy.guard import Guard, load_policy
from cua.policy.redact import Redactor
from cua.replay.engine import ReplayEngine, ReplayResult
from cua.surface.playwright_web import WebSurface

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "policy.yaml"
DEFAULT_APP_PROFILE = ROOT / "config" / "apps" / "mockcore.yaml"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def load_app_profile(path: Path = DEFAULT_APP_PROFILE) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    data["known_conditions"] = [KnownCondition.model_validate(k) for k in data.get("known_conditions", [])]
    return data


def make_operator(spec: str | None) -> Operator | None:
    """none | cli | scripted:<path.json>"""
    if not spec or spec == "none":
        return None
    if spec == "cli":
        return CliOperator()
    if spec.startswith("scripted:"):
        return ScriptedOperator.from_file(spec.split(":", 1)[1])
    raise ValueError(f"unknown operator {spec!r}")


def inject_fault(base_url: str, mode: str | None, count: int = 1) -> None:
    """Test hook on the mock app (never reachable by the agent: blocked by policy)."""
    req = urllib.request.Request(f"{base_url}/__faults", data=json.dumps({"mode": mode, "count": count}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


@dataclass
class Runtime:
    surface: WebSurface
    guard: Guard
    redactor: Redactor
    log: RunLog
    handoff: HandoffController

    def close(self) -> None:
        self.log.close()
        self.surface.close()


def build(kind: str, subject: str, *, base_url: str = DEFAULT_BASE_URL, headed: bool = False,
          policy_path: Path = DEFAULT_POLICY, operator: str | None = None, verbose: bool = True,
          evidence_root: Path | None = None) -> Runtime:
    import os
    evidence_root = evidence_root or Path(os.environ.get("CUA_EVIDENCE_DIR") or EVIDENCE_ROOT)
    redactor = Redactor()
    guard = Guard(load_policy(policy_path))
    log = RunLog(kind, redactor, root=evidence_root, verbose=verbose)
    surface = WebSurface(base_url, headed=headed)
    handoff = HandoffController(surface, log, make_operator(operator), subject)
    return Runtime(surface, guard, redactor, log, handoff)


def load_with_deps(ref: str, tenant: str | None = None, root: Path | None = None) -> tuple[Capability, dict[str, Capability]]:
    cap, _ = store.load(ref, tenant=tenant, root=root)
    deps: dict[str, Capability] = {}
    todo = list(cap.requires)
    while todo:
        dep_id = todo.pop()
        if dep_id in deps:
            continue
        dep, _ = store.load(dep_id, tenant=tenant, root=root)
        deps[dep_id] = dep
        todo += dep.requires
    return cap, deps


def replay(cap: Capability, params: dict[str, str], deps: dict[str, Capability] | None = None, *,
           approve_irreversible: bool = False, **build_kw) -> ReplayResult:
    rt = build("replay", cap.ref, **build_kw)
    try:
        engine = ReplayEngine(rt.surface, rt.guard, rt.log, rt.redactor, rt.handoff,
                              approve_irreversible=approve_irreversible, dependencies=deps or {})
        return engine.run(cap, params)
    finally:
        rt.close()

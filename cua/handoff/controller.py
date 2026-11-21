"""Control-transfer model for human-in-the-loop handoff on the *same live session*.

Exactly one party owns the session at any time:

    AGENT ──escalate()──▶ HUMAN ──operator signals resume/approve/deny/abort──▶ AGENT
      │                                                                         ▲
      └── unplanned human input detected while AGENT owns it ──▶ (pause) ───────┘

* The automation only acts when `owner == "agent"`; escalate() blocks the automation thread
  until the operator hands control back, so automation and human never act concurrently.
* The live browser context is never closed or recreated across a handoff; the operator acts in
  the same window/session (cookies, server-side session, page state intact).
* Every input event raised by the human is captured through the surface (same element
  vocabulary the agent uses), redacted, logged and attached to the resolution, so discovery can
  turn them into artifact steps and replay can report them.
* Human input that arrives while the agent owns the session is treated as an *unplanned
  takeover*: the agent pauses at the next safe point and raises an intervention rather than
  racing the human.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from cua.evidence.logger import RunLog
from cua.replay.conditions import observed_summary
from cua.surface.base import Surface

Kind = Literal["stuck", "approval", "replay_failure", "unplanned_human_input", "agent_requested"]
Decision = Literal["approve", "deny", "resume", "abort"]


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    kind: Kind
    subject: str  # capability ref or discovery goal
    reason: str
    step: dict
    url: str
    observed: dict
    evidence: dict
    options: list[Decision]
    assigned_to: str = "operator-queue"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class InterventionResolution:
    request_id: str
    decision: Decision
    operator: str
    notes: str = ""
    human_actions: list[dict] = field(default_factory=list)
    duration_s: float = 0.0


class Operator(Protocol):
    name: str

    def handle(self, req: InterventionRequest, surface: Surface) -> tuple[Decision, str]:
        """Called with control already transferred to the human. Returns (decision, notes)."""


class HandoffController:
    def __init__(self, surface: Surface, log: RunLog, operator: Operator | None, subject: str):
        self.surface = surface
        self.log = log
        self.operator = operator
        self.subject = subject
        self.owner: Literal["agent", "human"] = "agent"
        self.actions: list[dict] = []  # every captured human action in this run
        self.unplanned: list[dict] = []
        self.resolutions: list[InterventionResolution] = []
        surface.on_human_event(self._on_human)

    @property
    def available(self) -> bool:
        return self.operator is not None

    def _on_human(self, payload: dict) -> None:
        el = payload.get("el", {})
        rec = {
            "type": payload.get("type"),
            "frame": payload.get("frame"),
            "url": payload.get("url"),
            "element": {k: el.get(k) for k in ("role", "name", "row_label", "text", "attr_name", "tag", "input_type")},
            "value": payload.get("value"),
            "raw": payload,
            "during": self.owner,
        }
        self.actions.append(rec)
        self.log.event("human_action", **{k: v for k, v in rec.items() if k != "raw"})
        if self.owner == "agent":
            self.unplanned.append(rec)

    def take_unplanned(self) -> list[dict]:
        out, self.unplanned = self.unplanned, []
        return out

    def _transfer(self, to: str, reason: str, request_id: str) -> None:
        self.log.event("control_transfer", from_=self.owner, to=to, reason=reason, request_id=request_id)
        self.owner = to  # type: ignore[assignment]

    def escalate(self, kind: Kind, reason: str, step: dict | None = None,
                 options: list[Decision] | None = None) -> InterventionResolution:
        if self.operator is None:
            raise RuntimeError("no operator configured")
        rid = f"int-{len(self.resolutions) + 1:02d}-{secrets.token_hex(2)}"
        states = self.surface.frame_states()
        req = InterventionRequest(
            id=rid, run_id=self.log.run_id, kind=kind, subject=self.subject, reason=reason,
            step=step or {}, url=self.surface.current_url(),
            observed=observed_summary(states, 400),
            evidence=self.log.capture(self.surface, f"intervention-{kind}"),
            options=options or (["approve", "deny", "abort"] if kind == "approval" else ["resume", "abort"]),
        )
        self.log.write_json(f"interventions/{rid}.request.json", req.__dict__)
        self.log.event("intervention_requested", id=rid, kind=kind, reason=reason, step=step, assigned_to=req.assigned_to)
        start_idx, t0 = len(self.actions), time.monotonic()
        self._transfer("human", f"intervention {kind}", rid)
        try:
            decision, notes = self.operator.handle(req, self.surface)
        except KeyboardInterrupt:
            decision, notes = "abort", "operator interrupted"
        self.surface.wait(300)  # drain trailing human events before taking control back
        human_actions = [{k: v for k, v in a.items() if k != "raw"} for a in self.actions[start_idx:]]
        res = InterventionResolution(rid, decision, self.operator.name, notes, human_actions,
                                     round(time.monotonic() - t0, 1))
        self.resolutions.append(res)
        self.log.write_json(f"interventions/{rid}.resolution.json", res.__dict__)
        self.log.event("intervention_resolved", id=rid, decision=decision, operator=self.operator.name,
                       human_actions=len(human_actions), notes=notes)
        self.log.capture(self.surface, f"after-{rid}", dom=False)
        self._transfer("agent", f"operator decision: {decision}", rid)
        self.unplanned.clear()
        return res

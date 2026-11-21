"""Goal-driven discovery: an LLM observe -> decide -> act loop on a live surface.

The model never touches the browser directly. It sees a redacted textual observation
(frame texts + numbered actionable controls) and calls one tool per turn. Every tool call is
policy-checked, executed through the *same* locator machinery replay uses (so what gets
recorded is known to resolve), and appended to a structured trace the recorder turns into an
artifact. The transcript is evidence only; the artifact never depends on it.

Stop conditions: goal_met | agent_failed | max_steps | timeout | stuck (no operator) |
human_aborted | model_no_action. Stuck = same action on the same screen 3x, 3 consecutive
tool errors, or 3 policy blocks; with an operator configured, "stuck" escalates instead of
stopping. Unplanned human input pauses the agent and is recorded (see handoff/controller.py).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from cua.artifact.schema import AnchorNextLocator, Capability, Locator, TableCellLocator
from cua.policy.redact import Redactor
from cua.replay.engine import ReplayEngine, coerce
from cua.runtime import Runtime
from cua.surface.base import ElementInfo, FrameState, LocatorError, Observation

from .model import ModelClient

SYSTEM_PROMPT = """You operate a legacy back-office banking application for a credit union, on behalf of an \
automation platform. Your successful run will be turned into a deterministic, reusable automation, so act \
the way a careful human operator would and take the most direct path to the goal.

Each observation shows the visible text of every frame and a numbered list of actionable controls. Controls \
in legacy screens often have no accessible name; they are identified by the label of the table row they sit in.

Rules:
- One tool call per turn. Refer to controls by their [index] in the LATEST observation only.
- When a value you type or select corresponds to one of the provided input parameters, pass `param` with the \
parameter name instead of the literal text. This is how the automation becomes reusable.
- Never type credentials as text. Use type_secret with one of the allowed secret names.
- To read data, use `extract` with a locator built from stable labels on screen (a table column header plus the \
text identifying the row, or the label cell next to the value). Prefer labels that are the same for every \
member (e.g. row 'SAVINGS'), not values that change.
- Stay on the task. Do not explore unrelated screens. Do not perform actions beyond what the goal requires.
- If a tool result says an action was blocked or denied, do not retry it; choose another path or finish.
- If you are stuck or the screen shows something you cannot handle safely, call request_human.
- When the goal is achieved, call finish with status "success" and a checkpoint_text: a short text visible on \
the final screen that proves success and would be true for ANY valid input (e.g. a page heading), not data \
specific to this member."""


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


_REASON = {"type": "string", "description": "One short sentence: why this action moves toward the goal."}
_EL = {"type": "integer", "description": "Index of the control in the latest observation."}

TOOLS = [
    _tool("click", "Click a control (button, link, checkbox).", {"element": _EL, "reason": _REASON}, ["element", "reason"]),
    _tool("fill", "Type into a text field, replacing its content. Provide exactly one of `param` or `text`.",
          {"element": _EL, "param": {"type": "string", "description": "Name of an input parameter whose value to type."},
           "text": {"type": "string", "description": "Literal text (only for values that are NOT parameters)."},
           "reason": _REASON}, ["element", "reason"]),
    _tool("type_secret", "Type a credential by reference. The value is injected by the platform; you never see it.",
          {"element": _EL, "secret": {"type": "string"}, "reason": _REASON}, ["element", "secret", "reason"]),
    _tool("select", "Choose an option in a drop-down. Provide exactly one of `param` or `option`.",
          {"element": _EL, "param": {"type": "string"}, "option": {"type": "string"}, "reason": _REASON},
          ["element", "reason"]),
    _tool("extract", "Read a value from the screen and return it as a named output of the automation.",
          {"output": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
           "type": {"type": "string", "enum": ["string", "decimal", "integer"]},
           "locator": {"type": "object", "description":
                       "Either {\"kind\":\"table_cell\",\"column\":<column header>,\"row_match\":<exact text of a cell in the row>} "
                       "or {\"kind\":\"anchor_next\",\"anchor_text\":<exact label cell text>}.",
                       "properties": {"kind": {"type": "string", "enum": ["table_cell", "anchor_next"]},
                                      "column": {"type": "string"}, "row_match": {"type": "string"},
                                      "anchor_text": {"type": "string"}},
                       "required": ["kind"]},
           "frame": {"type": "string", "description": "Frame name holding the value, if not the main document."},
           "sensitive": {"type": "boolean", "description": "True for personal data (names, SSNs, addresses)."},
           "reason": _REASON}, ["output", "type", "locator", "reason"]),
    _tool("navigate", "Go to a path within the application (e.g. /app/search). Prefer clicking links.",
          {"path": {"type": "string"}, "reason": _REASON}, ["path", "reason"]),
    _tool("request_human", "Ask a human operator to take over the live session (stuck, unexpected state, or a "
          "decision you should not make).", {"reason": {"type": "string"}}, ["reason"]),
    _tool("finish", "End the run.", {"status": {"type": "string", "enum": ["success", "failed"]},
                                     "summary": {"type": "string"},
                                     "checkpoint_text": {"type": "string"}}, ["status", "summary"]),
]


@dataclass
class TraceStep:
    action: str
    source: str  # agent | human
    description: str
    element: ElementInfo | None = None
    candidates: list[tuple[Locator, str]] = field(default_factory=list)
    value_ref: str | None = None
    output: str | None = None
    output_type: str | None = None
    output_sensitive: bool = False
    read_target: list[Locator] = field(default_factory=list)
    risk: str = "safe"
    pre: FrameState | None = None
    post: FrameState | None = None


@dataclass
class DiscoveryResult:
    status: str  # success | failed
    stop_reason: str
    trace: list[TraceStep]
    summary: str = ""
    checkpoint_text: str | None = None
    final_frame: FrameState | None = None
    steps_taken: int = 0
    usage: dict = field(default_factory=dict)
    secrets_used: set[str] = field(default_factory=set)
    entry_url: str = "/"


class DiscoveryAgent:
    def __init__(self, rt: Runtime, model: ModelClient, goal: str, params: dict[str, str], *,
                 entry_url: str = "/", requires: list[Capability] | None = None, outputs_hint: str = "",
                 max_steps: int | None = None, timeout_s: int | None = None):
        self.rt, self.model, self.goal, self.params = rt, model, goal, params
        self.entry_url = entry_url
        self.requires = requires or []
        self.outputs_hint = outputs_hint
        pol = rt.guard.policy
        self.max_steps = max_steps or pol.max_steps
        self.timeout_s = timeout_s or pol.timeout_s
        self.trace: list[TraceStep] = []
        self.obs: Observation | None = None
        self.secret_values: dict[str, str] = {}
        self.secrets_used: set[str] = set()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
        self._sig_history: list[str] = []
        self._errors = 0
        self._blocks = 0
        self._outputs: dict[str, Any] = {}

    # ------------------------------------------------------------------ setup
    def _load_secrets(self) -> None:
        for name in self.rt.guard.policy.secrets:
            try:
                v = self.rt.guard.policy.resolve_secret(name)
            except RuntimeError:
                continue
            self.secret_values[name] = v
            self.rt.redactor.add_secret(name, v)

    def _observe(self) -> Observation:
        self.obs = self.rt.surface.observe()
        return self.obs

    def _obs_text(self) -> str:
        return self._observe().to_prompt(self.rt.redactor.text)

    def _frame_state(self, frame: str | None) -> FrameState | None:
        for s in self.rt.surface.frame_states():
            if s.name == frame:
                return s
        return None

    def _settle(self) -> None:
        try:
            self.rt.surface.page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass
        self.rt.surface.wait(400)

    # ------------------------------------------------------------------ main loop
    def run(self) -> DiscoveryResult:
        rt, log = self.rt, self.rt.log
        self._load_secrets()
        log.event("discovery_start", goal=self.goal, params=self.params, model=self.model.name,
                  entry_url=self.entry_url, requires=[c.ref for c in self.requires],
                  max_steps=self.max_steps, timeout_s=self.timeout_s)
        deps = {c.id: c for c in self.requires}
        if self.requires:  # preconditions run deterministically, not by the model
            eng = ReplayEngine(rt.surface, rt.guard, log, rt.redactor, rt.handoff, dependencies=deps)
            for cap in self.requires:
                res = eng.run(cap, {})
                if res.status != "success":
                    return self._done("failed", f"precondition {cap.ref} failed: {res.failure}")
        rt.surface.open(self.entry_url)
        intro = (f"GOAL: {self.goal}\n\nINPUT PARAMETERS (use `param` to type these): "
                 f"{json.dumps(self.params)}\n"
                 f"ALLOWED SECRETS for type_secret: {sorted(self.secret_values)}\n"
                 + (f"REQUESTED OUTPUTS: {self.outputs_hint}\n" if self.outputs_hint else "")
                 + f"\nCURRENT SCREEN:\n{self._obs_text()}")
        messages: list[dict] = [{"role": "user", "content": intro}]
        t0 = time.monotonic()
        steps = 0
        nudges = 0
        while True:
            if steps >= self.max_steps:
                return self._done("failed", "max_steps")
            if time.monotonic() - t0 > self.timeout_s:
                return self._done("failed", "timeout")
            if rt.handoff.unplanned:
                note = self._handle_unplanned()
                if note is None:
                    return self._done("failed", "human_aborted")
                messages.append({"role": "user", "content": note})
            turn = self.model.next_turn(SYSTEM_PROMPT, TOOLS, messages)
            self.usage["model_calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                self.usage[k] += turn.usage.get(k, 0)
            log.event("model_turn", text=turn.text[:1500], tool_calls=[{"name": n, "input": i} for _, n, i in turn.tool_calls],
                      stop_reason=turn.stop_reason, usage=turn.usage)
            messages.append({"role": "assistant", "content": turn.content})
            if turn.stop_reason == "refusal":
                return self._done("failed", "model_refusal")
            if not turn.tool_calls:
                nudges += 1
                if nudges > 2:
                    return self._done("failed", "model_no_action")
                messages.append({"role": "user", "content": "Continue by calling exactly one tool."})
                continue
            results = []
            finished: DiscoveryResult | None = None
            for n, (tid, name, args) in enumerate(turn.tool_calls):
                if n > 0:
                    results.append({"type": "tool_result", "tool_use_id": tid, "is_error": True,
                                    "content": "Ignored: only one action per turn is executed."})
                    continue
                steps += 1
                text, ok, finished = self._execute(name, args or {})
                log.event("tool_result", tool=name, ok=ok, result=text.split("\nCURRENT SCREEN:")[0][:400])
                results.append({"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": not ok})
            if finished is not None:
                return finished
            messages.append({"role": "user", "content": results})
            stuck = self._stuck_reason()
            if stuck:
                if rt.handoff.available:
                    note = self._escalate("stuck", stuck)
                    if note is None:
                        return self._done("failed", "human_aborted")
                    messages.append({"role": "user", "content": note})
                    self._sig_history.clear()
                    self._errors = self._blocks = 0
                else:
                    return self._done("failed", f"stuck: {stuck}")

    # ------------------------------------------------------------------ helpers
    def _done(self, status: str, reason: str, summary: str = "", checkpoint: str | None = None,
              frame: FrameState | None = None) -> DiscoveryResult:
        self.rt.log.event("discovery_end", status=status, stop_reason=reason, steps=len(self.trace), usage=self.usage)
        if status != "success":
            self.rt.log.capture(self.rt.surface, f"discovery-{reason}")
        return DiscoveryResult(status, reason, self.trace, summary, checkpoint, frame, len(self.trace),
                               self.usage, self.secrets_used, self.entry_url)

    def _stuck_reason(self) -> str | None:
        if len(self._sig_history) >= 3 and len(set(self._sig_history[-3:])) == 1:
            return "same action on the same screen three times"
        if self._errors >= 3:
            return "three consecutive failed actions"
        if self._blocks >= 3:
            return "three actions blocked by policy"
        return None

    def _signature(self, name: str, args: dict) -> str:
        screen = hashlib.sha1(json.dumps([(s.path, s.title, s.text[:500]) for s in (self.obs.frames if self.obs else [])]).encode()).hexdigest()[:10]
        return f"{name}:{json.dumps(args, sort_keys=True)}:{screen}"

    def _element(self, idx: Any) -> ElementInfo:
        if self.obs is None:
            raise ValueError("no observation yet")
        try:
            return next(e for e in self.obs.elements if e.index == int(idx))
        except (StopIteration, TypeError, ValueError):
            raise ValueError(f"no element [{idx}] in the latest observation")

    def _ok(self, msg: str) -> tuple[str, bool, None]:
        self._errors = 0
        return f"{msg}\nCURRENT SCREEN:\n{self._obs_text()}", True, None

    def _err(self, msg: str, blocked: bool = False) -> tuple[str, bool, None]:
        self._errors += 1
        if blocked:
            self._blocks += 1
        return f"ERROR: {msg}\nCURRENT SCREEN:\n{self._obs_text()}", False, None

    def _param_ref(self, value: str) -> str | None:
        for k, v in self.params.items():
            if v == value:
                return "{{param.%s}}" % k
        return None

    def _execute(self, name: str, a: dict) -> tuple[str, bool, DiscoveryResult | None]:
        rt = self.rt
        self._sig_history.append(self._signature(name, a))
        if name == "finish":
            return self._finish(a)
        if name == "request_human":
            note = self._escalate("agent_requested", a.get("reason", ""))
            if note is None:
                return "aborted", False, self._done("failed", "human_aborted")
            return self._ok(note)
        if name == "navigate":
            path = str(a.get("path", ""))
            d = rt.guard.check_action("navigate", current_url=rt.surface.current_url(),
                                      target_url=rt.surface.base_url + path)
            if not d.allowed:
                rt.log.event("policy_block", action="navigate", path=path, reason=d.reason)
                return self._err(f"blocked by policy: {d.reason}", blocked=True)
            pre = self._frame_state(None)
            rt.surface.navigate(path)
            self._settle()
            self.trace.append(TraceStep("navigate", "agent", a.get("reason", ""), value_ref=path, pre=pre,
                                        post=self._frame_state(None)))
            return self._ok(f"navigated to {path}")
        if name == "extract":
            return self._extract(a)
        try:
            el = self._element(a.get("element"))
        except ValueError as e:
            return self._err(str(e))
        action = {"click": "click", "fill": "fill", "type_secret": "fill", "select": "select"}.get(name)
        if action is None:
            return self._err(f"unknown tool {name}")
        # value resolution (params / secrets / literals)
        value, ref = None, None
        if name == "type_secret":
            sname = a.get("secret", "")
            if sname not in self.secret_values:
                return self._err(f"secret {sname!r} is not allowlisted or not configured", blocked=True)
            value, ref = self.secret_values[sname], "{{secret.%s}}" % sname
            self.secrets_used.add(sname)
        elif name in ("fill", "select"):
            if a.get("param"):
                p = a["param"]
                if p not in self.params:
                    return self._err(f"unknown parameter {p!r}; known: {sorted(self.params)}")
                value, ref = self.params[p], "{{param.%s}}" % p
            else:
                value = a.get("text") if name == "fill" else a.get("option")
                if value is None:
                    return self._err("provide `param` or a literal value")
                ref = self._param_ref(value) or value  # literal equal to a param -> parameterise
            if el.input_type == "password" and name == "fill":
                return self._err("password fields must be filled with type_secret", blocked=True)
        decision = rt.guard.check_action(action, current_url=rt.surface.current_url(), target_text=el.name or el.text)
        rt.log.event("policy_check", tool=name, element=el.label(), allowed=decision.allowed, risk=decision.risk,
                     needs_approval=decision.needs_approval, reason=decision.reason)
        if not decision.allowed:
            return self._err(f"blocked by policy: {decision.reason}", blocked=True)
        if decision.needs_approval:
            if not rt.handoff.available:
                return self._err("this action is irreversible and requires human approval, but no operator is "
                                 "configured. Do not retry; finish instead.", blocked=True)
            start = len(rt.handoff.actions)
            res = rt.handoff.escalate("approval", f"agent wants to {name} '{el.label()}' (irreversible)",
                                      {"tool": name, "element": el.label(), "reason": a.get("reason", "")})
            if res.decision != "approve":
                self._ingest_human(rt.handoff.actions[start:])
                return self._err(f"operator {res.decision} this irreversible action. Do not retry it.", blocked=True)
        candidates = rt.surface.candidates_for(el)
        if not candidates:
            return self._err("this control cannot be targeted reliably; choose another")
        target = [c for c, _ in candidates]
        pre = self._frame_state(el.frame)
        try:
            if action == "click":
                rt.surface.click(target, 5000)
            elif action == "fill":
                rt.surface.fill(target, value or "", 5000)
            else:
                rt.surface.select(target, value or "", 5000)
        except (LocatorError, Exception) as e:  # surface errors go back to the model as tool errors
            return self._err(f"{action} failed: {str(e)[:300]}")
        self._settle()
        post = self._frame_state(el.frame)
        self.trace.append(TraceStep(action, "agent", a.get("reason", ""), el, candidates, ref,
                                    risk=decision.risk, pre=pre, post=post))
        shown = ref if ref and ref.startswith("{{") else (value if action != "click" else None)
        rt.log.event("agent_action", tool=name, element=el.label(), value=shown, risk=decision.risk,
                     locators=[c.by for c in target], reason=a.get("reason", ""))
        return self._ok(f"{name} OK on [{el.index}] {el.label()}")

    def _extract(self, a: dict):
        rt = self.rt
        loc = a.get("locator") or {}
        frame = a.get("frame") or None
        row_match = loc.get("row_match", "")
        try:
            if loc.get("kind") == "table_cell":
                target: Locator = TableCellLocator(column=loc["column"], row_match=row_match, frame=frame)
            else:
                target = AnchorNextLocator(anchor_text=loc["anchor_text"], frame=frame)
            raw, _ = rt.surface.read([target], 3000)
            value = coerce(raw, a.get("type", "string"))
        except Exception as e:
            return self._err(f"extract failed: {str(e)[:300]}")
        out = a["output"]
        sensitive = bool(a.get("sensitive"))
        if sensitive:
            rt.redactor.add_sensitive(out, raw)
        # parameterise the row selector if it equals an input value
        if isinstance(target, TableCellLocator) and self._param_ref(row_match):
            target = TableCellLocator(column=target.column, row_match=self._param_ref(row_match), frame=frame)
        self.trace.append(TraceStep("extract", "agent", a.get("reason", ""), output=out,
                                    output_type=a.get("type", "string"), output_sensitive=sensitive,
                                    read_target=[target]))
        self._outputs[out] = value
        rt.log.event("agent_extract", output=out, value="***" if sensitive else value, locator=target.model_dump(exclude_none=True))
        shown = "[sensitive value captured]" if sensitive else value
        return self._ok(f"extracted {out} = {shown}")

    def _finish(self, a: dict):
        status = a.get("status", "failed")
        summary = a.get("summary", "")
        if status != "success":
            return "finished", True, self._done("failed", "agent_failed", summary)
        cp = (a.get("checkpoint_text") or "").strip()
        states = self.rt.surface.frame_states()
        if not cp:
            return self._err("finish(success) requires checkpoint_text visible on the final screen")
        frame = next((s for s in reversed(states) if cp.upper() in s.text.upper()), None)
        if frame is None:
            return self._err(f"checkpoint_text {cp!r} is not visible on the current screen")
        self.rt.log.capture(self.rt.surface, "discovery-checkpoint", dom=False)
        self.rt.log.event("goal_met", summary=summary, checkpoint_text=cp, outputs=self._outputs)
        return "finished", True, self._done("success", "goal_met", summary, cp, frame)

    # ------------------------------------------------------------------ human-in-the-loop
    def _escalate(self, kind: str, reason: str) -> str | None:
        start = len(self.rt.handoff.actions)
        res = self.rt.handoff.escalate(kind, reason, {"goal": self.goal, "steps_so_far": len(self.trace)})  # type: ignore[arg-type]
        self._ingest_human(self.rt.handoff.actions[start:])
        if res.decision == "abort":
            return None
        did = "; ".join(f"{h['type']} {h['element'].get('name') or h['element'].get('row_label') or h['element'].get('text')}"
                        for h in res.human_actions) or "nothing"
        return (f"A human operator took control ({kind}: {reason}). They did: {did}. Control is back with you. "
                f"Continue toward the goal.\nCURRENT SCREEN:\n{self._obs_text()}")

    def _handle_unplanned(self) -> str | None:
        pending = self.rt.handoff.take_unplanned()
        self.rt.log.event("unplanned_human_input", count=len(pending))
        if not self.rt.handoff.available:
            self._ingest_human(pending)  # no operator to coordinate with: record and continue
            return (f"NOTE: a human interacted with the session ({len(pending)} action(s)). Re-check the screen.\n"
                    f"CURRENT SCREEN:\n{self._obs_text()}")
        self._ingest_human(pending)
        return self._escalate("unplanned_human_input", "human input detected while the agent was in control; "
                              "automation paused")

    def _ingest_human(self, actions: list[dict]) -> None:
        """Turn captured human actions (controller records incl. raw payload) into trace steps (source=human)."""
        surface = self.rt.surface
        for h in actions:
            raw = h.get("raw")
            if raw is None or h.get("type") not in ("click", "fill", "select"):
                continue
            if h["type"] == "fill" and h.get("value") is None:
                self.rt.log.event("human_action_not_recorded", reason="password field: value not captured")
                continue
            el = surface.element_from_capture(raw)
            value, ref = h.get("value"), None
            if value is not None:
                ref = self._param_ref(value)
                for sname, sval in self.secret_values.items():
                    if value == sval:
                        ref = "{{secret.%s}}" % sname
                        self.secrets_used.add(sname)
                ref = ref or value
            self.trace.append(TraceStep(h["type"], "human", "performed by human operator during handoff", el,
                                        surface.candidates_for(el, verify=False), ref))
        if actions and self.trace and self.trace[-1].source == "human":
            last = self.trace[-1]
            last.post = self._frame_state(last.element.frame if last.element else None)

"""Deterministic replay: the production execution path. No LLM anywhere in this module.

Contract (ReplayResult.status):
  success           all steps ran, every postcondition and the checkpoint held; outputs returned.
  business_outcome  the application gave a legitimate answer the caller must handle
                    (e.g. MEMBER_NOT_FOUND). Not an error in the automation.
  failed            hard failure: invalid input, policy violation, locator not found/ambiguous,
                    expectation timeout, known hard failure, recovery exhausted, approval
                    missing/denied, operator abort. Carries step, expected, observed and evidence.

Recoverable conditions (interstitials, session expiry, transient waits) never surface as a
status: they are handled in place, bounded by max_attempts, and listed in `recoveries`.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel

from cua.artifact.schema import Capability, KnownCondition, Step
from cua.evidence.logger import RunLog
from cua.handoff.controller import HandoffController
from cua.policy.guard import Guard
from cua.policy.redact import Redactor
from cua.surface.base import LocatorError, Surface

from .conditions import check_all, detect, observed_summary, render


class Failure(BaseModel):
    category: Literal[
        "invalid_input", "policy_violation", "approval_required", "approval_denied", "locator_not_found",
        "locator_ambiguous", "expectation_timeout", "known_hard_failure", "recovery_exhausted",
        "unsafe_to_recover", "checkpoint_not_met", "extraction_invalid", "dependency_failed", "human_aborted",
    ]
    message: str
    step_id: str | None = None
    step_index: int | None = None
    code: str | None = None
    expected: Any = None
    observed: Any = None
    url: str | None = None
    locator_attempts: list[Any] | None = None
    evidence: dict[str, str] = {}


class Outcome(BaseModel):
    code: str
    message: str
    step_id: str | None = None


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    status: Literal["success", "business_outcome", "failed"]
    outputs: dict[str, Any] = {}
    outcome: Outcome | None = None
    failure: Failure | None = None
    recoveries: list[dict] = []
    warnings: list[str] = []
    interventions: list[dict] = []
    steps_completed: int = 0
    duration_ms: int = 0
    evidence_dir: str = ""


class _Stop(Exception):
    def __init__(self, result_kind: str, payload: Failure | Outcome):
        self.kind = result_kind
        self.payload = payload


class _Restart(Exception):
    pass


def coerce(value: str, typ: str) -> Any:
    v = value.strip()
    if typ == "decimal":
        cleaned = re.sub(r"[,$\s]", "", v)
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        return str(Decimal(cleaned))  # decimal as string: no float rounding of money
    if typ == "integer":
        return int(re.sub(r"[,\s]", "", v))
    return v


def validate_inputs(cap: Capability, params: dict[str, str]) -> list[str]:
    errors = []
    for name, spec in cap.inputs.items():
        if name not in params:
            errors.append(f"missing input {name!r}")
            continue
        v = str(params[name])
        if spec.pattern and not re.fullmatch(spec.pattern, v):
            errors.append(f"input {name!r} does not match pattern {spec.pattern}")
        if spec.type == "integer" and not re.fullmatch(r"-?\d+", v):
            errors.append(f"input {name!r} must be an integer")
        if spec.type == "decimal":
            try:
                Decimal(v)
            except InvalidOperation:
                errors.append(f"input {name!r} must be a decimal")
        if spec.type == "enum" and spec.enum and v not in spec.enum:
            errors.append(f"input {name!r} must be one of {spec.enum}")
    errors += [f"unknown input {n!r}" for n in params if n not in cap.inputs]
    return errors


class ReplayEngine:
    def __init__(self, surface: Surface, guard: Guard, log: RunLog, redactor: Redactor,
                 handoff: HandoffController | None = None, approve_irreversible: bool = False,
                 dependencies: dict[str, Capability] | None = None):
        self.surface = surface
        self.guard = guard
        self.log = log
        self.redactor = redactor
        self.handoff = handoff
        self.approve_irreversible = approve_irreversible
        self.dependencies = dependencies or {}
        self.recoveries: list[dict] = []
        self.warnings: list[str] = []
        self._completed = 0  # steps of the current attempt finished so far (reported even on early stop)

    # ------------------------------------------------------------------ public API
    def run(self, cap: Capability, params: dict[str, str]) -> ReplayResult:
        params = {k: str(v) for k, v in params.items()}
        log = self.log
        log.event("replay_start", capability=cap.ref, status=cap.status, risk=cap.risk,
                  params={k: ("***" if cap.inputs.get(k) and cap.inputs[k].sensitive else v) for k, v in params.items()})
        for name, spec in cap.inputs.items():
            if spec.sensitive:
                self.redactor.add_sensitive(name, params.get(name))
        if cap.status != "approved":
            self.warnings.append(f"capability {cap.ref} is {cap.status}, not approved")
        outputs: dict[str, Any] = {}
        completed = 0
        try:
            errors = validate_inputs(cap, params)
            if errors:
                raise _Stop("failed", Failure(category="invalid_input", message="; ".join(errors),
                                              expected={k: v.model_dump(exclude_none=True) for k, v in cap.inputs.items()},
                                              observed={k: v for k, v in params.items()}))
            secrets = self._resolve_secrets(cap)
            restarts = 0
            while True:
                try:
                    self._run_dependencies(cap)
                    self.surface.open(cap.entry_url)
                    log.event("navigate", path=cap.entry_url)
                    completed = self._run_steps(cap, params, secrets, outputs)
                    break
                except _Restart:
                    restarts += 1
                    if restarts > 2:
                        raise _Stop("failed", Failure(category="recovery_exhausted", message="too many restarts"))
                    log.event("restart", attempt=restarts)
            self._verify_checkpoint(cap, params, len(cap.steps))
            ev = log.capture(self.surface, "checkpoint", dom=False)
            log.event("checkpoint_passed", evidence=ev)
            return self._result(cap, "success", outputs=outputs, completed=completed)
        except _Stop as stop:
            completed = self._completed
            if stop.kind == "business_outcome":
                log.event("business_outcome", **stop.payload.model_dump())
                return self._result(cap, "business_outcome", outcome=stop.payload, outputs=outputs, completed=completed)
            fail: Failure = stop.payload  # type: ignore[assignment]
            log.event("failed", **fail.model_dump(exclude={"evidence"}))
            return self._result(cap, "failed", failure=fail, outputs=outputs, completed=completed)

    # ------------------------------------------------------------------ internals
    def _result(self, cap, status, *, outputs, completed, outcome=None, failure=None) -> ReplayResult:
        # Diagnostics (observed screen text, messages, warnings) are redacted at the source so no
        # consumer (CLI stdout, a calling agent, a ticket) ever sees PII. Only `outputs`, the values
        # the caller explicitly asked for, are returned in the clear.
        r = self.redactor.obj
        res = ReplayResult(
            run_id=self.log.run_id, capability=cap.ref, status=status, outputs=outputs if status == "success" else {},
            outcome=Outcome.model_validate(r(outcome.model_dump())) if outcome else None,
            failure=(Failure.model_validate({**r(failure.model_dump(exclude={"evidence"})), "evidence": failure.evidence})
                     if failure else None),  # evidence = our own generated file paths
            recoveries=r(self.recoveries), warnings=r(self.warnings),
            interventions=r([dict(x.__dict__) for x in (self.handoff.resolutions if self.handoff else [])]),
            steps_completed=completed, duration_ms=self.log.elapsed_ms(), evidence_dir=self.log.rel(self.log.dir),
        )
        self.log.write_json("result.json", res.model_dump())  # log layer redacts again (outputs included)
        self.log.event("replay_end", status=status, steps_completed=completed, duration_ms=res.duration_ms)
        return res

    def _resolve_secrets(self, cap: Capability) -> dict[str, str]:
        out = {}
        for name in cap.secrets:
            try:
                out[name] = self.guard.policy.resolve_secret(name)
            except (PermissionError, RuntimeError) as e:
                raise _Stop("failed", Failure(category="policy_violation", message=str(e)))
            self.redactor.add_secret(name, out[name])
        return out

    def _run_dependencies(self, cap: Capability) -> None:
        for dep_id in cap.requires:
            dep = self.dependencies.get(dep_id)
            if dep is None:
                raise _Stop("failed", Failure(category="dependency_failed", message=f"required capability {dep_id} not loaded"))
            self.log.event("dependency_start", capability=dep.ref)
            sub = ReplayEngine(self.surface, self.guard, self.log, self.redactor, self.handoff,
                               self.approve_irreversible, self.dependencies)
            secrets = sub._resolve_secrets(dep)
            self.surface.open(dep.entry_url)
            sub._run_steps(dep, {}, secrets, {})
            sub._verify_checkpoint(dep, {}, len(dep.steps))
            self.recoveries += sub.recoveries
            self.warnings += sub.warnings
            self.log.event("dependency_passed", capability=dep.ref)

    def _fail(self, category: str, message: str, step: Step | None = None, idx: int | None = None, **kw) -> _Stop:
        states = self.surface.frame_states()
        evidence = self.log.capture(self.surface, f"failure-{category}")
        return _Stop("failed", Failure(category=category, message=message, step_id=step.id if step else None,
                                       step_index=idx, url=self.surface.current_url(),
                                       observed=kw.pop("observed", observed_summary(states)), evidence=evidence, **kw))

    def _run_steps(self, cap: Capability, params: dict[str, str], secrets: dict[str, str], outputs: dict) -> int:
        self._executed_irreversible = False
        i = 0
        retried: set[int] = set()
        while i < len(cap.steps):
            self._completed = i
            step = cap.steps[i]
            self._scan_conditions(cap, params, step, i)  # interstitials / expiry before acting
            self._enforce_policy(step, params, secrets, i)
            try:
                self._act(cap, step, params, secrets, outputs, i)
            except LocatorError as e:
                self._scan_conditions(cap, params, step, i)  # maybe the app is showing an error page
                category = "locator_ambiguous" if e.kind == "ambiguous" else "locator_not_found"
                if self._escalate_and_retry(step, i, f"{category}: could not resolve target", retried):
                    if step.expect and self._expect_ok(step, params):
                        self.log.event("step_completed_by_human", step=step.id)
                        i += 1
                    continue
                raise self._fail(category, f"step {step.id}: target {e.kind}", step, i,
                                 expected=[t.model_dump(exclude_none=True) for t in step.target],
                                 locator_attempts=e.tried)
            if step.risk == "irreversible":
                self._executed_irreversible = True
            if not self._await_expectation(cap, step, params, i):
                if self._escalate_and_retry(step, i, "postcondition not met after action", retried):
                    if self._expect_ok(step, params):
                        i += 1
                    continue
                states = self.surface.frame_states()
                ok, failed = check_all(step.expect, self.surface, params, states)
                raise self._fail("expectation_timeout", f"step {step.id}: expected state not reached within "
                                 f"{step.timeout_ms} ms", step, i, expected=failed)
            i += 1
        return i

    def _enforce_policy(self, step: Step, params, secrets, i: int) -> None:
        target_text = ""
        if step.target:
            first = step.target[0].model_dump()
            target_text = first.get("name") or first.get("text") or first.get("label") or ""
        target_url = None
        if step.action == "navigate":
            target_url = self.surface.base_url + render(step.value, params, secrets)
        d = self.guard.check_action(step.action, current_url=self.surface.current_url(),
                                    target_text=target_text, target_url=target_url)
        effective_risk = "irreversible" if (d.risk == "irreversible" or step.risk == "irreversible") else "safe"
        self.log.event("policy_check", step=step.id, action=step.action, allowed=d.allowed, risk=effective_risk,
                       reason=d.reason)
        if not d.allowed:
            raise self._fail("policy_violation", d.reason, step, i)
        if effective_risk != "irreversible" or (not d.needs_approval and step.risk != "irreversible"):
            return
        if self.approve_irreversible:
            self.log.event("approval", step=step.id, by="caller (pre-authorised for this invocation)")
            return
        if self.handoff and self.handoff.available:
            res = self.handoff.escalate("approval", f"irreversible step {step.id}: {step.description}",
                                        {"id": step.id, "index": i, "description": step.description})
            if res.decision == "approve":
                return
            raise self._fail("approval_denied" if res.decision == "deny" else "human_aborted",
                             f"operator {res.decision} irreversible step {step.id}", step, i)
        raise self._fail("approval_required", f"step {step.id} is irreversible and no approval was provided "
                         "(pass approve_irreversible or configure an operator)", step, i)

    def _act(self, cap: Capability, step: Step, params, secrets, outputs, i: int) -> None:
        value = render(step.value, params, secrets) if step.value is not None else None
        shown = step.value  # log the reference, never the substituted value
        if step.action == "navigate":
            self.surface.navigate(value or "/")
            self.log.event("step", index=i, id=step.id, action="navigate", value=shown)
            return
        if step.action == "click":
            r = self.surface.click(step.target, step.timeout_ms)
        elif step.action == "fill":
            r = self.surface.fill(step.target, value or "", step.timeout_ms)
        elif step.action == "select":
            r = self.surface.select(step.target, value or "", step.timeout_ms)
        elif step.action == "extract":
            raw, r = self.surface.read([self._render_loc(t, params) for t in step.target], step.timeout_ms)
            spec = cap.outputs[step.output]  # type: ignore[index]
            try:
                outputs[step.output] = coerce(raw, spec.type)  # type: ignore[index]
            except (InvalidOperation, ValueError):
                raise self._fail("extraction_invalid", f"could not parse {raw!r} as {spec.type}", step, i)
            if spec.sensitive:
                self.redactor.add_sensitive(step.output, raw)  # type: ignore[arg-type]
            self.log.event("step", index=i, id=step.id, action="extract", output=step.output,
                           value=outputs[step.output] if not spec.sensitive else "***",  # type: ignore[index]
                           locator_used=r.used_index, locator=r.locator.by)
            self._note_degraded(step, r)
            return
        else:
            raise ValueError(step.action)
        self.log.event("step", index=i, id=step.id, action=step.action, value=shown,
                       locator_used=r.used_index, locator=r.locator.by, source=step.source)
        self._note_degraded(step, r)

    @staticmethod
    def _render_loc(loc, params):
        data = loc.model_dump()
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = render(v, params)
        return type(loc).model_validate(data)

    def _note_degraded(self, step: Step, r) -> None:
        if r.used_index > 0:
            msg = f"locator_degraded: step {step.id} used fallback #{r.used_index} ({r.locator.by}); preferred locator(s) failed"
            self.warnings.append(msg)
            self.log.event("locator_degraded", step=step.id, used=r.used_index, tried=r.tried)

    def _expect_ok(self, step: Step, params) -> bool:
        ok, _ = check_all(step.expect, self.surface, params, self.surface.frame_states())
        return ok

    def _await_expectation(self, cap: Capability, step: Step, params, i: int) -> bool:
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            states = self.surface.frame_states()
            ok, _ = check_all(step.expect, self.surface, params, states)
            if ok:
                if step.expect:
                    self.log.event("expect_ok", step=step.id)
                return True
            kc = detect(cap.known_conditions, self.surface, params, states)
            if kc is not None:
                self._handle_condition(cap, kc, step, i, params)
                continue  # recovered in place; re-check the expectation
            if time.monotonic() >= deadline:
                return False
            self.surface.wait(250)

    def _scan_conditions(self, cap: Capability, params, step: Step, i: int) -> None:
        for _ in range(5):
            kc = detect(cap.known_conditions, self.surface, params, self.surface.frame_states())
            if kc is None:
                return
            self._handle_condition(cap, kc, step, i, params)

    def _handle_condition(self, cap: Capability, kc: KnownCondition, step: Step, i: int, params) -> None:
        self.log.event("condition_detected", code=kc.code, kind=kc.kind, step=step.id)
        if kc.kind == "business_outcome":
            self.log.capture(self.surface, f"outcome-{kc.code}", dom=False)
            raise _Stop("business_outcome", Outcome(code=kc.code, message=kc.message, step_id=step.id))
        if kc.kind == "hard_failure":
            if self._escalate_and_retry(step, i, f"known hard failure {kc.code}: {kc.message}", set()):
                raise _Restart() if not self._executed_irreversible else self._fail(
                    "unsafe_to_recover", "cannot restart after an irreversible step", step, i)
            raise self._fail("known_hard_failure", kc.message, step, i, code=kc.code)
        rec = kc.recovery
        assert rec is not None
        attempts = sum(1 for r in self.recoveries if r["code"] == kc.code)
        if attempts >= rec.max_attempts:
            raise self._fail("recovery_exhausted", f"{kc.code} persisted after {attempts} recovery attempt(s)",
                             step, i, code=kc.code)
        self.recoveries.append({"code": kc.code, "action": rec.action, "step": step.id, "attempt": attempts + 1})
        self.log.event("recovery", code=kc.code, action=rec.action, attempt=attempts + 1, step=step.id)
        if rec.action == "dismiss":
            try:
                self.surface.click(rec.target, 5000)
            except LocatorError as e:
                raise self._fail("recovery_exhausted", f"could not dismiss {kc.code}", step, i,
                                 code=kc.code, locator_attempts=e.tried)
            self.surface.wait(300)
        elif rec.action == "wait":
            self.surface.wait(1500)
        elif rec.action == "relogin_restart":
            if self._executed_irreversible:
                raise self._fail("unsafe_to_recover", f"{kc.code} after an irreversible step; refusing to "
                                 "restart a flow that may already have committed", step, i, code=kc.code)
            raise _Restart()

    def _escalate_and_retry(self, step: Step, i: int, reason: str, retried: set[int]) -> bool:
        """Hand the live session to a human; True if the step should be re-attempted."""
        if not (self.handoff and self.handoff.available) or i in retried:
            return False
        retried.add(i)
        res = self.handoff.escalate("replay_failure", reason,
                                    {"id": step.id, "index": i, "description": step.description})
        if res.decision in ("resume", "approve"):
            self.log.event("resume_after_handoff", step=step.id)
            return True
        raise self._fail("human_aborted", f"operator chose {res.decision}", step, i)

    def _verify_checkpoint(self, cap: Capability, params, completed: int) -> None:
        deadline = time.monotonic() + 5
        while True:
            states = self.surface.frame_states()
            ok, failed = check_all(cap.checkpoint, self.surface, params, states)
            if ok:
                return
            kc = detect(cap.known_conditions, self.surface, params, states)
            if kc is not None and kc.kind != "recoverable":
                self._handle_condition(cap, kc, cap.steps[-1], completed - 1, params)
            if time.monotonic() >= deadline:
                raise self._fail("checkpoint_not_met", "final checkpoint did not hold", None, None, expected=failed)
            self.surface.wait(250)

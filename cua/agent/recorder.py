"""Recorder: turns a successful discovery trace into a typed, versioned capability artifact.

What it does beyond copying steps:
* keeps only actions that worked, collapses repeated fills of the same field;
* parameterises values ({{param.x}}, {{secret.y}}) and URL/row selectors that contain inputs;
* attaches the verified, ordered locator candidates for every control with a robustness note;
* derives a postcondition (`expect`) for every action that changed the screen, so replay
  verifies state instead of assuming a click worked;
* builds the checkpoint from the agent's checkpoint_text plus the final screen identity;
* copies the app profile's known runtime conditions (error taxonomy) into the artifact;
* marks the capability irreversible if any step was, and records provenance (incl. human steps).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from cua.artifact.schema import (
    AllOf, AppRef, Capability, KnownCondition, OutputSpec, ParamSpec, Provenance, SecretRef, Step, TextVisible,
    TitleIs, UrlMatches,
)
from cua.policy.guard import Policy

from .loop import DiscoveryResult, TraceStep


def _slug(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:maxlen] or "step"


def _path_pattern(path: str, params: dict[str, str]) -> str:
    """'/app/member/12345' -> '^/app/member/{{param.member_id}}$' (literal parts regex-escaped)."""
    pieces = [(path, False)]
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if not value:
            continue
        nxt = []
        for text, is_ph in pieces:
            if is_ph or value not in text:
                nxt.append((text, is_ph))
                continue
            parts = text.split(value)
            for j, part in enumerate(parts):
                nxt.append((part, False))
                if j < len(parts) - 1:
                    nxt.append(("{{param.%s}}" % name, True))
        pieces = nxt
    return "^" + "".join(t if ph else re.escape(t) for t, ph in pieces) + "$"


def _param_text(text: str, params: dict[str, str]) -> str:
    for name, value in sorted(params.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = text.replace(value, "{{param.%s}}" % name)
    return text


def _infer_param(value: str) -> ParamSpec:
    if re.fullmatch(r"\d+", value):
        return ParamSpec(type="string", pattern=rf"^\d{{{len(value)}}}$",
                         description=f"numeric identifier ({len(value)} digits; inferred from the discovery example — review)")
    if re.fullmatch(r"\d+\.\d+", value):
        return ParamSpec(type="decimal", description="decimal amount")
    return ParamSpec(type="string", description="free text")


def _collapse(trace: list[TraceStep]) -> list[TraceStep]:
    out: list[TraceStep] = []
    for t in trace:
        if (out and t.action in ("fill", "select") and out[-1].action == t.action and t.element and out[-1].element
                and t.candidates and out[-1].candidates and t.candidates[0][0] == out[-1].candidates[0][0]):
            out[-1] = t  # the later value wins
            continue
        out.append(t)
    return out


def build_capability(*, cap_id: str, version: str, goal: str, result: DiscoveryResult, params: dict[str, str],
                     policy: Policy, app_profile: dict, requires: list[str], run_id: str, model: str,
                     tenant: str | None = None, title: str | None = None) -> Capability:
    if result.status != "success":
        raise ValueError("only successful discovery runs can be recorded")
    steps: list[Step] = []
    outputs: dict[str, OutputSpec] = {}
    used_ids: set[str] = set()
    trace = _collapse(result.trace)
    for t in trace:
        label = t.element.label() if t.element else (t.output or t.value_ref or t.action)
        base = _slug(f"{t.action}_{t.output or label}")
        sid, n = base, 2
        while sid in used_ids:
            sid, n = f"{base}_{n}", n + 1
        used_ids.add(sid)
        expect = None
        if t.action in ("click", "navigate") and t.post is not None and (
                t.pre is None or (t.post.path, t.post.title) != (t.pre.path, t.pre.title)):
            expect = AllOf(all_of=[UrlMatches(pattern=_path_pattern(t.post.path, params), frame=t.post.name),
                                   TitleIs(title=t.post.title, frame=t.post.name)])
        if t.action == "extract":
            outputs[t.output] = OutputSpec(type=t.output_type or "string", sensitive=t.output_sensitive,  # type: ignore[index]
                                           description=t.description)
            steps.append(Step(id=sid, action="extract", description=t.description or f"read {t.output}",
                              output=t.output, target=t.read_target,
                              why="read by table column header + row text (or adjacent label): independent of "
                                  "row order and cell position"))
            continue
        if t.action == "navigate":
            steps.append(Step(id=sid, action="navigate", description=t.description, value=t.value_ref, expect=expect))
            continue
        why = " | ".join(f"#{i + 1} {loc.by}: {w}" for i, (loc, w) in enumerate(t.candidates))
        steps.append(Step(
            id=sid, action=t.action, description=t.description or f"{t.action} {label}",  # type: ignore[arg-type]
            target=[c for c, _ in t.candidates], value=t.value_ref if t.action != "click" else None,
            expect=expect, risk="irreversible" if t.risk == "irreversible" else "safe",
            source=t.source, why=why))  # type: ignore[arg-type]

    final = result.final_frame
    checkpoint = [TextVisible(text=_param_text(result.checkpoint_text or "", params), frame=final.name if final else None)]
    if final is not None:
        checkpoint += [TitleIs(title=final.title, frame=final.name),
                       UrlMatches(pattern=_path_pattern(final.path, params), frame=final.name)]
    blob = "".join(s.model_dump_json() for s in steps) + "".join(c.model_dump_json() for c in checkpoint)
    used_params = {name for name in params if "{{param.%s}}" % name in blob}
    human = sum(1 for s in steps if s.source == "human")
    app = app_profile["app"]
    kcs: list[KnownCondition] = list(app_profile.get("known_conditions", []))
    return Capability(
        id=cap_id, version=version, status="draft",
        title=_param_text(title or goal, params)[:100], description=_param_text(goal, params),
        app=AppRef(**app), entry_url=result.entry_url, requires=requires,
        risk="irreversible" if any(s.risk == "irreversible" for s in steps) else "read_only",
        inputs={k: _infer_param(v) for k, v in params.items() if k in used_params},
        secrets={k: SecretRef(key=policy.secrets[k].key) for k in sorted(result.secrets_used)},
        outputs=outputs, steps=steps, checkpoint=AllOf(all_of=checkpoint), known_conditions=kcs,
        provenance=Provenance(
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), discovery_run_id=run_id,
            model=model, tenant=tenant, human_steps=human,
            notes=("contains human-performed steps with unverified locators — review before approval; " if human else "")
                  + "input patterns inferred from a single example",
        ),
    )

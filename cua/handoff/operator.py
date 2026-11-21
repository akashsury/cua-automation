"""Operator surfaces.

* CliOperator — the minimal real operator console: the intervention request is printed in the
  terminal, the human works directly in the *same* headed browser window the automation was
  driving, and types a decision when done. Human input is captured while they work.
* ScriptedOperator — a deterministic stand-in used for tests and for reproducible evidence.
  It performs its actions through real browser input on the live session (so they flow through
  exactly the same capture path as a person's clicks), but it is a simulation and is labelled
  as such in every log line.

Production design (not built): requests go to a queue/inbox, the operator opens a
co-browsing view of the session (CDP screencast or VNC to the worker), and the decision is
posted back to the run via an API. The controller contract is identical.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path

from cua.artifact.schema import Locator
from cua.surface.base import Surface

from .controller import Decision, InterventionRequest


class CliOperator:
    name = "cli-operator"

    def handle(self, req: InterventionRequest, surface: Surface) -> tuple[Decision, str]:
        bar = "=" * 78
        print(f"\n{bar}\nINTERVENTION REQUIRED [{req.kind}] {req.id}\n"
              f"  subject : {req.subject}\n  reason  : {req.reason}\n"
              f"  step    : {json.dumps(req.step)[:200]}\n  url     : {req.url}\n"
              f"  evidence: {req.evidence.get('screenshot')}\n"
              f"You now have control of the browser window. Your actions are recorded.\n"
              f"Options: {', '.join(req.options)}\n{bar}", file=sys.stderr, flush=True)
        answers: queue.Queue[str] = queue.Queue()

        def reader() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    answers.put("abort")
                    return
                answers.put(line.strip().lower())

        threading.Thread(target=reader, daemon=True).start()
        while True:
            print(f"decision ({'/'.join(req.options)}) > ", end="", file=sys.stderr, flush=True)
            while True:
                try:
                    ans = answers.get_nowait()
                    break
                except queue.Empty:
                    surface.wait(250)  # keep pumping browser events so human input is captured
            for opt in req.options:
                if ans and opt.startswith(ans):
                    notes = ""
                    return opt, notes  # type: ignore[return-value]
            print(f"unknown option {ans!r}", file=sys.stderr)


class ScriptedOperator:
    """Replays a JSON script: a list of responses consumed one per intervention.

    [{"kind": "approval", "decision": "approve", "notes": "..."},
     {"actions": [{"click": [<locator>...]}, {"fill": [[<locator>...], "value"]}], "decision": "resume"}]
    """

    name = "scripted-operator (simulated human)"

    def __init__(self, script: list[dict]):
        self.script = list(script)

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedOperator":
        return cls(json.loads(Path(path).read_text()))

    def handle(self, req: InterventionRequest, surface: Surface) -> tuple[Decision, str]:
        if not self.script:
            return "abort", "scripted operator has no response left"
        entry = self.script.pop(0)
        if "kind" in entry and entry["kind"] != req.kind:
            return "abort", f"scripted operator expected {entry['kind']} but got {req.kind}"
        from pydantic import TypeAdapter
        locs = TypeAdapter(list[Locator])
        for act in entry.get("actions", []):
            surface.wait(400)
            if "click" in act:
                surface.operator_click(locs.validate_python(act["click"]))
            elif "fill" in act:
                target, value = act["fill"]
                surface.operator_fill(locs.validate_python(target), value)
            elif "select" in act:
                target, value = act["select"]
                surface.operator_select(locs.validate_python(target), value)
            surface.wait(400)
        return entry.get("decision", "resume"), entry.get("notes", "")

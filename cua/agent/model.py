"""Model clients for the discovery loop.

AnthropicModel calls Claude via the official SDK. ScriptedModel replays canned tool calls so the
whole pipeline (loop -> recorder -> artifact -> replay) can run in tests without network access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class ModelTurn:
    content: list[Any]  # blocks to append verbatim as the assistant message
    tool_calls: list[tuple[str, str, dict]]  # (id, name, input)
    text: str
    stop_reason: str
    usage: dict


class ModelClient(Protocol):
    name: str

    def next_turn(self, system: str, tools: list[dict], messages: list[dict]) -> ModelTurn: ...


class AnthropicModel:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium"):
        import anthropic

        self.client = anthropic.Anthropic()
        self.name = model
        self.effort = effort

    def next_turn(self, system, tools, messages) -> ModelTurn:
        resp = self.client.beta.messages.create(
            model=self.name,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            # Server-side refusal fallback: if a safety classifier declines, the API re-runs the
            # request on a fallback model instead of stopping the run.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        calls = [(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return ModelTurn(resp.content, calls, text, resp.stop_reason or "", usage)


class ScriptedModel:
    """Returns pre-written tool calls in order: [{"tool": name, "input": {...}}, ...]."""

    name = "scripted"

    def __init__(self, calls: list[dict]):
        self.calls = list(calls)
        self.n = 0

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedModel":
        return cls(json.loads(Path(path).read_text()))

    def next_turn(self, system, tools, messages) -> ModelTurn:
        if not self.calls:
            return ModelTurn([{"type": "text", "text": "script exhausted"}], [], "script exhausted", "end_turn", {})
        c = self.calls.pop(0)
        self.n += 1
        tid = f"toolu_scripted_{self.n:03d}"
        block = {"type": "tool_use", "id": tid, "name": c["tool"], "input": c["input"]}
        return ModelTurn([block], [(tid, c["tool"], c["input"])], "", "tool_use", {})

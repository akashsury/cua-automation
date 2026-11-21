"""The surface seam: how we perceive and act on an application, independent of the flow.

Everything above this interface (agent loop, recorder, replay engine, handoff) talks only in
terms of `ElementInfo`, `Observation`, `FrameState` and `Locator`s from the artifact schema.
A desktop implementation would back the same interface with UI Automation / AX trees
(role + name + containing-row map directly onto those APIs); a screenshot-only surface would
back `observe()` with a vision model and locators with image anchors. See REPORT.md §4.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cua.artifact.schema import Locator


@dataclass
class ElementInfo:
    """One actionable control as perceived on the surface (set-of-marks style)."""
    index: int
    frame: str | None
    role: str
    name: str
    tag: str
    input_type: str = ""
    attr_name: str = ""
    row_label: str = ""
    text: str = ""
    css: str = ""
    value: str = ""
    options: list[str] | None = None

    def label(self) -> str:
        return self.name or self.row_label or self.text or self.attr_name or self.css


@dataclass
class FrameState:
    name: str | None
    url: str
    path: str
    title: str
    text: str


@dataclass
class Observation:
    url: str
    frames: list[FrameState]
    elements: list[ElementInfo]

    def to_prompt(self, redact: Callable[[str], str], max_text: int = 2500) -> str:
        lines = [f"TOP URL: {self.url}"]
        for f in self.frames:
            where = f"frame '{f.name}'" if f.name else "main document"
            body = redact(f.text)[:max_text]
            lines.append(f"\n--- {where} | path {f.path} | title '{f.title}' ---\n{body}")
        lines.append("\nACTIONABLE ELEMENTS (use the [index]):")
        for e in self.elements:
            bits = [f"[{e.index}] {e.role}"]
            if e.name:
                bits.append(f'name="{e.name}"')
            if e.row_label and e.row_label != e.name:
                bits.append(f'row="{e.row_label}"')
            if e.input_type == "password":
                bits.append("(password field)")
            elif e.value and e.role in ("textbox", "combobox"):
                bits.append(f'value="{redact(e.value)}"')
            if e.options:
                bits.append(f"options={e.options}")
            if e.frame:
                bits.append(f"frame={e.frame}")
            lines.append("  " + " ".join(bits))
        return "\n".join(lines)


@dataclass
class Resolution:
    used_index: int  # 0 = preferred locator; >0 means a fallback was used
    locator: Locator
    tried: list[tuple[str, int]] = field(default_factory=list)  # (locator summary, match count)


class LocatorError(Exception):
    def __init__(self, kind: str, tried: list[tuple[str, int]]):
        self.kind = kind  # "not_found" | "ambiguous"
        self.tried = tried
        super().__init__(f"{kind}: " + "; ".join(f"{d} -> {n}" for d, n in tried))


HumanEventCallback = Callable[[dict], None]


class Surface(abc.ABC):
    """Perceive / act / evidence. One live session per instance."""

    base_url: str

    @abc.abstractmethod
    def open(self, path: str) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    # perception
    @abc.abstractmethod
    def observe(self) -> Observation: ...

    @abc.abstractmethod
    def frame_states(self) -> list[FrameState]: ...

    @abc.abstractmethod
    def count(self, loc: Locator) -> int: ...

    @abc.abstractmethod
    def candidates_for(self, el: ElementInfo, verify: bool = True) -> list[tuple[Locator, str]]:
        """Locator strategies for an observed element, ordered most-robust first, each verified
        (unless verify=False) to resolve uniquely right now, with a reviewer-facing reason."""

    # action (all targets are ordered fallback lists)
    @abc.abstractmethod
    def resolve(self, target: list[Locator], timeout_ms: int) -> Resolution: ...

    @abc.abstractmethod
    def click(self, target: list[Locator], timeout_ms: int) -> Resolution: ...

    @abc.abstractmethod
    def fill(self, target: list[Locator], value: str, timeout_ms: int) -> Resolution: ...

    @abc.abstractmethod
    def select(self, target: list[Locator], value: str, timeout_ms: int) -> Resolution: ...

    @abc.abstractmethod
    def read(self, target: list[Locator], timeout_ms: int) -> tuple[str, Resolution]: ...

    @abc.abstractmethod
    def navigate(self, path: str) -> None: ...

    @abc.abstractmethod
    def current_url(self) -> str: ...

    @abc.abstractmethod
    def wait(self, ms: int) -> None:
        """Idle while still pumping surface events (human capture etc.)."""

    # evidence
    @abc.abstractmethod
    def screenshot(self, path: Path) -> Path: ...

    @abc.abstractmethod
    def dom_snapshot(self, path: Path, redact: Callable[[str], str]) -> Path: ...

    # handoff support
    @abc.abstractmethod
    def on_human_event(self, cb: HumanEventCallback) -> None: ...

    @abc.abstractmethod
    def agent_acting(self):
        """Context manager: events raised inside are attributed to automation, not a human."""

"""Evaluation of surface-neutral conditions and parameter substitution."""

from __future__ import annotations

import re
from typing import Iterable

from cua.artifact.schema import (
    PLACEHOLDER, AllOf, Condition, ElementPresent, KnownCondition, TextVisible, TitleIs, UrlMatches,
)
from cua.surface.base import FrameState, Surface


def render(value: str | None, params: dict[str, str], secrets: dict[str, str] | None = None, *, regex: bool = False) -> str:
    """Substitute {{param.x}} / {{secret.y}}. With regex=True, substituted values are escaped."""
    if value is None:
        return ""
    secrets = secrets or {}

    def sub(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2)
        src = params if kind == "param" else secrets
        if name not in src:
            raise KeyError(f"missing {kind}.{name}")
        v = str(src[name])
        return re.escape(v) if regex else v

    return PLACEHOLDER.sub(sub, value)


def _frames(states: list[FrameState], frame: str | None) -> Iterable[FrameState]:
    return [s for s in states if s.name == frame] if frame else states


def check(cond: Condition, surface: Surface, params: dict[str, str], states: list[FrameState]) -> bool:
    if isinstance(cond, TextVisible):
        text = render(cond.text, params, regex=cond.match == "regex")
        for s in _frames(states, cond.frame):
            if cond.match == "regex" and re.search(text, s.text):
                return True
            if cond.match == "contains" and text.upper() in s.text.upper():
                return True
        return False
    if isinstance(cond, UrlMatches):
        pat = render(cond.pattern, params, regex=True)
        return any(re.search(pat, s.path) for s in _frames(states, cond.frame))
    if isinstance(cond, TitleIs):
        want = render(cond.title, params).strip().upper()
        return any(s.title.strip().upper() == want for s in _frames(states, cond.frame))
    if isinstance(cond, ElementPresent):
        return any(surface.count(loc) == 1 for loc in cond.target)
    raise TypeError(cond)


def check_all(allof: AllOf | None, surface: Surface, params: dict[str, str], states: list[FrameState]) -> tuple[bool, list[str]]:
    if allof is None:
        return True, []
    failed = [c.model_dump_json(exclude_none=True) for c in allof.all_of if not check(c, surface, params, states)]
    return not failed, failed


def detect(known: list[KnownCondition], surface: Surface, params: dict[str, str], states: list[FrameState]) -> KnownCondition | None:
    for kc in known:
        try:
            if check(kc.detect, surface, params, states):
                return kc
        except KeyError:
            continue
    return None


def observed_summary(states: list[FrameState], limit: int = 600) -> dict:
    return {
        "frames": [
            {"frame": s.name, "path": s.path, "title": s.title, "text": s.text.strip()[:limit]} for s in states
        ]
    }

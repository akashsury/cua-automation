"""Policy guard: the allowlist and the risk model.

Every action — whether proposed by the LLM during discovery, executed by replay, or taken
by the operator through the automation API — is checked here *before* it reaches the surface.

Risk model (REPORT.md §6):
* `safe`         — navigation, reading, filling a form field. Reversible: nothing is committed.
* `irreversible` — a click that commits a change (confirm/submit/transfer/delete) or any action
                   on a commit route. Handling is configurable: block | require_approval | flag.
                   Default is require_approval: a human (or the calling agent with an explicit,
                   per-invocation approval) must authorise it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict

ActionType = Literal["click", "fill", "select", "extract", "navigate", "type_secret"]


class SecretSource(BaseModel):
    provider: Literal["env"] = "env"
    key: str


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_origins: list[str]
    allowed_paths: list[str] = [".*"]
    blocked_paths: list[str] = []
    allowed_actions: list[ActionType] = ["click", "fill", "select", "extract", "navigate", "type_secret"]
    irreversible_text: list[str] = []  # regexes over the target control's name/text
    irreversible_paths: list[str] = []  # regexes over the path the action is taken on
    irreversible_mode: Literal["block", "require_approval", "flag"] = "require_approval"
    secrets: dict[str, SecretSource] = {}
    max_steps: int = 30
    timeout_s: int = 300

    def resolve_secret(self, name: str) -> str:
        src = self.secrets.get(name)
        if src is None:
            raise PermissionError(f"secret {name!r} is not allowlisted")
        value = os.environ.get(src.key)
        if not value:
            raise RuntimeError(f"secret {name!r}: environment variable {src.key} is not set")
        return value


@dataclass
class Decision:
    allowed: bool
    risk: Literal["safe", "irreversible"] = "safe"
    needs_approval: bool = False
    reason: str = ""


def load_policy(path: str | Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text()))


class Guard:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check_url(self, url: str) -> Decision:
        if url in ("about:blank", ""):
            return Decision(True)
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.policy.allowed_origins:
            return Decision(False, reason=f"origin {origin} is not allowlisted")
        path = u.path or "/"
        if any(re.search(p, path) for p in self.policy.blocked_paths):
            return Decision(False, reason=f"path {path} is explicitly blocked")
        if not any(re.search(p, path) for p in self.policy.allowed_paths):
            return Decision(False, reason=f"path {path} is not allowlisted")
        return Decision(True)

    def check_action(self, action: str, *, current_url: str, target_text: str = "", target_url: str | None = None) -> Decision:
        if action not in self.policy.allowed_actions:
            return Decision(False, reason=f"action type {action!r} is not allowlisted")
        for url in filter(None, [current_url, target_url]):
            d = self.check_url(url)
            if not d.allowed:
                return d
        risky = action == "click" and any(re.search(p, target_text or "", re.I) for p in self.policy.irreversible_text)
        path = urlparse(current_url).path
        risky = risky or (action == "click" and any(re.search(p, path) for p in self.policy.irreversible_paths))
        if not risky:
            return Decision(True)
        mode = self.policy.irreversible_mode
        if mode == "block":
            return Decision(False, "irreversible", reason=f"irreversible action on {target_text!r} is blocked by policy")
        return Decision(True, "irreversible", needs_approval=(mode == "require_approval"),
                        reason=f"irreversible action: {target_text!r}")

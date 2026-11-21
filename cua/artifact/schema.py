"""Capability artifact schema (schema_version 1.0).

A capability is the reusable, reviewable, agent-invocable output of a discovery run.
It is deliberately decoupled from the model transcript: nothing here says *why the model*
did something, only *what* to do, *how to find* each control, *what must be true* afterwards,
and *what can legitimately go wrong*.

Design notes (see REPORT.md §2 for the full rationale):

* Contract first: `inputs`, `outputs`, `risk`, `known_conditions` are what a calling agent
  needs; `steps` are the implementation. A reviewer reads top to bottom.
* Every step targets a control through an *ordered list* of locator strategies, most
  semantic first. Replay uses the first one that resolves to exactly one element.
* Every step can declare an `expect` postcondition so replay never assumes a click worked.
* Runtime conditions are data (`known_conditions`), classified into the three-way taxonomy
  business_outcome / recoverable / hard_failure.
* Values are references (`{{param.x}}`, `{{secret.y}}`), never recorded literals, so no PII or
  credentials are stored in the artifact.
* Surface-specific details live only inside `Locator`; steps/conditions are surface-neutral.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"
PLACEHOLDER = re.compile(r"\{\{\s*(param|secret)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# Locators — HOW a control is found. The only surface-specific part of the schema.
# --------------------------------------------------------------------------------------


class RoleLocator(_Model):
    """Accessibility role + accessible name. Most stable; exists on web and desktop (UIA/AX)."""
    by: Literal["role"] = "role"
    role: str
    name: str
    frame: str | None = None


class LabelLocator(_Model):
    """Control associated with a visible <label>."""
    by: Literal["label"] = "label"
    text: str
    frame: str | None = None


class RowLabelLocator(_Model):
    """Legacy table forms: the input that sits in the table row whose label cell reads `label`.
    Works when there is no accessible name at all — the common legacy case."""
    by: Literal["row_label"] = "row_label"
    label: str
    control: Literal["input", "select", "textarea", "button"] = "input"
    frame: str | None = None


class TextLocator(_Model):
    """Clickable element by exact visible text (links, buttons rendered as text)."""
    by: Literal["text"] = "text"
    text: str
    frame: str | None = None


class CssLocator(_Model):
    """Last resort: structural/attribute selector. Brittle; kept so replay degrades rather than dies."""
    by: Literal["css"] = "css"
    selector: str
    frame: str | None = None


class TableCellLocator(_Model):
    """Read-only target: the cell in column `column` of the row containing `row_match`."""
    by: Literal["table_cell"] = "table_cell"
    column: str
    row_match: str
    frame: str | None = None


class AnchorNextLocator(_Model):
    """Read-only target: the value cell next to a label cell (e.g. 'NAME:' -> 'JANE ...')."""
    by: Literal["anchor_next"] = "anchor_next"
    anchor_text: str
    frame: str | None = None


Locator = Annotated[
    Union[RoleLocator, LabelLocator, RowLabelLocator, TextLocator, CssLocator, TableCellLocator, AnchorNextLocator],
    Field(discriminator="by"),
]


# --------------------------------------------------------------------------------------
# Conditions — surface-neutral predicates over the current state.
# --------------------------------------------------------------------------------------


class TextVisible(_Model):
    type: Literal["text_visible"] = "text_visible"
    text: str
    match: Literal["contains", "regex"] = "contains"
    frame: str | None = None


class UrlMatches(_Model):
    type: Literal["url_matches"] = "url_matches"
    pattern: str  # regex on the path; may contain {{param.x}}
    frame: str | None = None


class TitleIs(_Model):
    type: Literal["title_is"] = "title_is"
    title: str
    frame: str | None = None


class ElementPresent(_Model):
    type: Literal["element_present"] = "element_present"
    target: list[Locator]


Condition = Annotated[Union[TextVisible, UrlMatches, TitleIs, ElementPresent], Field(discriminator="type")]


class AllOf(_Model):
    all_of: list[Condition]


# --------------------------------------------------------------------------------------
# Known runtime conditions — the error taxonomy as data.
# --------------------------------------------------------------------------------------


class Recovery(_Model):
    action: Literal["dismiss", "wait", "relogin_restart"]
    target: list[Locator] = []  # for dismiss
    max_attempts: int = 1


class KnownCondition(_Model):
    code: str  # stable, caller-facing, e.g. MEMBER_NOT_FOUND
    kind: Literal["business_outcome", "recoverable", "hard_failure"]
    detect: Condition
    message: str
    recovery: Recovery | None = None

    @model_validator(mode="after")
    def _recovery_only_for_recoverable(self):
        if self.kind == "recoverable" and self.recovery is None:
            raise ValueError("recoverable conditions must declare a recovery")
        if self.kind != "recoverable" and self.recovery is not None:
            raise ValueError("only recoverable conditions may declare a recovery")
        return self


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


class Step(_Model):
    id: str
    action: Literal["fill", "click", "select", "extract", "navigate"]
    description: str
    target: list[Locator] = []  # ordered fallbacks; empty only for navigate
    value: str | None = None  # literal or {{param.x}} / {{secret.y}}; fill/select/navigate
    output: str | None = None  # extract: name of the declared output
    expect: AllOf | None = None  # postcondition verified after the action
    risk: Literal["safe", "irreversible"] = "safe"
    source: Literal["agent", "human"] = "agent"
    timeout_ms: int = 10_000
    why: str = ""  # reviewer-facing note on locator robustness

    @field_validator("target")
    @classmethod
    def _non_empty_target(cls, v, info):
        if info.data.get("action") != "navigate" and not v:
            raise ValueError("non-navigate steps need at least one locator")
        return v


# --------------------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------------------


class ParamSpec(_Model):
    type: Literal["string", "integer", "decimal", "enum"] = "string"
    description: str = ""
    pattern: str | None = None
    enum: list[str] | None = None
    sensitive: bool = False  # masked in logs/evidence


class OutputSpec(_Model):
    type: Literal["string", "integer", "decimal"] = "string"
    description: str = ""
    sensitive: bool = False


class SecretRef(_Model):
    """Where a credential comes from at run time. The value is never stored."""
    provider: Literal["env"] = "env"
    key: str


class AppRef(_Model):
    vendor: str
    product: str
    version_range: str = "*"
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"


class Provenance(_Model):
    recorded_at: str
    discovery_run_id: str
    model: str | None = None
    tenant: str | None = None
    human_steps: int = 0
    notes: str = ""


class Capability(_Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str
    version: str
    status: Literal["draft", "approved", "deprecated"] = "draft"
    title: str
    description: str
    app: AppRef
    entry_url: str  # path relative to the tenant's base URL
    requires: list[str] = []  # capability ids that must have run first (e.g. sign-on)
    risk: Literal["read_only", "reversible", "irreversible"] = "read_only"
    inputs: dict[str, ParamSpec] = {}
    secrets: dict[str, SecretRef] = {}
    outputs: dict[str, OutputSpec] = {}
    steps: list[Step]
    checkpoint: AllOf
    known_conditions: list[KnownCondition] = []
    provenance: Provenance

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not SEMVER.match(v):
            raise ValueError("version must be semver MAJOR.MINOR.PATCH")
        return v

    def model_post_init(self, __context) -> None:  # cross-field integrity checks
        declared = {"param": set(self.inputs), "secret": set(self.secrets)}
        for step in self.steps:
            for kind, name in PLACEHOLDER.findall(step.model_dump_json()):
                if name not in declared[kind]:
                    raise ValueError(f"step {step.id} references undeclared {kind}.{name}")
            if step.action == "extract" and step.output not in self.outputs:
                raise ValueError(f"step {step.id} extracts undeclared output {step.output!r}")
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        if any(s.risk == "irreversible" for s in self.steps) and self.risk != "irreversible":
            raise ValueError("capability with an irreversible step must declare risk: irreversible")

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

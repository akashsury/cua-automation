"""File-based artifact store.

Layout:  artifacts/<capability_id>/<MAJOR.MINOR.PATCH>.yaml       (immutable once written)
         artifacts/<capability_id>/overrides/<tenant>.yaml         (per-tenant patches)

Versions are never overwritten; a new recording or edit produces a new version. Callers can
pin `id@1.2.0` or ask for `id` (latest approved, else latest draft).
Tenant overrides patch individual steps/conditions by id so one base recording can be
reused across institutions running the same vendor product (see REPORT.md §4).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from .schema import Capability

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "artifacts"


def artifacts_root() -> Path:
    """Override with CUA_ARTIFACTS_DIR (tests, sandboxes)."""
    return Path(os.environ.get("CUA_ARTIFACTS_DIR") or DEFAULT_ROOT)


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def versions(cap_id: str, root: Path | None = None) -> list[str]:
    root = root or artifacts_root()
    d = root / cap_id
    if not d.is_dir():
        return []
    return sorted((p.stem for p in d.glob("*.yaml")), key=_vkey)


def next_version(cap_id: str, root: Path | None = None, bump: str = "minor") -> str:
    root = root or artifacts_root()
    vs = versions(cap_id, root)
    if not vs:
        return "1.0.0"
    major, minor, patch = _vkey(vs[-1])
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def to_yaml(cap: Capability) -> str:
    data = cap.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def save(cap: Capability, root: Path | None = None) -> Path:
    root = root or artifacts_root()
    path = root / cap.id / f"{cap.version}.yaml"
    if path.exists():
        raise FileExistsError(f"{path} exists; artifacts are immutable — bump the version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_yaml(cap))
    return path


def set_status(path: Path, status: str) -> None:
    """The one permitted in-place change: lifecycle status (draft -> approved -> deprecated)."""
    data = yaml.safe_load(path.read_text())
    data["status"] = status
    Capability.model_validate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))


def resolve_path(ref: str, root: Path | None = None) -> Path:
    root = root or artifacts_root()
    p = Path(ref)
    if p.suffix in {".yaml", ".yml"} and p.exists():
        return p
    cap_id, _, version = ref.partition("@")
    if version:
        return root / cap_id / f"{version}.yaml"
    vs = versions(cap_id, root)
    if not vs:
        raise FileNotFoundError(f"no versions of capability {cap_id!r} under {root}")
    approved = [v for v in vs if yaml.safe_load((root / cap_id / f"{v}.yaml").read_text()).get("status") == "approved"]
    return root / cap_id / f"{(approved or vs)[-1]}.yaml"


def _apply_overrides(data: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    steps = {s["id"]: s for s in data["steps"]}
    for step_id, patch in (override.get("steps") or {}).items():
        if step_id not in steps:
            raise ValueError(f"override references unknown step {step_id!r}")
        if "prepend_target" in patch:  # tenant-specific locators tried first, base ones kept as fallback
            steps[step_id]["target"] = patch.pop("prepend_target") + steps[step_id]["target"]
        steps[step_id].update(patch)
    data["known_conditions"] = data.get("known_conditions", []) + (override.get("add_known_conditions") or [])
    data.setdefault("provenance", {})["notes"] = (
        data["provenance"].get("notes", "") + f" [tenant override: {override.get('tenant')}]"
    ).strip()
    return data


def load(ref: str, tenant: str | None = None, root: Path | None = None) -> tuple[Capability, Path]:
    root = root or artifacts_root()
    path = resolve_path(ref, root)
    data = yaml.safe_load(path.read_text())
    if tenant:
        opath = path.parent / "overrides" / f"{tenant}.yaml"
        if opath.exists():
            data = _apply_overrides(data, yaml.safe_load(opath.read_text()))
    return Capability.model_validate(data), path

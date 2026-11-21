import pytest
import yaml
from pydantic import ValidationError

from cua.artifact import store
from cua.artifact.schema import Capability

from .conftest import FIX, load_fixture


def raw(name):
    return yaml.safe_load((FIX / name).read_text())


def test_fixtures_validate():
    for n in ["sign_on.yaml", "lookup.yaml", "open_share.yaml"]:
        assert load_fixture(n).schema_version == "1.0"


def test_undeclared_param_rejected():
    d = raw("lookup.yaml")
    d["steps"][0]["value"] = "{{param.nope}}"
    with pytest.raises(ValidationError, match="undeclared param.nope"):
        Capability.model_validate(d)


def test_irreversible_step_requires_irreversible_capability():
    d = raw("open_share.yaml")
    d["risk"] = "read_only"
    with pytest.raises(ValidationError, match="irreversible"):
        Capability.model_validate(d)


def test_recoverable_condition_needs_recovery():
    d = raw("lookup.yaml")
    d["known_conditions"] = [{"code": "X", "kind": "recoverable", "message": "m",
                              "detect": {"type": "text_visible", "text": "X"}}]
    with pytest.raises(ValidationError, match="must declare a recovery"):
        Capability.model_validate(d)


def test_store_is_immutable_and_versioned(tmp_path):
    cap = load_fixture("lookup.yaml")
    store.save(cap)
    with pytest.raises(FileExistsError):
        store.save(cap)
    assert store.next_version(cap.id) == "1.1.0"
    assert store.next_version(cap.id, bump="major") == "2.0.0"
    loaded, _ = store.load(cap.id)
    assert loaded.ref == cap.ref


def test_tenant_override_prepends_locators(tmp_path):
    cap = load_fixture("lookup.yaml")
    path = store.save(cap)
    (path.parent / "overrides").mkdir()
    (path.parent / "overrides" / "b.yaml").write_text(yaml.safe_dump({
        "tenant": "b",
        "steps": {"inquire": {"prepend_target": [{"by": "role", "role": "button", "name": "Search", "frame": "workarea"}]}},
    }))
    base, _ = store.load(cap.id)
    over, _ = store.load(cap.id, tenant="b")
    step = next(s for s in over.steps if s.id == "inquire")
    assert step.target[0].name == "Search"
    assert len(step.target) == len(next(s for s in base.steps if s.id == "inquire").target) + 1

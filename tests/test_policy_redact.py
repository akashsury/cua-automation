from cua.policy.guard import Guard, Policy
from cua.policy.redact import Redactor

POL = Policy(allowed_origins=["http://127.0.0.1:8765"], allowed_paths=["^/$", "^/app/", "^/login$"],
             blocked_paths=["^/__"], irreversible_text=[r"^\s*confirm\b"], allowed_actions=["click", "fill", "navigate"])


def test_origin_and_path_allowlist():
    g = Guard(POL)
    assert g.check_url("http://127.0.0.1:8765/app/search").allowed
    assert not g.check_url("https://evil.example.com/app/search").allowed
    assert not g.check_url("http://127.0.0.1:8765/__faults").allowed
    assert not g.check_url("http://127.0.0.1:8765/other").allowed


def test_action_allowlist():
    g = Guard(POL)
    assert not g.check_action("select", current_url="http://127.0.0.1:8765/app/search").allowed


def test_irreversible_requires_approval_or_blocks():
    g = Guard(POL)
    d = g.check_action("click", current_url="http://127.0.0.1:8765/app/x", target_text="Confirm")
    assert d.allowed and d.risk == "irreversible" and d.needs_approval
    assert g.check_action("click", current_url="http://127.0.0.1:8765/app/x", target_text="Continue").risk == "safe"
    blocked = Guard(POL.model_copy(update={"irreversible_mode": "block"}))
    assert not blocked.check_action("click", current_url="http://127.0.0.1:8765/app/x", target_text="Confirm").allowed


def test_redaction_layers():
    r = Redactor()
    r.add_secret("pw", "hunter2-secret")
    r.add_sensitive("member_name", "JANE Q SAMPLE")
    out = r.text("pw=hunter2-secret name=JANE Q SAMPLE ssn=900-12-3456 acct=123456789012 mail=a.b@example.com ref=x.y@1.0.0")
    assert "hunter2" not in out and "JANE" not in out and "900-12" not in out and "123456789012" not in out
    assert "example.com" not in out and "x.y@1.0.0" in out
    assert r.obj({"a": ["hunter2-secret"]}) == {"a": ["[SECRET:pw]"]}


def test_card_pattern_is_luhn_checked_and_spares_run_ids():
    r = Redactor()
    out = r.text("card 4111 1111 1111 1111 in replay-20261120-165012-8b4b; not a card 1234 5678 9012 3456")
    assert "[REDACTED:card] in" in out and "replay-20261120-165012-8b4b" in out and "1234 5678 9012 3456" in out

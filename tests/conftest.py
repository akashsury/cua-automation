import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml

os.environ.setdefault("MOCKBANK_USER", "teller01")
os.environ.setdefault("MOCKBANK_PASSWORD", "training-only-pw")

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def server():
    from mockbank.app import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.05)
    yield BASE
    srv.should_exit = True


@pytest.fixture
def policy_path(tmp_path):
    data = yaml.safe_load((Path(__file__).parents[1] / "config" / "policy.yaml").read_text())
    data["allowed_origins"] = [BASE]
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("CUA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CUA_EVIDENCE_DIR", str(tmp_path / "evidence"))


@pytest.fixture
def reset(server):
    from cua import runtime
    import urllib.request
    urllib.request.urlopen(urllib.request.Request(f"{server}/__reset", method="POST")).read()
    runtime.inject_fault(server, None)
    return server


def load_fixture(name, known=True):
    from cua import runtime
    from cua.artifact.schema import Capability
    cap = Capability.model_validate(yaml.safe_load((FIX / name).read_text()))
    if known and not cap.known_conditions:
        cap.known_conditions = runtime.load_app_profile()["known_conditions"]
    return cap

"""Run evidence: one directory per run with a redacted structured log and rich failure signals.

evidence/runs/<run_id>/
  events.jsonl            every step, decision, locator used, condition, control transfer
  shots/*.png             masked screenshots (checkpoint, failures, handoffs)
  dom/*.html              redacted DOM snapshots (all frames) on failure / handoff
  interventions/*.json    intervention requests and their resolutions
  result.json             the final structured result (redacted)
  artifact.yaml           (discovery) copy of the emitted capability

Playwright traces are deliberately NOT kept: they embed unredacted DOM and screenshots,
which would violate the no-raw-PII rule. See REPORT.md §6.
"""

from __future__ import annotations

import json
import secrets as _secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.policy.redact import Redactor

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "evidence" / "runs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, kind: str, redactor: Redactor, root: Path = DEFAULT_ROOT, verbose: bool = True,
                 run_id: str | None = None):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = run_id or f"{kind}-{stamp}-{_secrets.token_hex(2)}"
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.verbose = verbose
        self._seq = 0
        self._t0 = time.monotonic()
        self._fh = (self.dir / "events.jsonl").open("a")

    def event(self, type_: str, **fields: Any) -> dict:
        self._seq += 1
        rec = {"ts": _now(), "seq": self._seq, "run_id": self.run_id, "type": type_, **self.redactor.obj(fields)}
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        if self.verbose:
            brief = {k: v for k, v in rec.items() if k not in {"ts", "run_id", "seq", "type"}}
            text = json.dumps(brief, ensure_ascii=False, default=str)
            print(f"  [{self._seq:03d}] {type_:<22} {text[:220]}", file=sys.stderr)
        return rec

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.dir.parents[2]))
        except ValueError:
            return str(p)

    def capture(self, surface, label: str, dom: bool = True) -> dict[str, str]:
        """Screenshot (+ redacted DOM) — the richer signal on failures and handoffs."""
        out: dict[str, str] = {}
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        name = f"{self._seq:03d}-{safe}"
        try:
            out["screenshot"] = self.rel(surface.screenshot(self.dir / "shots" / f"{name}.png"))
        except Exception as e:  # evidence must never crash the run
            out["screenshot_error"] = str(e)
        if dom:
            try:
                out["dom"] = self.rel(surface.dom_snapshot(self.dir / "dom" / f"{name}.html", self.redactor.text))
            except Exception as e:
                out["dom_error"] = str(e)
        return out

    def write_json(self, rel_path: str, obj: Any) -> Path:
        p = self.dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, ensure_ascii=False, default=str))
        return p

    def write_text(self, rel_path: str, text: str) -> Path:
        p = self.dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.redactor.text(text))
        return p

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def close(self) -> None:
        self._fh.close()

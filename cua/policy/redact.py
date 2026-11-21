"""Redaction of credentials and regulated data before anything is logged or persisted.

Three layers, applied in order:
1. Exact secret values (credentials resolved at run time)    -> [SECRET:<name>]
2. Exact sensitive values the run knows about (sensitive params/outputs) -> [REDACTED:<label>]
3. Pattern-based PII/financial identifiers (SSN, long account numbers, card numbers, emails)

Limits (documented in REPORT.md §6): pattern redaction is best-effort; screenshots are masked
by element, not by OCR.
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}\b")),  # + Luhn check below
    ("account", re.compile(r"\b\d{10,17}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")),
]


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(self) -> None:
        self._exact: list[tuple[str, str]] = []

    def add_secret(self, name: str, value: str | None) -> None:
        if value:
            self._exact.append((value, f"[SECRET:{name}]"))
            self._exact.sort(key=lambda kv: -len(kv[0]))

    def add_sensitive(self, label: str, value: str | None) -> None:
        if value and len(value) >= 3:
            self._exact.append((value, f"[REDACTED:{label}]"))
            self._exact.sort(key=lambda kv: -len(kv[0]))

    def text(self, s: str) -> str:
        if not s:
            return s
        for value, repl in self._exact:
            s = s.replace(value, repl)
        for label, pat in PATTERNS:
            if label == "card":  # only Luhn-valid digit runs: avoids eating dates, run ids, timestamps
                s = pat.sub(lambda m: "[REDACTED:card]" if _luhn(m.group()) else m.group(), s)
            else:
                s = pat.sub(f"[REDACTED:{label}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

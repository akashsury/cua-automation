"""Web implementation of the surface seam using Playwright (Chromium).

Perception is DOM/accessibility based but deliberately *not* dependent on a clean DOM:
each control is described by role, accessible name, the label of the table row it sits in,
its form-field name and, as a last resort, its structural CSS path. Frames are first-class.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, sync_playwright

from cua.artifact.schema import (
    AnchorNextLocator, CssLocator, LabelLocator, Locator, RoleLocator, RowLabelLocator,
    TableCellLocator, TextLocator,
)

from .base import ElementInfo, FrameState, HumanEventCallback, LocatorError, Observation, Resolution, Surface

# Shared JS: element description (used for perception AND for capturing human actions,
# so a human's click is recorded in exactly the same vocabulary as the agent's).
JS_HELPERS = r"""
const __norm = s => (s || '').replace(/\s+/g, ' ').trim();
const __visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
const __role = el => { const r = el.getAttribute('role'); if (r) return r;
  const t = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || 'text').toLowerCase();
  if (t === 'a') return 'link'; if (t === 'button') return 'button'; if (t === 'select') return 'combobox';
  if (t === 'textarea') return 'textbox';
  if (t === 'input') { if (['submit','button','reset','image'].includes(ty)) return 'button';
    if (ty === 'checkbox') return 'checkbox'; if (ty === 'radio') return 'radio'; return 'textbox'; }
  return t; };
const __name = el => { const a = el.getAttribute('aria-label'); if (a) return __norm(a);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) return __norm(lb.split(' ').map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
  if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return __norm(l.innerText); }
  const wl = el.closest('label'); if (wl) return __norm(wl.innerText);
  const t = el.tagName.toLowerCase(); const ty = (el.getAttribute('type') || '').toLowerCase();
  if (t === 'input' && ['submit','button','reset'].includes(ty)) return __norm(el.value);
  if (t === 'a' || t === 'button') return __norm(el.innerText);
  return __norm(el.getAttribute('title') || ''); };
const __rowLabel = el => { const tr = el.closest('tr'); if (!tr) return '';
  for (const c of tr.children) { if (c.contains(el)) continue; const tx = __norm(c.innerText); if (tx) return tx; }
  return ''; };
const __css = el => { const parts = [];
  while (el && el.nodeType === 1 && el.tagName.toLowerCase() !== 'html') { let i = 1, s = el;
    while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
    parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); el = el.parentElement; }
  return parts.join(' > '); };
const __describe = el => ({
  role: __role(el), name: __name(el), tag: el.tagName.toLowerCase(),
  input_type: (el.getAttribute('type') || '').toLowerCase(), attr_name: el.getAttribute('name') || '',
  row_label: __rowLabel(el), text: __norm(el.innerText || '').slice(0, 80), css: __css(el),
  value: el.tagName === 'SELECT' ? __norm((el.options[el.selectedIndex] || {}).text)
        : (el.type === 'password' ? '' : (el.value || '')),
  options: el.tagName === 'SELECT' ? [...el.options].map(o => __norm(o.text)) : null });
const __SEL = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link]';
"""

JS_ENUMERATE = "() => {" + JS_HELPERS + "return [...document.querySelectorAll(__SEL)].filter(__visible).map(__describe); }"

JS_CAPTURE = "(() => {" + JS_HELPERS + r"""
if (window.__cuaCapture) return; window.__cuaCapture = true;
const send = p => { try { window.__cua_human_event && window.__cua_human_event(p); } catch (e) {} };
document.addEventListener('click', e => { const el = e.target.closest(__SEL); if (el) send({type: 'click', el: __describe(el)}); }, true);
document.addEventListener('change', e => { const el = e.target; if (!el.matches || !el.matches(__SEL)) return;
  const d = __describe(el); send({type: el.tagName === 'SELECT' ? 'select' : 'fill', el: d,
    value: el.type === 'password' ? null : (el.tagName === 'SELECT' ? d.value : el.value)}); }, true);
})();"""

JS_TABLE_CELL = r"""([column, rowMatch]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toUpperCase();
  const col = norm(column), row = norm(rowMatch); const hits = [];
  for (const table of document.querySelectorAll('table')) {
    const rows = [...table.rows]; if (!rows.length) continue;
    const hdrIdx = rows.findIndex(r => [...r.cells].some(c => norm(c.innerText) === col));
    if (hdrIdx < 0) continue;
    const ci = [...rows[hdrIdx].cells].findIndex(c => norm(c.innerText) === col);
    for (const r of rows.slice(hdrIdx + 1))
      if ([...r.cells].some(c => norm(c.innerText) === row) && r.cells[ci]) hits.push(r.cells[ci].innerText.trim());
  }
  return hits; }"""

JS_ANCHOR_NEXT = r"""(anchor) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toUpperCase(); const a = norm(anchor); const hits = [];
  for (const el of document.querySelectorAll('td,th,dt,label,span,b')) {
    if (norm(el.innerText) !== a) continue; const nx = el.nextElementSibling; if (nx) hits.push(nx.innerText.trim()); }
  return hits; }"""


def _xq(s: str) -> str:
    """Quote a string for XPath."""
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in s.split('"')) + ")"


def describe_locator(loc: Locator) -> str:
    d = loc.model_dump(exclude_none=True)
    return json.dumps(d, ensure_ascii=False)


ROW_CONTROLS = {
    "input": "input[not(@type='hidden') and not(@type='submit') and not(@type='button')]",
    "select": "select",
    "textarea": "textarea",
    "button": "*[self::button or self::input[@type='submit' or @type='button']]",
}


class WebSurface(Surface):
    def __init__(self, base_url: str, headed: bool = False, slow_mo: int = 0, viewport=(1100, 760)):
        self.base_url = base_url.rstrip("/")
        self._play = sync_playwright().start()
        self._browser = self._play.chromium.launch(headless=not headed, slow_mo=slow_mo)
        self._context = self._browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        self._human_cbs: list[HumanEventCallback] = []
        self._acting = 0
        self._context.expose_binding("__cua_human_event", self._on_binding)
        self._context.add_init_script(JS_CAPTURE)
        self.page: Page = self._context.new_page()

    # ------------------------------------------------------------------ lifecycle
    def open(self, path: str) -> None:
        self.page.goto(self._abs(path), wait_until="load")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._context.close()
            self._browser.close()
        with contextlib.suppress(Exception):
            self._play.stop()

    def _abs(self, path: str) -> str:
        return path if path.startswith("http") else self.base_url + (path if path.startswith("/") else "/" + path)

    # ------------------------------------------------------------------ frames
    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def _frame(self, name: str | None) -> Frame | None:
        if not name:
            return self.page.main_frame
        return self.page.frame(name=name)

    @staticmethod
    def _fname(f: Frame) -> str | None:
        return f.name or None

    def frame_states(self) -> list[FrameState]:
        out = []
        for f in self._frames():
            try:
                f.wait_for_load_state("domcontentloaded", timeout=5000)
                text = f.locator("body").inner_text(timeout=2000)
                title = f.title()
            except PWError:
                continue
            out.append(FrameState(self._fname(f), f.url, urlparse(f.url).path, title, text))
        return out

    # ------------------------------------------------------------------ perception
    def observe(self) -> Observation:
        self.page.wait_for_load_state("load")
        elements: list[ElementInfo] = []
        for f in self._frames():
            try:
                items = f.evaluate(JS_ENUMERATE)
            except PWError:
                continue
            for it in items:
                elements.append(ElementInfo(index=len(elements) + 1, frame=self._fname(f), **it))
        return Observation(self.page.url, self.frame_states(), elements)

    def _loc(self, loc: Locator):
        """Playwright locator for an actionable Locator. frame=None means "any frame": the
        locator is evaluated in every frame and the frame that holds the match is used."""
        if loc.frame is None:
            frames = self._frames()
            hits = [self._loc_in(f, loc) for f in frames]
            nonzero = [h for h in hits if h.count() > 0]
            return nonzero[0] if len(nonzero) == 1 else hits[0]
        f = self._frame(loc.frame)
        if f is None:
            raise LookupError(f"frame {loc.frame!r} not present")
        return self._loc_in(f, loc)

    def _loc_in(self, f: Frame, loc: Locator):
        if isinstance(loc, RoleLocator):
            return f.get_by_role(loc.role, name=loc.name, exact=True).filter(visible=True)
        if isinstance(loc, LabelLocator):
            return f.get_by_label(loc.text, exact=True).filter(visible=True)
        if isinstance(loc, RowLabelLocator):
            xp = f"//tr[*[normalize-space(.)={_xq(loc.label)}]]//{ROW_CONTROLS[loc.control]}"
            return f.locator(f"xpath={xp}").filter(visible=True)
        if isinstance(loc, TextLocator):
            return f.get_by_text(loc.text, exact=True).filter(visible=True)
        if isinstance(loc, CssLocator):
            return f.locator(loc.selector).filter(visible=True)
        raise TypeError(f"{loc.by} is not an actionable locator")

    def count(self, loc: Locator) -> int:
        try:
            if isinstance(loc, (TableCellLocator, AnchorNextLocator)):
                return len(self._read_hits(loc))
            if loc.frame is None:
                return sum(self._loc_in(f, loc).count() for f in self._frames())
            return self._loc(loc).count()
        except (LookupError, PWError):
            return 0

    def candidates_for(self, el: ElementInfo, verify: bool = True) -> list[tuple[Locator, str]]:
        fr = el.frame
        raw: list[tuple[Locator, str]] = []
        if el.name and el.role in {"button", "link", "textbox", "combobox", "checkbox", "radio"}:
            raw.append((RoleLocator(role=el.role, name=el.name, frame=fr),
                        "accessible role+name: survives layout/markup changes; maps to desktop a11y APIs"))
        if el.row_label and el.role in {"textbox", "combobox"}:
            control = "select" if el.tag == "select" else ("textarea" if el.tag == "textarea" else "input")
            raw.append((RowLabelLocator(label=el.row_label, control=control, frame=fr),
                        "legacy form: no accessible name, anchored on the visible label in the same table row"))
        if el.role in {"link", "button"} and el.text:
            raw.append((TextLocator(text=el.text, frame=fr), "exact visible text"))
        if el.attr_name:
            raw.append((CssLocator(selector=f'{el.tag}[name="{el.attr_name}"]', frame=fr),
                        "form field name attribute: stable within a product version, opaque to humans"))
        if el.tag == "input" and el.input_type == "submit" and el.name:
            raw.append((CssLocator(selector=f'input[type="submit"][value="{el.name}"]', frame=fr), "submit value"))
        raw.append((CssLocator(selector=el.css, frame=fr), "structural path: last resort, breaks on any layout change"))
        seen, out = set(), []
        for loc, why in raw:
            key = describe_locator(loc)
            if key in seen:
                continue
            seen.add(key)
            if not verify:  # e.g. element captured from a human click; page has since moved on
                out.append((loc, why + " (unverified: recorded from operator action)"))
            elif self.count(loc) == 1:
                out.append((loc, why))
        return out

    # ------------------------------------------------------------------ action
    def resolve(self, target: list[Locator], timeout_ms: int) -> Resolution:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            tried = []
            for i, loc in enumerate(target):
                n = self.count(loc)
                tried.append((describe_locator(loc), n))
                if n == 1:
                    return Resolution(i, loc, tried)
            if time.monotonic() >= deadline:
                raise LocatorError("ambiguous" if any(n > 1 for _, n in tried) else "not_found", tried)
            self.page.wait_for_timeout(200)

    @contextlib.contextmanager
    def agent_acting(self):
        self._acting += 1
        try:
            yield
        finally:
            with contextlib.suppress(PWError):
                self.page.wait_for_timeout(150)  # let events raised by our own action drain
            self._acting -= 1

    def click(self, target, timeout_ms):
        r = self.resolve(target, timeout_ms)
        with self.agent_acting():
            self._loc(r.locator).click(timeout=timeout_ms)
        return r

    def fill(self, target, value, timeout_ms):
        r = self.resolve(target, timeout_ms)
        with self.agent_acting():
            loc = self._loc(r.locator)
            loc.fill(value, timeout=timeout_ms)
            # blur now so the field's 'change' event fires while automation owns the session,
            # not later when a human clicks elsewhere (which would mis-attribute it)
            loc.evaluate("el => el.blur()")
        return r

    def select(self, target, value, timeout_ms):
        r = self.resolve(target, timeout_ms)
        with self.agent_acting():
            self._loc(r.locator).select_option(label=value, timeout=timeout_ms)
        return r

    def _read_hits(self, loc: Locator) -> list[str]:
        if loc.frame is None:
            return [h for f in self._frames() for h in self._read_hits_in(f, loc)]
        f = self._frame(loc.frame)
        return self._read_hits_in(f, loc) if f is not None else []

    def _read_hits_in(self, f: Frame, loc: Locator) -> list[str]:
        if isinstance(loc, TableCellLocator):
            return f.evaluate(JS_TABLE_CELL, [loc.column, loc.row_match])
        if isinstance(loc, AnchorNextLocator):
            return f.evaluate(JS_ANCHOR_NEXT, loc.anchor_text)
        return [t.strip() for t in self._loc_in(f, loc).all_inner_texts()]

    def read(self, target, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            tried = []
            for i, loc in enumerate(target):
                try:
                    hits = self._read_hits(loc)
                except (LookupError, PWError):
                    hits = []
                tried.append((describe_locator(loc), len(hits)))
                if len(hits) == 1:
                    return hits[0], Resolution(i, loc, tried)
            if time.monotonic() >= deadline:
                raise LocatorError("ambiguous" if any(n > 1 for _, n in tried) else "not_found", tried)
            self.page.wait_for_timeout(200)

    def navigate(self, path: str) -> None:
        with self.agent_acting():
            self.page.goto(self._abs(path), wait_until="load")

    def current_url(self) -> str:
        # The work area iframe is where the app lives; report the deepest non-blank frame URL.
        urls = [f.url for f in self._frames() if f.url and f.url != "about:blank"]
        return urls[-1] if urls else self.page.url

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # ------------------------------------------------------------------ evidence
    def screenshot(self, path: Path) -> Path:
        """Screenshot with password fields and SSN-shaped text masked (element-level masking)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        ssn = re.compile(r"\d{3}-\d{2}-\d{4}")
        masks = [self.page.locator("input[type=password]"), self.page.get_by_text(ssn)]
        for f in self._frames():
            if f.name:
                fl = self.page.frame_locator(f'iframe[name="{f.name}"]')
                masks += [fl.locator("input[type=password]"), fl.get_by_text(ssn)]
        self.page.screenshot(path=str(path), mask=masks, full_page=True)
        return path

    def dom_snapshot(self, path: Path, redact: Callable[[str], str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        parts = []
        for f in self._frames():
            with contextlib.suppress(PWError):
                parts.append(f"<!-- frame name={f.name!r} url={f.url} -->\n{f.content()}")
        path.write_text(redact("\n\n".join(parts)))
        return path

    # ------------------------------------------------------------------ simulated operator input
    # Used only by ScriptedOperator: real input events on the live page, deliberately NOT wrapped
    # in agent_acting(), so they flow through the human-capture path exactly like a person's.
    def operator_click(self, target: list[Locator]) -> None:
        self._loc(self.resolve(target, 5000).locator).click()

    def operator_fill(self, target: list[Locator], value: str) -> None:
        loc = self._loc(self.resolve(target, 5000).locator)
        loc.fill(value)
        loc.press("Tab")  # blur -> 'change', as a person tabbing out of the field

    def operator_select(self, target: list[Locator], value: str) -> None:
        self._loc(self.resolve(target, 5000).locator).select_option(label=value)

    # ------------------------------------------------------------------ human capture
    def on_human_event(self, cb: HumanEventCallback) -> None:
        self._human_cbs.append(cb)

    def _on_binding(self, source, payload) -> None:
        if self._acting:
            return  # raised by our own automation
        frame = source.get("frame")
        payload["frame"] = (frame.name or None) if frame is not None else None
        payload["url"] = frame.url if frame is not None else self.page.url
        for cb in self._human_cbs:
            cb(payload)

    def element_from_capture(self, payload: dict) -> ElementInfo:
        return ElementInfo(index=0, frame=payload.get("frame"), **payload["el"])

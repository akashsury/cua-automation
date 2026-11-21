"""MockCore Member Services — a deliberately "legacy" credit-union back-office app.

This is the proxy target for the computer-use system. It imitates the things that make
real core-banking screens hard to automate:

* server-rendered pages, table-based layout, a work-area <iframe>, uppercase labels;
* no test IDs, no <label for=...>: inputs are only identifiable by the table row they sit in
  and by cryptic `name` attributes (f1, s1, n1, a1);
* real runtime conditions that can be injected on demand (see FAULTS below):
  not-found, validation errors, permission denial, an interstitial notice, session expiry,
  slow responses and outright server errors.

Two "tenants" (MOCKBANK_TENANT=a|b) run the same product with different branding and labels,
standing in for two institutions on the same vendor software.

All data is fictitious. Nothing here is a real institution, person or account.
"""

from __future__ import annotations

import html
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# --------------------------------------------------------------------------------------
# Tenant configuration (same vendor product, different configuration/branding)
# --------------------------------------------------------------------------------------

TENANTS: dict[str, dict[str, str]] = {
    "a": {
        "brand": "LAKESIDE COMMUNITY CU",
        "member_label": "Member Number:",
        "inquire_button": "Inquire",
        "open_share_link": "Open New Share",
    },
    "b": {
        "brand": "PRAIRIE FEDERAL CU",
        "member_label": "Acct/Member #:",
        "inquire_button": "Search",
        "open_share_link": "Add Share",
    },
}

USER = os.environ.get("MOCKBANK_USER", "teller01")
PASSWORD = os.environ.get("MOCKBANK_PASSWORD", "training-only-pw")


@dataclass
class Share:
    share_id: str
    type: str
    nickname: str
    balance: Decimal


@dataclass
class Member:
    member_id: str
    name: str
    ssn: str  # fictitious, used to exercise redaction
    since: str
    shares: list[Share] = field(default_factory=list)


def _seed() -> dict[str, Member]:
    return {
        "12345": Member(
            "12345", "JANE Q SAMPLE", "900-12-3456", "03/14/2011",
            [Share("0000", "SAVINGS", "PRIMARY SAVINGS", Decimal("1520.75")),
             Share("0010", "CHECKING", "EVERYDAY", Decimal("342.10"))],
        ),
        "23456": Member(
            "23456", "JOHN R TESTCASE", "900-65-4321", "07/02/2019",
            [Share("0000", "SAVINGS", "PRIMARY SAVINGS", Decimal("88.00"))],
        ),
    }


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.members = _seed()
        self.sessions: set[str] = set()
        self.fault_mode: str | None = None
        self.fault_remaining = 0
        self.confirmations = 0

    def take_fault(self, mode: str) -> bool:
        """Consume one occurrence of an injected fault if it is active."""
        with self.lock:
            if self.fault_mode == mode and self.fault_remaining > 0:
                self.fault_remaining -= 1
                if self.fault_remaining == 0:
                    self.fault_mode = None
                return True
            return False


FAULTS = {"session_timeout", "interstitial", "slow", "server_error", "permission_denied", "supervisor_prompt"}
STATE = State()
app = FastAPI(title="MockCore Member Services (proxy target)")


def tenant() -> dict[str, str]:
    return TENANTS.get(os.environ.get("MOCKBANK_TENANT", "a"), TENANTS["a"])


# --------------------------------------------------------------------------------------
# HTML helpers — intentionally old-fashioned markup
# --------------------------------------------------------------------------------------

STYLE = """
<style>
 body{font-family:Courier New,monospace;font-size:13px;background:#d4d0c8;margin:6px}
 table.frm td{padding:2px 6px} .err{color:#c00;font-weight:bold}
 .hdr{background:#000080;color:#fff;font-weight:bold;padding:4px}
 table.grid{border-collapse:collapse} table.grid td,table.grid th{border:1px solid #666;padding:2px 8px}
</style>"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<html><head><title>{html.escape(title)}</title>{STYLE}</head><body>"
        f"<table width='100%'><tr><td class='hdr'>{html.escape(title)}</td></tr></table>"
        f"{body}</body></html>"
    )


def login_form(message: str = "", next_url: str = "/") -> HTMLResponse:
    err = f"<p class='err'>{html.escape(message)}</p>" if message else ""
    return page(
        "SIGN ON",
        f"""{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(next_url)}">
<table class="frm">
 <tr><td>USER ID:</td><td><input type="text" name="u1" size="12"></td></tr>
 <tr><td>PASSWORD:</td><td><input type="password" name="p1" size="12"></td></tr>
 <tr><td></td><td><input type="submit" value="Sign On"></td></tr>
</table></form>""",
    )


def authed(request: Request) -> bool:
    return request.cookies.get("sid") in STATE.sessions


def guard(request: Request) -> HTMLResponse | None:
    """Common pre-processing for /app/* pages: auth + injected faults."""
    if STATE.take_fault("slow"):
        time.sleep(3)
    is_post = request.method == "POST"  # faults fire on submissions, i.e. mid-flow
    if is_post and STATE.take_fault("server_error"):
        return HTMLResponse(
            "<html><head><title>ERROR</title></head><body><h3>SYSTEM ERROR 0x5F</h3>"
            "<p>AN UNEXPECTED CONDITION OCCURRED. CONTACT HELP DESK.</p></body></html>",
            status_code=500,
        )
    if is_post and STATE.take_fault("session_timeout"):
        STATE.sessions.discard(request.cookies.get("sid", ""))
    if not authed(request):
        return login_form("SESSION EXPIRED - PLEASE SIGN ON AGAIN", next_url="/app/search")
    return None


def money(v: Decimal) -> str:
    return f"${v:,.2f}"


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=303)
    t = tenant()
    return HTMLResponse(
        f"""<html><head><title>MOCKCORE MEMBER SERVICES</title>{STYLE}</head><body>
<table width="100%" bgcolor="#000080"><tr>
 <td><font color="#ffffff"><b>MOCKCORE MEMBER SERVICES v3.2</b></font></td>
 <td align="right"><font color="#ffff00">{t['brand']}</font></td></tr></table>
<iframe name="workarea" src="/app/search" width="100%" height="560" frameborder="1"></iframe>
</body></html>"""
    )


@app.get("/login", response_class=HTMLResponse)
def login_get():
    return login_form()


@app.post("/login")
def login_post(u1: str = Form(""), p1: str = Form(""), next: str = Form("/")):
    if u1 != USER or p1 != PASSWORD:
        return login_form("INVALID USER ID OR PASSWORD", next_url=next)
    sid = secrets.token_hex(16)
    STATE.sessions.add(sid)
    target = next if next.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("sid", sid, httponly=True)
    return resp


@app.get("/app/search", response_class=HTMLResponse)
def search_get(request: Request):
    if (r := guard(request)) is not None:
        return r
    return search_form()


def search_form(message: str = "", value: str = "") -> HTMLResponse:
    t = tenant()
    err = f"<p class='err'>{html.escape(message)}</p>" if message else ""
    return page(
        "MEMBER INQUIRY",
        f"""{err}
<form method="post" action="/app/search">
<table class="frm">
 <tr><td>{t['member_label']}</td><td><input type="text" name="f1" size="8" value="{html.escape(value)}"></td></tr>
 <tr><td></td><td><input type="submit" value="{t['inquire_button']}"></td></tr>
</table></form>""",
    )


@app.post("/app/search")
def search_post(request: Request, f1: str = Form("")):
    if (r := guard(request)) is not None:
        return r
    f1 = f1.strip()
    if not re.fullmatch(r"\d{5}", f1):
        return search_form("INVALID MEMBER NUMBER - MUST BE 5 DIGITS", f1)
    if f1 not in STATE.members:
        return search_form(f"MBR NOT FOUND - {f1}", f1)
    if STATE.take_fault("interstitial"):
        return page(
            "SYSTEM NOTICE",
            f"""<p>SCHEDULED MAINTENANCE TONIGHT 23:00-01:00. SAVE YOUR WORK.</p>
<p><a href="/app/member/{f1}">Continue</a></p>""",
        )
    if STATE.take_fault("supervisor_prompt"):
        # An unexpected state that is NOT in the app profile's known conditions: replay cannot
        # classify it and must escalate to a human.
        return page(
            "SUPERVISOR OVERRIDE",
            f"""<p>INQUIRY ON THIS ACCOUNT REQUIRES SUPERVISOR ACKNOWLEDGEMENT (POLICY 7.2).</p>
<form method="get" action="/app/member/{f1}"><input type="submit" value="Acknowledge"></form>""",
        )
    return RedirectResponse(f"/app/member/{f1}", status_code=303)


@app.get("/app/member/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    if (r := guard(request)) is not None:
        return r
    m = STATE.members.get(member_id)
    if m is None:
        return search_form(f"MBR NOT FOUND - {member_id}", member_id)
    t = tenant()
    rows = "".join(
        f"<tr><td>{s.share_id}</td><td>{s.type}</td><td>{s.nickname}</td>"
        f"<td align='right'>{money(s.balance)}</td></tr>"
        for s in m.shares
    )
    return page(
        "MEMBER DETAIL",
        f"""
<table class="frm">
 <tr><td>MEMBER:</td><td>{m.member_id}</td></tr>
 <tr><td>NAME:</td><td>{m.name}</td></tr>
 <tr><td>SSN:</td><td>{m.ssn}</td></tr>
 <tr><td>MEMBER SINCE:</td><td>{m.since}</td></tr>
</table>
<br>
<table class="grid">
 <tr><th>SHARE</th><th>TYPE</th><th>NICKNAME</th><th>BALANCE</th></tr>
 {rows}
</table>
<p><a href="/app/member/{m.member_id}/newshare">{t['open_share_link']}</a> &nbsp;
   <a href="/app/search">Back to Inquiry</a></p>""",
    )


@app.get("/app/member/{member_id}/newshare", response_class=HTMLResponse)
def newshare_get(request: Request, member_id: str):
    if (r := guard(request)) is not None:
        return r
    if STATE.take_fault("permission_denied"):
        return page("ERROR", "<p class='err'>NOT AUTHORIZED FOR THIS FUNCTION (SEC-403)</p>")
    if member_id not in STATE.members:
        return search_form(f"MBR NOT FOUND - {member_id}", member_id)
    return newshare_form(member_id)


def newshare_form(member_id: str, message: str = "") -> HTMLResponse:
    err = f"<p class='err'>{html.escape(message)}</p>" if message else ""
    return page(
        "OPEN NEW SHARE",
        f"""{err}
<form method="post" action="/app/member/{member_id}/newshare">
<table class="frm">
 <tr><td>MEMBER:</td><td>{member_id}</td></tr>
 <tr><td>SHARE TYPE:</td><td><select name="s1">
   <option value="">--SELECT--</option><option>SAVINGS</option>
   <option>MONEY MARKET</option><option>CERTIFICATE</option></select></td></tr>
 <tr><td>NICKNAME:</td><td><input type="text" name="n1" size="20"></td></tr>
 <tr><td>INITIAL DEPOSIT:</td><td><input type="text" name="a1" size="10"></td></tr>
 <tr><td></td><td><input type="submit" value="Continue"> <a href="/app/member/{member_id}">Cancel</a></td></tr>
</table></form>""",
    )


@app.post("/app/member/{member_id}/newshare")
def newshare_post(request: Request, member_id: str, s1: str = Form(""), n1: str = Form(""), a1: str = Form("")):
    if (r := guard(request)) is not None:
        return r
    if s1 not in {"SAVINGS", "MONEY MARKET", "CERTIFICATE"}:
        return newshare_form(member_id, "SHARE TYPE REQUIRED")
    try:
        amount = Decimal(a1.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return newshare_form(member_id, "INVALID AMOUNT")
    if amount < 5:
        return newshare_form(member_id, "MINIMUM OPENING DEPOSIT IS $5.00")
    return page(
        "REVIEW NEW SHARE",
        f"""<p>PLEASE REVIEW. THIS ACTION CANNOT BE UNDONE.</p>
<form method="post" action="/app/member/{member_id}/newshare/confirm">
<input type="hidden" name="s1" value="{html.escape(s1)}">
<input type="hidden" name="n1" value="{html.escape(n1)}">
<input type="hidden" name="a1" value="{amount}">
<table class="grid">
 <tr><td>MEMBER</td><td>{member_id}</td></tr>
 <tr><td>SHARE TYPE</td><td>{html.escape(s1)}</td></tr>
 <tr><td>NICKNAME</td><td>{html.escape(n1.upper())}</td></tr>
 <tr><td>INITIAL DEPOSIT</td><td>{money(amount)}</td></tr>
</table><br>
<input type="submit" value="Confirm"> <a href="/app/member/{member_id}/newshare">Back</a>
</form>""",
    )


@app.post("/app/member/{member_id}/newshare/confirm")
def newshare_confirm(request: Request, member_id: str, s1: str = Form(""), n1: str = Form(""), a1: str = Form("0")):
    if (r := guard(request)) is not None:
        return r
    m = STATE.members.get(member_id)
    if m is None:
        return search_form(f"MBR NOT FOUND - {member_id}", member_id)
    with STATE.lock:
        STATE.confirmations += 1
        new_id = f"{(len(m.shares) + 1) * 10:04d}"
        m.shares.append(Share(new_id, s1, n1.upper(), Decimal(a1)))
        conf = f"C-{70000 + STATE.confirmations}"
    return page(
        "SHARE OPENED",
        f"""<p>NEW SHARE {new_id} OPENED FOR MEMBER {member_id}.</p>
<table class="frm"><tr><td>CONFIRMATION NO:</td><td>{conf}</td></tr></table>
<p><a href="/app/member/{member_id}">Return to Member</a></p>""",
    )


# --------------------------------------------------------------------------------------
# Test hooks (not part of the "product"): fault injection and reset.
# --------------------------------------------------------------------------------------


@app.post("/__faults")
async def set_fault(request: Request):
    body = await request.json()
    mode = body.get("mode")
    if mode not in FAULTS and mode is not None:
        return JSONResponse({"error": f"unknown fault {mode}", "known": sorted(FAULTS)}, status_code=400)
    with STATE.lock:
        STATE.fault_mode = mode
        STATE.fault_remaining = int(body.get("count", 1)) if mode else 0
    return {"mode": STATE.fault_mode, "remaining": STATE.fault_remaining}


@app.post("/__reset")
def reset():
    with STATE.lock:
        STATE.members = _seed()
        STATE.fault_mode = None
        STATE.fault_remaining = 0
    return {"ok": True}

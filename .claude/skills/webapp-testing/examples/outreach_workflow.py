#!/usr/bin/env python3
"""Drive this app end to end: log in, run the planner, triage, read a trace.

Run as-is to smoke the whole workflow, or lift the pieces. Everything here was
executed against the real app -- the selectors and the login dance are the ones
that actually work, not a sketch of them.

    python .claude/skills/webapp-testing/scripts/with_server.py \
        --server "python manage.py runserver 8137 --noreload" --port 8137 \
        -- python .claude/skills/webapp-testing/examples/outreach_workflow.py

Prerequisites the app enforces and this script does not paper over:

* ``LOGIN_ALLOWED_EMAILS`` in ``.env`` must contain the address below, or
  ``request-link`` returns 200 and mints nothing (deliberate: the endpoint
  never reveals whether an address is allowed).
* ``DJANGO_DEBUG=True`` puts ``dev_link`` in the response body. Without it the
  link only reaches the server log and this script cannot log in.
* The planner refuses to run with no provider key -- the button renders
  *disabled* with "No API key configured". That is the app working, not a
  broken selector. Set a provider key first, and expect real spend.
* ``OUTREACH_AGENT_ENABLED=1`` for the agent path; default is off.
"""

import json
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8137"
EMAIL = "bd@example.com"
SHOTS = "/tmp"


def mint_login_token() -> str:
    """POST the magic-link endpoint and pull the token out of ``dev_link``."""
    request = urllib.request.Request(
        f"{BASE}/api/auth/request-link/",
        data=json.dumps({"email": EMAIL}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        payload = json.load(response)
    link = payload.get("dev_link")
    if not link:
        sys.exit(f"no dev_link in {payload} -- check LOGIN_ALLOWED_EMAILS and DJANGO_DEBUG")
    return re.search(r"token=([^&]+)", link).group(1)


# Session-authenticated POST from page context. DRF's session auth needs the
# CSRF header, which the cookie carries -- a bare fetch() gets a 403.
POST_FROM_PAGE = """async ([url, body]) => {
  const csrf = document.cookie.split('; ').find(c => c.startsWith('csrftoken='));
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json',
              'X-CSRFToken': csrf ? csrf.split('=')[1] : ''},
    credentials: 'same-origin',
    body: JSON.stringify(body),
  });
  return {status: r.status, body: await r.text()};
}"""


def main() -> int:
    token = mint_login_token()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})

        # Always attach these before navigating: a silent React crash renders a
        # blank page that a screenshot alone will not explain.
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None,
        )

        # 1. Log in. Consuming the token establishes the session cookie and
        #    redirects to /inbox.
        page.goto(f"{BASE}/auth/consume?token={token}")
        page.wait_for_load_state("networkidle")
        print("logged in ->", page.url)

        # 2. Run the planner.
        page.goto(f"{BASE}/")
        page.wait_for_load_state("networkidle")
        run = page.get_by_role("button", name="Run Outreach Plan")
        if run.is_disabled():
            print("planner disabled -- no provider key configured; stopping here")
            browser.close()
            return 0
        # Generous timeout: this is one provider call per lead, not a page load.
        with page.expect_response(lambda r: "/api/outreach/run/" in r.url, timeout=600_000) as got:
            run.click()
        print("POST /api/outreach/run/ ->", got.value.status)

        # 3. Triage. Leads are buttons in the left rail labelled "Name\nP1 · reason".
        page.goto(f"{BASE}/inbox")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        page.screenshot(path=f"{SHOTS}/inbox.png", full_page=True)
        leads = [
            (b.inner_text() or "").strip().split("\n")[0]
            for b in page.locator("button").all()
            if "·" in (b.inner_text() or "")
        ]
        print(f"{len(leads)} leads queued:", leads[:5])

        if leads:
            page.get_by_role("button", name=leads[0], exact=False).first.click()
            page.wait_for_timeout(600)
            approve = page.get_by_role("button", name="Approve & copy")
            if approve.count() and not approve.is_disabled():
                with page.expect_response(lambda r: "/approve/" in r.url) as got:
                    approve.click()
                print(f"approved {leads[0]} ->", got.value.status)

        # 4. The per-lead agent trace, on the reports page.
        page.goto(f"{BASE}/reports")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        trace = page.get_by_text("How this draft was reached", exact=False)
        print("trace toggles:", trace.count())
        if trace.count():
            trace.first.click()
            page.wait_for_timeout(1000)
        page.screenshot(path=f"{SHOTS}/reports.png", full_page=True)

        # 5. Endpoints with no UI affordance -- drive them from page context so
        #    they carry the session.
        for label, body in [
            ("unknown run id", {"resume_run_id": "no-such-run"}),
            ("no resume", {}),
        ]:
            result = page.evaluate(POST_FROM_PAGE, ["/api/outreach/run/", body])
            print(f"  {label}: {result['status']} {result['body'][:80]}")

        print("problems:", problems or "none")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

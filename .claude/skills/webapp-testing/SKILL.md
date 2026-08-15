---
name: webapp-testing
description: Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs.
---

# Web Application Testing

To test local web applications, write native Python Playwright scripts.

## Setup (once per environment)

Playwright is **not** in `requirements-dev.txt` — it is a testing tool, not a
runtime or CI dependency, and the browser download is ~150MB. Install it into
the project venv before the first run:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

`ModuleNotFoundError: No module named 'playwright'` means this step was skipped.

**Helper Scripts Available**:
- `scripts/with_server.py` - Manages server lifecycle (supports multiple servers)

**Always run scripts with `--help` first** to see usage. DO NOT read the source until you try running the script first and find that a customized solution is abslutely necessary. These scripts can be very large and thus pollute your context window. They exist to be called directly as black-box scripts rather than ingested into your context window.

## Decision Tree: Choosing Your Approach

```
User task → Is it static HTML?
    ├─ Yes → Read HTML file directly to identify selectors
    │         ├─ Success → Write Playwright script using selectors
    │         └─ Fails/Incomplete → Treat as dynamic (below)
    │
    └─ No (dynamic webapp) → Is the server already running?
        ├─ No → Run: python scripts/with_server.py --help
        │        Then use the helper + write simplified Playwright script
        │
        └─ Yes → Reconnaissance-then-action:
            1. Navigate and wait for networkidle
            2. Take screenshot or inspect DOM
            3. Identify selectors from rendered state
            4. Execute actions with discovered selectors
```

## Example: Using with_server.py

To start a server, run `--help` first, then use the helper:

**Single server:**
```bash
python scripts/with_server.py --server "npm run dev" --port 5173 -- python your_automation.py
```

**Multiple servers (e.g., backend + frontend):**
```bash
python scripts/with_server.py \
  --server "cd backend && python server.py" --port 3000 \
  --server "cd frontend && npm run dev" --port 5173 \
  -- python your_automation.py
```

Use it rather than backgrounding a server yourself. A dev server started from
the shell outlives the turn that started it, so the next run finds the port
taken and quietly drives the *previous* build — a failure that looks like a
pass. `with_server.py` refuses to start when the port is already occupied, and
takes the servers down however the command exits.

`--server` is run through a shell, so quote any path containing spaces
(`--server "'$PWD/.venv/bin/python' manage.py runserver 8137 --noreload"`).
When a server dies during startup the script prints its exit code and the tail
of its log, which is usually the whole diagnosis.

To create an automation script, include only Playwright logic (servers are managed automatically):
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True) # Always launch chromium in headless mode
    page = browser.new_page()
    page.goto('http://localhost:5173') # Server already running and ready
    page.wait_for_load_state('networkidle') # CRITICAL: Wait for JS to execute
    # ... your automation logic
    browser.close()
```

## Reconnaissance-Then-Action Pattern

1. **Inspect rendered DOM**:
   ```python
   page.screenshot(path='/tmp/inspect.png', full_page=True)
   content = page.content()
   page.locator('button').all()
   ```

2. **Identify selectors** from inspection results

3. **Execute actions** using discovered selectors

## Common Pitfall

❌ **Don't** inspect the DOM before waiting for `networkidle` on dynamic apps
✅ **Do** wait for `page.wait_for_load_state('networkidle')` before inspection

## Best Practices

- **Use bundled scripts as black boxes** - To accomplish a task, consider whether one of the scripts available in `scripts/` can help. These scripts handle common, complex workflows reliably without cluttering the context window. Use `--help` to see usage, then invoke directly. 
- Use `sync_playwright()` for synchronous scripts
- Always close the browser when done
- Use descriptive selectors: `text=`, `role=`, CSS selectors, or IDs
- Add appropriate waits: `page.wait_for_selector()` or `page.wait_for_timeout()`

## Driving *this* app

The four things that will otherwise cost you a round trip each. All verified
against the running app.

**1. Logging in.** Every page is behind a magic-link session. There is no
password form to fill; mint a token over HTTP and consume it:

```bash
curl -s -X POST http://127.0.0.1:8137/api/auth/request-link/ \
  -H 'Content-Type: application/json' -d '{"email":"bd@example.com"}'
# {"status":"sent", ..., "dev_link":"...?token=<TOKEN>"}
```

then `page.goto(f"{BASE}/auth/consume?token={TOKEN}")`, which sets the session
cookie and redirects to `/inbox`. Two `.env` preconditions, both silent when
unmet: the address must be in `LOGIN_ALLOWED_EMAILS` (otherwise the endpoint
still returns 200 and mints nothing — deliberate, it never reveals whether an
address is allowed), and `DJANGO_DEBUG=True` is what puts `dev_link` in the
body at all. Tokens are single-use: mint a fresh one per script run.

**2. The planner will not run without a provider key.** "Run Outreach Plan"
renders *disabled* next to "No API key configured". That is the app working —
do not go hunting for a broken selector. A real run means real provider spend,
so get the user's go-ahead first, and expect a free tier to rate-limit partway
through and leave most leads with empty drafts.

**3. Endpoints with no UI affordance** (resume, and anything else you want to
poke) must be driven from page context so they carry the session — and DRF's
session auth needs the CSRF header, so a bare `fetch()` gets a 403. The working
snippet is `POST_FROM_PAGE` in `examples/outreach_workflow.py`.

**4. Seed first.** `python scripts/populate_demo_data.py` is the single source
of demo state; an unseeded database renders "Nothing to triage" and every
selector below will miss.

## Reference Files

- **examples/outreach_workflow.py** — the whole workflow end to end: magic-link
  login, planner run, triage selection and approve, the per-lead agent trace,
  and session-authenticated POSTs. Lift the pieces; the selectors are the ones
  that actually work.
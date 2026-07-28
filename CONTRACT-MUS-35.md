# FROZEN INTERFACE CONTRACT — MUS-35 (tickets MUS-36 … MUS-42)

**Base commit:** `origin/master` @ `9c35079` (after PR #15). Every branch is cut from
`feature/mus-35-product-ui`, which is that commit plus this file.

Nothing in this document may be "improved" by an agent. If something here is wrong, it stays
wrong until integration — a consistent wrong contract merges; seven locally-correct contracts do
not. If you believe a section is genuinely unbuildable, say so in your PR description and build
it as written anyway.

This is the same mechanism `CONTRACT.md` used for the earlier parallel build. That contract was
correct and still produced an integration-only bug, because it named things without pinning their
*edges*. §9 is this contract's edges.

---

## 0. Ground truth about the base you are building on

Facts every agent needs and cannot see from their own ticket:

| Fact | Value |
|---|---|
| Django / DRF | 4.2.30 / 3.16.1 |
| Latest migration leaf | `app.0003_llmprovider_llmmodel_llmconfiguration_and_more` |
| DRF global defaults | **none** — no `REST_FRAMEWORK` dict exists in `project/settings.py` today. Every endpoint is `AllowAny` except the `/api/llm/config/` views, which set `authentication_classes = [LLMAdminBasicAuthentication]` explicitly |
| Existing API tests hitting `self.client` | 41 in `project/app/tests_api.py`, 8 in `project/app/tests_llm.py` |
| Coverage gate | `fail_under = 90` in `pyproject.toml` — CI fails for *everyone* if any branch lands untested code |
| Lint | `ruff check .` + `ruff format --check .`, `select = ["E","F","I","W"]`, `line-length = 100` |
| Typecheck | `mypy project/app/services/` only |
| Golden rules eval | `evals/golden/leads.jsonl` = 64 lines / **41 records** (the rest are `//` comments). `evals/baselines/rules.json` pins `action_accuracy 0.9512`, `n: 41`, frozen `TODAY = 2026-06-12` |
| Frontend bundle | committed at `project/app/static/frontend/assets/index.js` + `index.css`, fixed filenames, built by `cd frontend && npm run build` |
| SPA shells | Django views in `project/app/views_frontend.py`, all `@ensure_csrf_cookie`, all extending `app/spa_base.html`. **There is no catch-all** — every React route needs a Django URL or a hard refresh 404s |
| API view style | plain `APIView` throughout. No routers, no ViewSets, no pagination. Follow it |

`determine_action` is called with **2-tuple unpacking** in four places outside `outreach.py`:
`evals/run_rules_eval.py:204`, `evals/run_redteam_eval.py:123`, `project/app/tests_logic.py` (×8),
`project/app/tests_redteam.py` (×6, via `[0]`). Its arity is therefore immutable.

---

## 0.1 Tickets, waves, and merge order

Seven tickets. MUS-42 was split out of MUS-39 because the instrumentation layer is a refactor of
the two purest modules in the codebase and a bad fit for the agent building a state machine and
nine endpoints.

| Ticket | Wave | Area |
|---|---|---|
| MUS-36 | 1 | [FE] Design tokens, type scale, dark/light, UI primitives |
| MUS-37 | 1 | [BE] Magic-link auth, global `IsAuthenticated` |
| MUS-42 | 1 | [BE] Structured rule trace + verification spans (services only) |
| MUS-39 | 1 | [BE] Triage queue state model + API |
| MUS-38 | 2 | [FE] Sign-in flow, route guard |
| MUS-40 | 2 | [FE] Triage inbox |
| MUS-41 | 2 | [FE] Done view |

**Wave 1 runs all four in parallel.** Wave 2 starts after MUS-36 merges, so the three FE screens
inherit real tokens.

**Merge order into `feature/mus-35-product-ui` is a hard gate, not a suggestion:**

```
MUS-36  →  MUS-37  →  MUS-42  →  MUS-39  →  MUS-38  →  MUS-40  →  MUS-41
(tokens)   (auth BE)  (services) (queue BE) (auth FE)  (inbox)    (done)
```

Each ticket rebases onto the feature branch before merging. The order is load-bearing: it is what
makes the migration graph, `models.py`, `settings.py`, `project/urls.py`, `frontend/src/main.tsx`
and `project/app/urls.py` resolvable mechanically instead of by judgement.

Working in parallel and merging in order are not in tension. Build against this contract; do not
wait for the branch ahead of you.

### Mini PRs

Each ticket decomposes into small branches PR'd **into its own ticket branch**, so every review has
tight scope. Naming: `musansht/mus-NN-<letter>-<slug>`, based on the ticket branch.

Ticket branch names are Linear's `gitBranchName` verbatim. Commits are
`type(scope): subject (MUS-NN)`, one per logical step.

---

## 1. Migration ownership and numbering

### 1.1 Reservation table

| Number | Filename (exact) | Owner | Contents |
|---|---|---|---|
| `0004` | `project/app/migrations/0004_logintoken.py` | **MUS-37** | `CreateModel LoginToken` |
| `0005` | `project/app/migrations/0005_triage_queue.py` | **MUS-39** | `AddField` ×10 on `OutreachAction`, `CreateModel DismissedOutreachKey`, `CreateModel OutreachEdit`, `AlterModelOptions` |

**No other ticket may create a migration or run `makemigrations`.** MUS-36/38/40/41/42 branches must
produce `No changes detected` from `python manage.py makemigrations --check --dry-run`.

MUS-42 has no migration by design: it changes only pure functions. The JSONFields that hold its
output (`rule_trace`, `verification`) are owned by MUS-39, so that every `OutreachAction` schema
change lands in exactly one migration.

### 1.2 The resolution mechanism (explicit)

The failure being avoided is `Conflicting migrations detected; multiple leaf nodes in the migration
graph`, which happens the moment two migrations both declare `dependencies = [("app", "0003_…")]`
and neither depends on the other.

**Mechanism: reserved numbers + a single pinned dependency repoint at rebase time.**

1. **MUS-37** authors `0004_logintoken.py` with
   `dependencies = [("app", "0003_llmprovider_llmmodel_llmconfiguration_and_more")]`. Final; never
   changes.

2. **MUS-39** runs `python manage.py makemigrations app --name triage_queue`, which on its branch
   emits `0004_triage_queue.py`. MUS-39 then does exactly two things and no others:
   - `git mv project/app/migrations/0004_triage_queue.py project/app/migrations/0005_triage_queue.py`
   - leaves the `0003` dependency in place **while developing**, so its branch migrates and CI-passes
     standalone, with this exact comment above it:

   ```python
   class Migration(migrations.Migration):
       # CONTRACT MUS-39: this dependency is repointed to ("app", "0004_logintoken")
       # in the rebase-onto-feature-branch commit, once MUS-37 has merged. Do not
       # merge this file with the 0003 dependency still in place.
       dependencies = [("app", "0003_llmprovider_llmmodel_llmconfiguration_and_more")]
   ```

3. **At rebase** (MUS-39 rebases after MUS-37 and MUS-42 merge — mandated by the merge order),
   MUS-39 changes that one line to `dependencies = [("app", "0004_logintoken")]` and deletes the
   comment. The filename already encodes the order, so nothing else moves.

4. **Acceptance for that rebase commit** — all three must pass before MUS-39 updates its PR:

   ```bash
   python manage.py migrate                            # clean DB, applies 0001..0005
   python manage.py makemigrations --check --dry-run   # "No changes detected"
   python manage.py showmigrations app                 # single linear chain, one leaf
   ```

### 1.3 Explicitly banned

- `makemigrations --merge`. Non-deterministic filename, two permanent parallel leaves, unreviewable.
  If you see two leaves, you repointed wrong — fix the dependency, do not merge-migrate.
- Renumbering an already-pushed migration. Squashing. Editing `0001`–`0003`.

### 1.4 CI guard (owned by MUS-37)

One step added to the `test` job in `.github/workflows/ci.yml`, immediately before the coverage step:

```yaml
      - name: Check for missing migrations
        run: python manage.py makemigrations --check --dry-run
```

No other ticket edits `ci.yml`. The frontend job in §8.2 is added by the **integrator**, not by a
ticket agent.

---

## 2. Model field definitions

### 2.1 Where models live

All new model classes go in `project/app/models.py`, **appended at end of file**, in this order:

```
… existing: Lead, Event, OutreachAction, LLMProvider, LLMModel, LLMConfiguration, ReviewDecision
[MUS-37 block]  LoginToken
[MUS-39 block]  DismissedOutreachKey, OutreachEdit
```

MUS-39's edits to `OutreachAction` are confined to the existing class body (mid-file); its new
classes are appended after MUS-37's `LoginToken`. Because MUS-37 merges first and MUS-39 rebases,
this is a clean append. If a conflict surfaces anyway: **keep both hunks, in ticket-number order.**
Never reorder, never interleave.

### 2.2 `LoginToken` — MUS-37, verbatim

```python
class LoginToken(models.Model):
    """A single-use, short-lived magic-link login token (MUS-37).

    The raw token is NEVER stored: ``secrets.token_urlsafe(32)`` is generated,
    emailed/printed, and only ``sha256(token).hexdigest()`` is persisted. A
    plain SHA-256 (not a slow KDF) is correct here because the token is 256
    bits of CSPRNG output -- there is no dictionary to attack, and the verify
    path must stay cheap enough to be rate-limited rather than DoS'd.

    Single-use is enforced by a conditional UPDATE (see views_auth.py), not by
    read-then-write, so two concurrent consumes cannot both succeed.
    """

    email = models.EmailField(db_index=True)
    # sha256 hexdigest of the raw token -- 64 chars, unique so a replayed
    # insert fails loudly at the DB rather than silently.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    # NULL until redeemed; set exactly once by the conditional update.
    consumed_at = models.DateTimeField(null=True, blank=True, default=None)
    # Best-effort audit only -- never used for authorization decisions.
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "-created_at"], name="logintoken_email_recent"),
            models.Index(fields=["expires_at", "consumed_at"], name="logintoken_sweep"),
        ]

    def __str__(self):
        return f"LoginToken({self.email}, expires {self.expires_at:%Y-%m-%d %H:%M})"
```

### 2.3 New `OutreachAction` fields — MUS-39, verbatim

Inserted **after** `further_action` and **before** `def __str__`. `suggested_copy` is not touched:
it is immutable after creation, forever.

```python
    # ---- Triage queue (MUS-39) --------------------------------------------
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_SNOOZED = "snoozed"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_SNOOZED, "Snoozed"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    TRIGGER_TOMORROW = "tomorrow"
    TRIGGER_IN_3_DAYS = "in_3_days"
    TRIGGER_NEXT_WEEK = "next_week"
    TRIGGER_CUSTOM = "custom"
    TRIGGER_ON_ACTIVITY = "on_activity"
    SNOOZE_TRIGGER_CHOICES = [
        (TRIGGER_TOMORROW, "Tomorrow"),
        (TRIGGER_IN_3_DAYS, "In 3 days"),
        (TRIGGER_NEXT_WEEK, "Next week"),
        (TRIGGER_CUSTOM, "Custom date"),
        (TRIGGER_ON_ACTIVITY, "When they do something"),
    ]

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    # Set on EVERY status write, including a re-snooze of an already-snoozed
    # item. Drives both the undo window and the reverse-chron /done ordering.
    status_changed_at = models.DateTimeField(null=True, blank=True, default=None)

    # The reviewer's edit of `suggested_copy`. "" means never edited.
    # `suggested_copy` is IMMUTABLE -- the eval corpus depends on being able to
    # diff the model's original output against what a human actually sent.
    edited_copy = models.TextField(blank=True, default="")

    # Always non-NULL while status == "snoozed", including for on_activity
    # (which uses a backstop -- see TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS),
    # so the unsnooze sweep and the FE ordering never need a NULL branch.
    snooze_until = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    snooze_trigger = models.CharField(
        max_length=16, choices=SNOOZE_TRIGGER_CHOICES, blank=True, default=""
    )
    # Watermark for trigger == "on_activity": the item returns to the queue as
    # soon as the lead has an Event with timestamp > this value. Captured at
    # snooze time so later events (not historical ones) are what wake it.
    snooze_activity_after = models.DateTimeField(null=True, blank=True, default=None)

    dismiss_reason = models.CharField(max_length=64, blank=True, default="")

    # Stable identity of "this recommendation for this lead", used to suppress
    # a permanently dismissed recommendation on later plan_outreach() runs and
    # to stop a re-run duplicating an already-open item. See DismissedOutreachKey.
    dedupe_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Structured rule trace snapshot (schema v1, section 3), produced by
    # MUS-42's services/outreach.py::explain(). A SNAPSHOT: never recomputed
    # after creation, because every relative figure in it ("28d since last
    # contact") is only true as of `trace.today`.
    rule_trace = models.JSONField(default=dict, blank=True)

    # Verification report (schema v1, section 4) for `effective_copy`, produced
    # by MUS-42's services/verify.py::verify_spans(). Rewritten on every /edit/
    # and /approve/; regenerated, not appended.
    verification = models.JSONField(default=dict, blank=True)
```

Plus a `Meta` on `OutreachAction` (the class currently has none — MUS-39 adds it directly above
`def __str__`):

```python
    class Meta:
        indexes = [
            models.Index(fields=["status", "priority", "lead"], name="oa_queue_order"),
            models.Index(fields=["status", "-status_changed_at"], name="oa_done_order"),
        ]
```

### 2.4 `DismissedOutreachKey` — MUS-39, verbatim

```python
class DismissedOutreachKey(models.Model):
    """Permanent suppression ledger for dismissed recommendations (MUS-39).

    Dismiss means "never show me this again". `plan_outreach()` consults this
    table BEFORE generating copy, so a dismissed recommendation costs no LLM
    call on a re-run and creates no row to filter out downstream.

    TABLE, not a JSONField on Lead, because:
      * it needs a UNIQUE index on `dedupe_key` -- the whole point is that a
        second dismissal of the same recommendation is a no-op, enforced by
        the DB rather than by read-modify-write;
      * `plan_outreach()` reads the entire active set once per run
        (`values_list("dedupe_key", flat=True)`) -- one query, O(1) in leads;
      * undo must REVOKE a dismissal, and a revocation needs its own timestamp
        and audit trail, which a blob cannot carry;
      * it outlives the OutreachAction row that created it (SET_NULL), so
        pruning old actions cannot silently resurrect dismissed work.
    """

    # sha256("v1|{lead_id}|{action_type}").hexdigest(). See services/dedupe.py.
    dedupe_key = models.CharField(max_length=128, unique=True, db_index=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="dismissed_keys")
    action_type = models.CharField(max_length=64)
    reason = models.CharField(max_length=64, blank=True, default="")
    dismissed_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.EmailField(blank=True, default="")
    source_action = models.ForeignKey(
        OutreachAction, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Set by undo-of-dismiss. A revoked row no longer suppresses, but is kept
    # so the audit trail shows the dismissal happened and was reversed.
    revoked_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-dismissed_at"]

    def __str__(self):
        return f"dismissed {self.lead_id}/{self.action_type}"
```

### 2.5 `OutreachEdit` — MUS-39, verbatim

```python
class OutreachEdit(models.Model):
    """Append-only log of reviewer edits to generated copy (MUS-39).

    This is the eval corpus: "what did the model write, what did the human
    actually send, and what changed" is the only honest signal we have about
    copy quality, and it is only capturable at the moment of editing.

    TABLE, not a JSONField on OutreachAction, because:
      * it is 1:N -- a reviewer typically edits, re-verifies, edits again, then
        approves, and every intermediate state is corpus-worthy;
      * it is exported by date range for offline scoring, which is a query,
        not a blob scan;
      * the queue list endpoint has a CONSTANT-query-count requirement, and
        diffs are large -- keeping them off the hot row means the queue payload
        never carries them.

    The `diff_ops` payload itself IS a JSONField: it is opaque to SQL, always
    read whole, and its internal shape is versioned rather than queried.
    """

    outreach_action = models.ForeignKey(
        OutreachAction, on_delete=models.CASCADE, related_name="edits"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    editor = models.EmailField(blank=True, default="")

    # Full before/after so the corpus is self-contained and survives any later
    # change to the diff algorithm.
    before_text = models.TextField()
    after_text = models.TextField()

    # difflib.SequenceMatcher opcodes, non-"equal" only. Schema v1:
    # [{"op":"replace","a0":312,"a1":330,"b0":312,"b1":341,
    #   "before":"volume pricing","after":"volume pricing tiers"}, ...]
    diff_ops = models.JSONField(default=list, blank=True)

    chars_added = models.IntegerField(default=0)
    chars_removed = models.IntegerField(default=0)
    # difflib.SequenceMatcher(None, before, after).ratio(), 0.0-1.0.
    similarity = models.FloatField(default=1.0)
    # True for the edit that was in place at the moment of approval.
    committed = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"edit of action {self.outreach_action_id} @ {self.created_at:%Y-%m-%d %H:%M}"
```

### 2.6 Dedupe key — the documented definition

`project/app/services/dedupe.py` (new file, sole owner MUS-39):

```python
DEDUPE_VERSION = "v1"

def dedupe_key(lead_id: str, action_type: str) -> str:
    """Stable identity of a recommendation.

    KEY = sha256("v1|{lead_id}|{action_type}").hexdigest()

    Deliberately scoped to (lead, action_type) and NOT to the reason text or
    the rule trace. The product promise is "dismiss is permanent": if a
    reviewer says "stop asking me to nudge this lead's usage", a re-run that
    computes a marginally different reason string must not resurrect it.

    It is scoped to action_type (rather than lead alone) so a genuinely
    different situation still surfaces: dismissing `nudge_usage` for lead_007
    does not suppress a later `reengage_dormant` for the same lead.

    Bumping DEDUPE_VERSION intentionally un-suppresses everything and is a
    deliberate, reviewed act -- never a side effect.
    """
```

`plan_outreach()` gains exactly two behaviours, both keyed off this:

1. **Suppression** — before copy generation:
   `if key in <set of DismissedOutreachKey where revoked_at IS NULL>: continue` (set built once per run).
2. **No-duplicate** — an *open* item wins:
   `if OutreachAction.objects.filter(dedupe_key=key, status__in=("pending","snoozed")).exists(): continue`.

Both are no-ops on a virgin database, so existing `tests_api.py` behaviour is unchanged.

### 2.7 New settings — pinned names

`project/settings.py`, appended at EOF in this order. MUS-37's block first, MUS-39's second.

```python
# --- Magic-link auth (MUS-37) -------------------------------------------------
LOGIN_ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("LOGIN_ALLOWED_EMAILS", "").split(",")
    if e.strip()
}
LOGIN_LINK_DELIVERY = os.environ.get("LOGIN_LINK_DELIVERY", "console")  # console | email
LOGIN_TOKEN_TTL_SECONDS = int(os.environ.get("LOGIN_TOKEN_TTL_SECONDS", "900"))
LOGIN_LINK_BASE_URL = os.environ.get("LOGIN_LINK_BASE_URL", "http://127.0.0.1:8000")
LOGIN_RATE_LIMIT_EMAIL = os.environ.get("LOGIN_RATE_LIMIT_EMAIL", "5/hour")
LOGIN_RATE_LIMIT_IP = os.environ.get("LOGIN_RATE_LIMIT_IP", "20/hour")
LOGIN_RESEND_COOLDOWN_SECONDS = int(os.environ.get("LOGIN_RESEND_COOLDOWN_SECONDS", "30"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "project.app.authentication.SessionAuthenticationWith401",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "project.app.exceptions.contract_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "auth_request_ip": LOGIN_RATE_LIMIT_IP,
        "auth_consume_ip": "60/hour",
        "queue_verify": "120/min",
    },
}

# --- Triage queue (MUS-39) ----------------------------------------------------
TRIAGE_UNDO_WINDOW_SECONDS = int(os.environ.get("TRIAGE_UNDO_WINDOW_SECONDS", "300"))
TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS = int(
    os.environ.get("TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS", "14")
)
TRIAGE_TIMEZONE = os.environ.get("TRIAGE_TIMEZONE", "UTC")
```

`LLM_ADMIN_USERNAME` and `LLM_ADMIN_PASSWORD` are **removed** from `settings.py` by MUS-37 — see §5.1.4.

---

## 3. Rule trace schema — MUS-42

### 3.1 The problem being solved

`determine_priority(lead, today=None) -> int` and `determine_action(lead, today=None) -> tuple[str, str]`
today emit a score and a prose sentence; the additive score's individual signal contributions are
computed and thrown away. The UI needs `trial_ends_in <= 6d → 4d` — individual conditions with their
evaluated values. The golden eval unpacks a 2-tuple. Both must hold.

### 3.2 Mechanism: a keyword-only out-parameter

```python
def determine_priority(lead, today=None, *, trace: list | None = None) -> int: ...
def determine_action(lead, today=None, *, trace: list | None = None) -> tuple[str, str]: ...
def explain(lead, today=None) -> dict: ...   # NEW -- assembles the v1 envelope
```

- Return types and arity are **unchanged**. `trace` defaults to `None`, and when `None` every
  recording call is a no-op — the functions execute exactly the code path they execute today.
- `trace` is a plain `list`; the functions append `Condition`/`ConditionGroup` dicts to it. It is
  not returned, so no caller can accidentally destructure it.
- `explain()` is the only public trace API. MUS-39's `plan_outreach()` calls it once per lead and
  stores the result on `OutreachAction.rule_trace`.

### 3.3 The mandatory transform (this is where the eval breaks if you free-hand it)

Every `if` in `determine_priority` / `determine_action` is rewritten mechanically as
**evaluate-once, branch-on-the-record**:

```python
# BEFORE
if book >= 5_000_000:
    score += 2

# AFTER -- the ONLY permitted shape
c = _cond(
    id="book_size_very_large",
    field="estimated_book_size_usd",
    label="estimated book size",
    operator=">=",
    threshold=5_000_000,
    value=book,
    unit="usd",
    weight=2,
    trace=trace,
)
if c.passed:
    score += 2
```

`_cond()` performs the comparison itself, returns a frozen `Condition`, and appends
`condition.to_dict()` to `trace` when `trace is not None`. **Re-evaluating the expression in the
`if` is forbidden** — that is precisely how the recorded trace drifts from the taken branch and how
the eval silently regresses.

```python
@dataclass(frozen=True)
class Condition:
    id: str          # stable slug, safe to key React lists on
    field: str       # lead attribute / derived name the value came from
    label: str       # human label, e.g. "days since last contact"
    operator: str    # ">=" "<=" ">" "<" "==" "!=" "in" "contains" "exists" "absent"
    threshold: object
    value: object
    unit: str        # "days" | "usd" | "count" | "date" | "text" | "bool" | "none"
    passed: bool
    weight: int      # points contributed to the priority score when passed; 0 for action rules
    source: str      # "lead" | "events" | "notes" | "derived"
    display: str     # SERVER-RENDERED mono string, e.g. "days_since_contact > 21d → 28d"
```

`display` is rendered **on the server** and the frontend prints it verbatim. Rationale: three FE
surfaces would otherwise each re-derive the string and drift. The formatter is pinned:

| unit | rendering |
|---|---|
| `days` | `{field} {operator} {threshold}d → {value}d` |
| `usd` | `{field} {operator} ${threshold:,} → ${value:,}` |
| `count` | `{field} {operator} {threshold} → {value}` |
| `date` | `{field} {operator} {threshold:%Y-%m-%d} → {value:%Y-%m-%d}` |
| `text` | `{field} {operator} "{threshold}" → "{value}"` |
| `bool` / `none` | `{field} → {value}` |
| any, `value is None` | `{field} {operator} {threshold} → (unset)` |

### 3.4 Envelope — schema v1

```jsonc
{
  "version": 1,
  "today": "2026-06-12",              // the evaluation date; ALL relative figures
                                      // below are relative to THIS date
  "generated_at": "2026-06-12T09:14:03Z",
  "priority": {
    "value": 2,
    "score": 2,
    "bands": [
      {"priority": 1, "min_score": 5},
      {"priority": 2, "min_score": 2},
      {"priority": 3, "min_score": 0}
    ],
    "signals": [ /* Condition | ConditionGroup */ ]
  },
  "action": {
    "value": "power_user_reward",
    "rule_id": "R2_power_user",
    "rule_label": "Power user near a reward / volume-pricing milestone",
    "matched_rule_index": 1,
    "conditions": [ /* the conditions of the MATCHED rule */ ],
    "rejected_rules": [
      {"rule_id": "R1_complete_onboarding", "rule_label": "…",
       "matched": false, "conditions": [ /* short-circuited conditions only */ ]}
    ]
  }
}
```

`ConditionGroup` (for compound signals like trial-at-risk):

```jsonc
{
  "kind": "group",
  "id": "trial_at_risk",
  "label": "trial at risk",
  "operator": "all_of",              // "all_of" | "any_of"
  "passed": false,
  "weight": 1,
  "display": "trial_at_risk → false",
  "conditions": [ /* Condition[] */ ]
}
```

A `Condition` dict carries `"kind": "condition"`. Nesting is **one level only** — groups may not
contain groups.

Rule ids are frozen (matching `determine_action`'s existing branch order):

| index | rule_id | rule_label |
|---|---|---|
| 0 | `R1_complete_onboarding` | Demo completed but never signed up |
| 1 | `R2_power_user` | Power user near a reward / volume-pricing milestone |
| 2 | `R3_follow_up_after_hold` | Hold period has passed and the lead went quiet |
| 3 | `R4_reengage_dormant` | Signed up but stopped using the portal |
| 4 | `R5_nudge_usage` | Active but underusing |
| 5 | `R6_unknown` | No pattern matched — needs human |

### 3.5 Worked example — `lead_001`, Priya Nair, Summit Risk Advisors, `today = 2026-06-12`

Input (from `raw_data/leads.json`): book `$1,400,000`; `stage=active_trial`;
`signed_up_date=2026-04-22` (51d); `last_login_date=2026-05-26` (17d);
`last_contacted_date=2026-05-15` (28d); 19 created / 14 submitted / 6 closed; notes mention a
20-deal milestone.

```jsonc
{
  "version": 1,
  "today": "2026-06-12",
  "generated_at": "2026-06-12T09:14:03Z",
  "priority": {
    "value": 2,
    "score": 2,
    "bands": [{"priority":1,"min_score":5},{"priority":2,"min_score":2},{"priority":3,"min_score":0}],
    "signals": [
      {"kind":"condition","id":"book_size_very_large","field":"estimated_book_size_usd",
       "label":"estimated book size","operator":">=","threshold":5000000,"value":1400000,
       "unit":"usd","passed":false,"weight":2,"source":"lead",
       "display":"estimated_book_size_usd >= $5,000,000 → $1,400,000"},

      {"kind":"condition","id":"book_size_large","field":"estimated_book_size_usd",
       "label":"estimated book size","operator":">=","threshold":2000000,"value":1400000,
       "unit":"usd","passed":false,"weight":1,"source":"lead",
       "display":"estimated_book_size_usd >= $2,000,000 → $1,400,000"},

      {"kind":"group","id":"demo_without_signup","label":"demo completed, never signed up",
       "operator":"all_of","passed":false,"weight":2,
       "display":"demo_without_signup → false",
       "conditions":[
         {"kind":"condition","id":"stage_is_demo_completed","field":"stage","label":"stage",
          "operator":"==","threshold":"demo_completed","value":"active_trial","unit":"text",
          "passed":false,"weight":0,"source":"lead",
          "display":"stage == \"demo_completed\" → \"active_trial\""},
         {"kind":"condition","id":"signed_up_date_absent","field":"signed_up_date",
          "label":"signed up date","operator":"absent","threshold":null,"value":"2026-04-22",
          "unit":"date","passed":false,"weight":0,"source":"lead",
          "display":"signed_up_date absent → 2026-04-22"}
       ]},

      {"kind":"group","id":"gone_quiet","label":"reached out, went quiet","operator":"all_of",
       "passed":false,"weight":2,"display":"gone_quiet → false",
       "conditions":[
         {"kind":"condition","id":"contact_old_enough","field":"days_since_last_contact",
          "label":"days since last contact","operator":">=","threshold":14,"value":28,
          "unit":"days","passed":true,"weight":0,"source":"derived",
          "display":"days_since_last_contact >= 14d → 28d"},
         {"kind":"condition","id":"no_reply_email_present","field":"events",
          "label":"email_sent with outcome=no_reply","operator":"exists","threshold":null,
          "value":false,"unit":"bool","passed":false,"weight":0,"source":"events",
          "display":"no_reply_email_present → false"},
         {"kind":"condition","id":"stall_phrase_in_notes","field":"hubspot_notes",
          "label":"stall phrase in notes","operator":"contains","threshold":"STALL_PHRASES",
          "value":null,"unit":"text","passed":false,"weight":0,"source":"notes",
          "display":"stall_phrase_in_notes → (none)"}
       ]},

      {"kind":"condition","id":"contact_stale","field":"days_since_last_contact",
       "label":"days since last contact","operator":">","threshold":21,"value":28,
       "unit":"days","passed":true,"weight":1,"source":"derived",
       "display":"days_since_last_contact > 21d → 28d"},

      {"kind":"group","id":"trial_at_risk","label":"trial at risk","operator":"all_of",
       "passed":false,"weight":1,"display":"trial_at_risk → false",
       "conditions":[
         {"kind":"condition","id":"signup_old_enough","field":"days_since_signup",
          "label":"days since signup","operator":">","threshold":30,"value":51,"unit":"days",
          "passed":true,"weight":0,"source":"derived",
          "display":"days_since_signup > 30d → 51d"},
         {"kind":"condition","id":"zero_deals","field":"deals_closed","label":"deals closed",
          "operator":"==","threshold":0,"value":6,"unit":"count","passed":false,"weight":0,
          "source":"lead","display":"deals_closed == 0 → 6"}
       ]},

      {"kind":"group","id":"hot_engagement","label":"hot revenue engagement","operator":"any_of",
       "passed":true,"weight":1,"display":"hot_engagement → true",
       "conditions":[
         {"kind":"condition","id":"deals_power_user","field":"deals_closed","label":"deals closed",
          "operator":">=","threshold":5,"value":6,"unit":"count","passed":true,"weight":0,
          "source":"lead","display":"deals_closed >= 5 → 6"},
         {"kind":"condition","id":"submissions_power_user","field":"quotes_submitted",
          "label":"quotes submitted","operator":">=","threshold":10,"value":14,"unit":"count",
          "passed":true,"weight":0,"source":"lead",
          "display":"quotes_submitted >= 10 → 14"}
       ]}
    ]
  },
  "action": {
    "value": "power_user_reward",
    "rule_id": "R2_power_user",
    "rule_label": "Power user near a reward / volume-pricing milestone",
    "matched_rule_index": 1,
    "conditions": [
      {"kind":"condition","id":"deals_power_user","field":"deals_closed","label":"deals closed",
       "operator":">=","threshold":5,"value":6,"unit":"count","passed":true,"weight":0,
       "source":"lead","display":"deals_closed >= 5 → 6"},
      {"kind":"condition","id":"submissions_power_user","field":"quotes_submitted",
       "label":"quotes submitted","operator":">=","threshold":10,"value":14,"unit":"count",
       "passed":true,"weight":0,"source":"lead","display":"quotes_submitted >= 10 → 14"},
      {"kind":"condition","id":"milestone_from_notes","field":"hubspot_notes",
       "label":"deal milestone in notes","operator":"exists","threshold":null,"value":20,
       "unit":"count","passed":true,"weight":0,"source":"notes",
       "display":"milestone_from_notes → 20"},
      {"kind":"condition","id":"deals_remaining_to_milestone","field":"deals_remaining",
       "label":"deals to milestone","operator":">","threshold":0,"value":14,"unit":"count",
       "passed":true,"weight":0,"source":"derived",
       "display":"deals_remaining > 0 → 14"}
    ],
    "rejected_rules": [
      {"rule_id":"R1_complete_onboarding",
       "rule_label":"Demo completed but never signed up","matched":false,
       "conditions":[
         {"kind":"condition","id":"stage_is_demo_completed","field":"stage","label":"stage",
          "operator":"==","threshold":"demo_completed","value":"active_trial","unit":"text",
          "passed":false,"weight":0,"source":"lead",
          "display":"stage == \"demo_completed\" → \"active_trial\""}
       ]}
    ]
  }
}
```

The FE renders the mono trace by mapping `priority.signals` + `action.conditions` to `display`,
styling `passed:true` at `--color-text` and `passed:false` at `--color-text-subtle`. It never
re-derives a string.

### 3.6 Backward-compatibility guarantees — MUS-42 ships these

`project/app/tests_trace.py` (new file, MUS-42 sole owner):

1. **Arity** — `determine_action(lead)` returns a 2-tuple of `(str, str)`; `determine_priority(lead)`
   returns an `int`. Asserted for all 41 golden records.
2. **Trace neutrality** — for all 41 golden records, `determine_action(lead, today=TODAY)` and
   `determine_action(lead, today=TODAY, trace=[])` return *identical* `(action, reason)`; same for
   `determine_priority`. **This is the test that catches a botched `if`-transform.**
3. **Reason parity** — the prose `reason` strings are byte-identical to a frozen fixture captured
   from `origin/master` before the transform, committed as
   `project/app/fixtures/reason_parity.json`. `reason` is displayed *and* is fed into
   `generate_copy`'s prompt; a whitespace change there silently perturbs the copy eval.
4. **Eval gate** — a unit test that shells `python evals/run_rules_eval.py` and asserts exit code 0.
   `evals/baselines/rules.json` must **not** be regenerated. If it needs regenerating, the transform
   changed behaviour and is wrong.
5. **Every emitted condition is reachable** — the union of `id`s across all 41 golden traces covers
   the pinned id list. A condition that is never recorded is a missing `_cond()` call.

---

## 4. Verification span schema — MUS-42

### 4.1 The problem being solved

`verify_copy(lead, copy, action_type, *, level, today) -> list[Violation]` where
`Violation = (kind, message)`. `match.start()` / `match.end()` exist inside `_check_amounts`,
`_check_counts`, `_check_contact_name`, `_check_iso_dates`, `_is_goal_context` — and are discarded.
Worse: the module records only **failures**, so "which claims were checked and passed" does not
exist anywhere, and `4 of 4 claims verified` cannot be computed from today's output.

### 4.2 Mechanism (deliberately identical in shape to §3)

```python
def verify_copy(
    lead, copy, action_type, *,
    level: str = DEFAULT_LEVEL,
    today: datetime.date | None = None,
    claims: list | None = None,          # NEW keyword-only out-parameter
) -> list[Violation]: ...                # return type UNCHANGED

def verify_spans(                        # NEW -- the public span API
    lead, copy, action_type, *,
    level: str = DEFAULT_LEVEL,
    today: datetime.date | None = None,
) -> dict: ...                           # returns the v1 report envelope
```

- `verify_copy`'s return type and dedupe behaviour are unchanged — `tests_verify.py` compares
  `== []` and inspects `.kind` / `.message` only, and `plan_outreach()` truth-tests the list.
- Each internal `_check_*` gains a `claims: list | None` parameter and calls `_claim(...)` for
  **every regex match it inspects**, not only for failures. `verify_copy` then derives its violation
  list from `[c for c in claims if c.verified is False]` plus the omission/offer claims, preserving
  current order.
- `Violation` gains three fields, all defaulted, so positional construction in existing tests still
  works:

  ```python
  @dataclass(frozen=True)
  class Violation:
      kind: str
      message: str
      start: int | None = None
      end: int | None = None
      field: str = ""
  ```

- `verify_spans()` is `_collect_claims()` + counters, wrapped in the envelope below.

### 4.3 Claim object

```python
@dataclass(frozen=True)
class Claim:
    id: str            # "claim-0003" -- stable within one report, ordered by start
    kind: str          # see table below
    start: int | None  # UNICODE CODE POINT index into `copy`, inclusive
    end: int | None    # exclusive; whitespace-trimmed (see 9.1)
    text: str          # copy[start:end] -- echoed so the FE never re-slices
    verified: bool | None  # True=grounded  False=contradicted  None=not a claim
    field: str         # lead field checked against ("deals_closed", …)
    expected: object   # the record value
    claimed: object    # the value the copy asserted
    message: str       # "" when verified is True
    counts_toward_summary: bool   # drives the "N of M claims verified" ratio
```

| `kind` | span? | `verified` | counts |
|---|---|---|---|
| `amount` | yes | true/false | yes |
| `deals_count` | yes | true/false | yes |
| `quotes_count` | yes | true/false | yes |
| `producers_count` | yes | true/false | yes |
| `years_count` | yes | true/false | yes |
| `iso_date` | yes | true/false | yes |
| `contact_name` | yes (group 1 span) | true/false | yes |
| `goal_reference` | yes | **null** | **no** — a target ("the 20-deal milestone"), not an assertion about the record |
| `future_date` | yes | **null** | **no** — scheduling language, deliberately ungrounded |
| `unauthorized_offer` | yes | false | **no** — a prohibition, not a claim about the record |
| `omission` | **null** offsets | false | **no** — `contact_name_absent` / `agency_name_absent` (strict level) |
| `unsupported_year` | yes | false | **no** — strict-level heuristic |

**`N of M claims verified` is exactly `verified_count / checked_count` over claims with
`counts_toward_summary == true`.** Nothing else. This ratio is computed server-side and returned;
the FE never counts.

### 4.4 Report envelope — schema v1

```jsonc
{
  "version": 1,
  "level": "standard",
  "today": "2026-06-12",
  "copy": "…",                    // the EXACT normalized string the offsets index into
  "copy_length": 369,             // in Unicode code points
  "is_astral_safe": true,         // true => JS string indices == code point indices
  "verified_count": 4,
  "unverified_count": 0,
  "checked_count": 4,
  "summary": "4 of 4 claims verified",   // server-rendered; FE prints verbatim
  "can_approve": true,            // SERVER decides -- see the two causes below
  "claims": [ /* Claim[] ordered by start ASC, then end ASC, then id */ ]
}
```

**`can_approve` has two independent causes, and they compose:**

```python
BLOCKING_KINDS = {"unauthorized_offer"}

can_approve = (unverified_count == 0) and not any(
    c.kind in BLOCKING_KINDS for c in claims
)
```

An earlier revision of this contract defined `can_approve` as `unverified_count == 0` alone. That
was wrong. `unauthorized_offer` correctly has `counts_toward_summary: false` — it is a prohibition,
not a claim about the record, and it must not pollute the `N of M` ratio — but that made copy
promising "20% off your renewal" report `can_approve: true` while `plan_outreach()` was
independently setting `needs_human=True` on it. The two halves of the system disagreeing about the
most consequential thing generated copy can contain is a defect, not a design choice.

The summary ratio and the approve gate answer two different questions. Do not define either in terms
of the other.

**MUS-40:** the warning state that replaces the primary button may therefore have to point at an
offer span rather than a mismatched number.

### 4.5 Worked example A — all verified (`lead_001`, `today=2026-06-12`)

Copy (369 code points, `\n` line endings):

```
Subject: Volume pricing ahead of your 20-deal milestone

Hi Priya,

You've closed 6 deals out of 14 quotes submitted since April, which puts Summit Risk Advisors on track for the 20 closed deals mark you mentioned. On a $1,400,000 book that pace is genuinely impressive.

Worth a 15-minute call this week to walk through volume pricing before you get there?

Best,
Dana
```

```jsonc
{
  "version": 1, "level": "standard", "today": "2026-06-12",
  "copy": "Subject: Volume pricing ahead of your 20-deal milestone\n\nHi Priya,\n\n…",
  "copy_length": 369, "is_astral_safe": true,
  "verified_count": 4, "unverified_count": 0, "checked_count": 4,
  "summary": "4 of 4 claims verified", "can_approve": true,
  "claims": [
    {"id":"claim-0001","kind":"contact_name","start":60,"end":65,"text":"Priya",
     "verified":true,"field":"contact_name","expected":"Priya Nair","claimed":"Priya",
     "message":"","counts_toward_summary":true},

    {"id":"claim-0002","kind":"deals_count","start":75,"end":89,"text":"closed 6 deals",
     "verified":true,"field":"deals_closed","expected":6,"claimed":6,
     "message":"","counts_toward_summary":true},

    {"id":"claim-0003","kind":"quotes_count","start":97,"end":116,
     "text":"14 quotes submitted","verified":true,"field":"quotes_submitted",
     "expected":14,"claimed":14,"message":"","counts_toward_summary":true},

    {"id":"claim-0004","kind":"goal_reference","start":179,"end":194,
     "text":"20 closed deals","verified":null,"field":"deals_closed",
     "expected":6,"claimed":20,
     "message":"Framed as a target (\"on track for the 20 closed deals mark\"), not a claim about the record.",
     "counts_toward_summary":false},

    {"id":"claim-0005","kind":"amount","start":220,"end":230,"text":"$1,400,000",
     "verified":true,"field":"estimated_book_size_usd","expected":1400000,"claimed":1400000,
     "message":"","counts_toward_summary":true}
  ]
}
```

Note `claim-0005`: `_CURRENCY_RE`'s `match.group(0)` is `"$1,400,000 "` (span `220–231`) because
the `\b`-terminated optional magnitude suffix swallows the trailing space. The span is **trimmed to
`220–230`**. See §9.1(b).

### 4.6 Worked example B — mixed verified / unverified

Same copy with `closed 9 deals` and `$2,500,000` (identical character lengths, so offsets are
unchanged):

```jsonc
{
  "version": 1, "level": "standard", "today": "2026-06-12",
  "copy": "…", "copy_length": 369, "is_astral_safe": true,
  "verified_count": 2, "unverified_count": 2, "checked_count": 4,
  "summary": "2 of 4 claims verified", "can_approve": false,
  "claims": [
    {"id":"claim-0001","kind":"contact_name","start":60,"end":65,"text":"Priya",
     "verified":true,"field":"contact_name","expected":"Priya Nair","claimed":"Priya",
     "message":"","counts_toward_summary":true},

    {"id":"claim-0002","kind":"deals_count","start":75,"end":89,"text":"closed 9 deals",
     "verified":false,"field":"deals_closed","expected":6,"claimed":9,
     "message":"Copy claims 9 closed deals but the record shows 6.",
     "counts_toward_summary":true},

    {"id":"claim-0003","kind":"quotes_count","start":97,"end":116,
     "text":"14 quotes submitted","verified":true,"field":"quotes_submitted",
     "expected":14,"claimed":14,"message":"","counts_toward_summary":true},

    {"id":"claim-0004","kind":"goal_reference","start":179,"end":194,
     "text":"20 closed deals","verified":null,"field":"deals_closed",
     "expected":6,"claimed":20,
     "message":"Framed as a target, not a claim about the record.",
     "counts_toward_summary":false},

    {"id":"claim-0005","kind":"amount","start":220,"end":230,"text":"$2,500,000",
     "verified":false,"field":"estimated_book_size_usd","expected":1400000,"claimed":2500000,
     "message":"Copy cites $2,500,000 but no matching dollar figure is in the lead record.",
     "counts_toward_summary":true}
  ]
}
```

`can_approve: false` → `POST /api/queue/{id}/approve/` returns **409 `unverified_claims`**. The FE
disables the approve affordance from this same field; the server is authoritative.

### 4.7 Behaviour guarantees — MUS-42 ships these

- `tests_verify.py` (58 tests) and `tests_redteam.py` (18 tests) pass **unmodified**. Any change to
  those files is a contract violation.
- New `project/app/tests_verify_spans.py` (MUS-42 sole owner): for every existing `tests_verify.py`
  fixture, `verify_copy(...)` output (kinds + messages, in order) is identical to the pre-change
  behaviour, and `verify_spans(...)["claims"]` slices back to `copy[start:end] == text` for every
  span-bearing claim.
- **Dedup is currently by `message` string.** That breaks the moment the same message can legitimately
  appear at two offsets. Re-key it to `(kind, start, end, message)` for claims while preserving
  `verify_copy`'s existing by-message dedup for the returned `Violation` list.
- `verify_spans` is pure: no DB, no LLM, duck-typed on the same lead attributes.
- Spans round-trip through `json.dumps` — MUS-39 persists them to a JSONField.

### 4.8 As-built deviations, accepted (MUS-42)

MUS-42 built §3 and §4 as written and flagged where the contract was self-inconsistent. These are the
resolutions. **Downstream tickets build against the as-built behaviour, not the earlier prose.**

1. **`gone_quiet`'s pinned `operator: "all_of"` is not the real predicate.** `_gone_quiet()` is
   `contact_old_enough AND (no_reply OR (contact >= 21d AND stall_phrase))`. The group carries the
   pinned `operator` and `label`, but `passed` comes from the real predicate. Making the branch
   follow `all_of` would change classifications and fail the golden eval — a contract that breaks the
   product is the contract's problem, not the code's.
2. **§3.3's formatter table and §3.5's worked example disagree** on `bool`/`none` rendering
   (`{field} → {value}` vs the pinned `no_reply_email_present → false`, which uses the **id**) and on
   `(none)` vs `(unset)`. **The worked example wins**; it is what four branches were shown.
3. **`exists` / `absent` needed a formatter rule the table lacks**: `signed_up_date absent → 2026-04-22`.
4. **Not every `if` becomes a `_cond()`.** Prose-decoration branches (e.g. the `if snippet:` guard)
   emit no condition; every classifying or scoring branch does. §3.5's pinned `action.conditions`
   confirms this — it omits that guard even though it is true for `lead_001`.
5. **`can_approve` blocks on `unauthorized_offer`** — see §4.4 above. This one was fixed, not accepted.
6. **§4.5 and §4.6 give the same `goal_reference` claim two different messages.** The short,
   deterministic form in §4.6 wins.
7. **`unsupported_year` records failures only**, and `field` for date claims is `"record_dates"`
   rather than a single lead column — they check against a union of four sources, and `expected`
   carries the sorted ISO list.

27 condition `id`s are emitted, all reachable across the 41 golden records and asserted by
`ConditionCoverageTests`. The 18 pinned in §3.5 are all present; rules R3–R5 had no pinned ids and
contributed 9 more.

---

## 5. API contract

Uniform error envelope for **every** non-2xx response from every endpoint in this contract:

```json
{"code": "machine_slug", "detail": "Human-readable sentence."}
```

`detail` is always present because `frontend/src/api/client.ts::toError` reads it first. `code` is
always present and is what the FE branches on. Implemented by
`project/app/exceptions.py::contract_exception_handler` (new file, MUS-37 sole owner), which wraps
DRF's default handler.

### 5.1 Auth — MUS-37

#### `POST /api/auth/request-link/` · AllowAny · throttle `auth_request_ip`

Request: `{"email": "musansht@gmail.com"}`

**200** (always, for any syntactically valid email, allowlisted or not):

```json
{
  "status": "sent",
  "expires_in": 900,
  "resend_after": 30,
  "dev_link": "http://127.0.0.1:8000/auth/consume?token=nJ7yQwVh3kR2mLpX8sTcAeB1dGfHiKoZuYvNqW0xMjE"
}
```

- `dev_link` is a **string only when all three hold**: `settings.DEBUG is True` **and**
  `LOGIN_LINK_DELIVERY == "console"` **and** the email is allowlisted. Otherwise `null`.
- The identical-response guarantee is scoped precisely: **when `DEBUG is False` or
  `LOGIN_LINK_DELIVERY == "email"`, the response body is byte-identical for allowlisted and
  non-allowlisted emails.** See §9.18 — the ticket asks for both `dev_link` and an identical
  response, and they cannot both hold in DEBUG+console.
- Rate limiting is applied **before** the allowlist check, so 429 timing cannot enumerate the
  allowlist.
- A non-allowlisted email performs a discard `secrets.token_urlsafe(32)` + `sha256` so wall-clock
  cost matches.

**400** `{"code":"invalid_email","detail":"Enter a valid email address."}`
**429** `{"code":"rate_limited","detail":"Too many login links requested. Try again in 8 minutes.","retry_after":480}` + `Retry-After` header.

#### `POST /api/auth/consume/` · AllowAny · throttle `auth_consume_ip`

Request: `{"token": "nJ7yQwVh3kR2mLpX8sTcAeB1dGfHiKoZuYvNqW0xMjE"}`

Server logic, pinned verbatim (this is the atomic single-use requirement):

```python
digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
now = timezone.now()
updated = LoginToken.objects.filter(
    token_hash=digest, consumed_at__isnull=True, expires_at__gt=now
).update(consumed_at=now)
if updated != 1:
    # distinguish expired from unknown/consumed for the /signin "expired" state
    ...
```

**200** — sets the `sessionid` cookie and rotates `csrftoken`:

```json
{"authenticated": true, "email": "musansht@gmail.com", "session_expires_at": "2026-08-11T09:14:03Z"}
```

**400** `{"code":"invalid_token","detail":"This sign-in link is not valid."}` — unknown hash, or already consumed.
**400** `{"code":"expired_token","detail":"This sign-in link has expired. Request a new one."}` — the row exists, is unconsumed, and `expires_at <= now`. This distinction is deliberate: MUS-38's third `/signin` state requires it. It leaks only that *some* link for *some* email once existed, which the requester already knows.
**429** as above.

#### `POST /api/auth/logout/` · IsAuthenticated

Request body `{}` (must still be sent so CSRF applies). **204** no content. **401** if not authenticated.

#### `GET /api/auth/me/` · AllowAny (returns 401 itself)

**200** `{"authenticated": true, "email": "musansht@gmail.com"}`
**401** `{"code":"not_authenticated","detail":"Authentication credentials were not provided."}`

> `AllowAny` at the DRF level, returning 401 explicitly, so the route guard can call it without
> triggering the global 401-handling path recursively.

#### 5.1.4 Exemption list — frozen

The **only** DRF views with `permission_classes = [AllowAny]` after MUS-37:

```
POST /api/auth/request-link/
POST /api/auth/consume/
GET  /api/auth/me/
```

Everything else becomes authenticated, including `GET /api/llm/catalog/`, `GET /api/leads/`,
`GET /api/outreach/`, `GET /api/reports/`, `GET /api/review-queue/`, `POST /api/review-decisions/`.

**`LLMAdminBasicAuthentication` is retired.** The magic-link session governs `/api/llm/config/` and
`/api/llm/config/test/` too, so `/settings/` is reachable once signed in. MUS-37:

- deletes `LLMAdminUser` and `LLMAdminBasicAuthentication` from `project/app/authentication.py`,
  leaving only `SessionAuthenticationWith401`;
- removes `authentication_classes` / `permission_classes` from `LLMConfigView` and
  `LLMConfigTestView` in `views.py` so they inherit the global default;
- removes `LLM_ADMIN_USERNAME` / `LLM_ADMIN_PASSWORD` from `project/settings.py`, `.env.example`,
  `docker-compose.yml`, `README.md`, and `SECURITY.md`;
- updates the MUS-32 Basic-auth tests in `tests_llm.py` to authenticate by session.

Rationale: two auth systems in a single-operator tool is one too many, and the stored provider API
key — the actually-sensitive thing in this database — should sit behind the same credential as
everything else.

The Django HTML shell views in `views_frontend.py` stay **public**. They contain no data,
`tests_frontend.py` depends on it, and access control for those pages is MUS-38's client-side route
guard.

### 5.2 Triage queue — MUS-39

All endpoints `IsAuthenticated`. All mutations are `POST` with a JSON body and `X-CSRFToken`.

#### `GET /api/queue/`

**200**:

```json
{
  "date": "2026-07-28",
  "timezone": "UTC",
  "counts": {"total_today": 14, "done_today": 3, "remaining": 11,
             "approved_today": 2, "snoozed_today": 1, "dismissed_today": 0},
  "items": [ /* QueueItem[] -- priority ASC then lead_id ASC */ ]
}
```

`counts.done_today` / `total_today` render the `03 / 14 today` header and the progress bar. **The
server computes `date` in `settings.TRIAGE_TIMEZONE` and returns it; the FE never calls `new Date()`
to decide what "today" means.** See §9.5.

`QueueItem` — the full shape, returned identically by `GET /api/queue/`, `GET /api/queue/{id}/`, and
every mutation:

```json
{
  "id": 412,
  "status": "pending",
  "status_changed_at": null,
  "priority": 2,
  "action_type": "power_user_reward",
  "action_label": "Reward power user (volume pricing)",
  "reason": "Priya Nair is a power user: 19 quotes created, 14 submitted, 6 deals closed, last login 2026-05-26. HubSpot notes flag a volume-pricing conversation at the 20-deal milestone — only 14 deals away.",
  "needs_human": false,
  "further_action": "",
  "created_at": "2026-06-12T09:14:03Z",
  "dedupe_key": "9f2c1a7e4b8d3056c1f9a2b7e4d80516c3a9f27e4b1d8305",
  "lead": {
    "id": "lead_001",
    "agency_name": "Summit Risk Advisors",
    "contact_name": "Priya Nair",
    "contact_email": "priya.nair@summitrisk.com",
    "state": "CO",
    "stage": "active_trial",
    "num_producers": 4,
    "estimated_book_size_usd": 1400000,
    "quotes_created": 19,
    "quotes_submitted": 14,
    "deals_closed": 6,
    "signed_up_date": "2026-04-22",
    "last_login_date": "2026-05-26",
    "last_contacted_date": "2026-05-15",
    "recent_events": [
      {"type": "quote_submitted", "timestamp": "2026-05-26T14:02:00Z", "summary": "Quote submitted"},
      {"type": "login", "timestamp": "2026-05-26T13:58:00Z", "summary": "Portal login"}
    ]
  },
  "suggested_copy": "Subject: Volume pricing ahead of your 20-deal milestone\n\nHi Priya,\n\n…",
  "edited_copy": "",
  "effective_copy": "Subject: Volume pricing ahead of your 20-deal milestone\n\nHi Priya,\n\n…",
  "is_edited": false,
  "rule_trace": { /* section 3.4 envelope, verbatim */ },
  "verification": { /* section 4.4 envelope, verbatim */ },
  "can_approve": true,
  "snooze": {"until": null, "trigger": "", "activity_after": null},
  "dismiss_reason": "",
  "undo": {"available": false, "expires_at": null}
}
```

Pinned semantics:

- `effective_copy = edited_copy or suggested_copy`, computed server-side. The FE **never** branches
  on `edited_copy` being empty.
- `verification` always describes `effective_copy`. `verification.copy === effective_copy` is an
  invariant MUS-39 asserts in a test.
- `can_approve` is a top-level mirror of `verification.can_approve`, duplicated for FE convenience.
  They can never disagree — the serializer derives both from one value.
- `lead.recent_events` is capped at 5, newest first, and is the ONLY event data the FE gets. It
  exists so the inbox needs no second request.
- `undo.expires_at` is an absolute server timestamp. See §9.6.

**Constant query count.** `GET /api/queue/` must issue the same number of queries for 3 items and
for 300. MUS-39 ships:

```python
def test_queue_query_count_is_constant(self):
    self._make_actions(3)
    with CaptureQueriesContext(connection) as small:
        self.client.get("/api/queue/")
    self._make_actions(297)
    with CaptureQueriesContext(connection) as large:
        self.client.get("/api/queue/")
    self.assertEqual(len(small), len(large))
    self.assertLessEqual(len(large), 8)   # absolute ceiling
```

Implementation: `select_related("lead")` + `Prefetch("lead__events", queryset=Event.objects.order_by("-timestamp"))`
sliced in Python. Do **not** slice inside the `Prefetch` queryset (Django re-queries per object).
The existing N+1 in `_events_list` otherwise lands right in the hot path.

#### `GET /api/queue/{id}/` → **200** `QueueItem` · **404** `{"code":"not_found",…}`

#### `POST /api/queue/{id}/edit/`

Request: `{"copy": "Subject: …\n\nHi Priya,\n\n…"}`

Persists `edited_copy`, appends one `OutreachEdit` row, re-runs `verify_spans` against the new copy,
rewrites `verification`.

**200** `QueueItem` · **400** `{"code":"empty_copy",…}` · **409** `{"code":"invalid_transition","detail":"Cannot edit an action with status \"approved\"."}` when status ∉ {pending, snoozed}.

"Revert to original" is the same endpoint with `{"copy": null}` → clears `edited_copy` to `""`,
writes an `OutreachEdit` with `after_text == suggested_copy`, re-verifies.

#### `POST /api/queue/{id}/verify/`

Request: `{"copy": "…"}` — a **dry run**: nothing is persisted, no `OutreachEdit` is written. This
backs live re-verify while typing.

**200**: the §4.4 report envelope alone (not a `QueueItem`).
Throttle scope `queue_verify` at `120/min` so key-repeat cannot hammer it. The FE debounces at
**250 ms** trailing.

#### `POST /api/queue/{id}/approve/`

Request `{}` (the copy in play is already persisted via `/edit/`). Sets `status="approved"`,
`status_changed_at=now()`, marks the latest `OutreachEdit` `committed=True`.

**200** `QueueItem` (with `undo.available: true`, `undo.expires_at` set).
**409** `{"code":"unverified_claims","detail":"2 of 4 claims are unverified. Fix or revert the copy before approving."}`
**409** `{"code":"invalid_transition","detail":"Cannot approve an action with status \"dismissed\"."}`

Allowed source states: `pending`, `snoozed`.

#### `POST /api/queue/{id}/snooze/`

Request `{"trigger": "in_3_days", "until": null}` or `{"trigger": "custom", "until": "2026-08-04T09:00:00Z"}`.

| `trigger` | `snooze_until` | `snooze_activity_after` |
|---|---|---|
| `tomorrow` | next day 09:00 in `TRIAGE_TIMEZONE` | `null` |
| `in_3_days` | today+3 at 09:00 | `null` |
| `next_week` | next Monday 09:00 | `null` |
| `custom` | the supplied `until` (required, must be `> now`) | `null` |
| `on_activity` | `now + TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS` (14d) — a **backstop**, see §9.17 | `now()` |

**200** `QueueItem` · **400** `{"code":"invalid_snooze","detail":"`until` is required and must be in the future for trigger \"custom\"."}` · **409** `invalid_transition` from `approved`/`dismissed`. Re-snoozing an already-`snoozed` item is **allowed** and refreshes both timestamps.

#### `POST /api/queue/{id}/dismiss/`

Request `{"reason": "not_a_fit"}` — `reason` ∈ `{"not_a_fit","bad_timing","wrong_contact","already_handled","copy_unusable","other"}` or `""`.

Sets `status="dismissed"`, `status_changed_at=now()`, `dismiss_reason`, and
`DismissedOutreachKey.objects.update_or_create(dedupe_key=..., defaults={..., "revoked_at": None})`
in the same `transaction.atomic()` block.

**200** `QueueItem` · **400** `{"code":"invalid_reason",…}` · **409** `invalid_transition`.

#### `POST /api/queue/{id}/undo/`

Request `{}`. Reverses the last transition back to `pending`.

- Allowed from `approved`, `snoozed`, `dismissed`.
- Only within `TRIAGE_UNDO_WINDOW_SECONDS` (300) of `status_changed_at`.
- When undoing a **dismiss**, the same transaction sets `DismissedOutreachKey.revoked_at = now()`
  for that `dedupe_key`. See §9.7 — this is the single highest-value cross-boundary bug available.
- Clears `snooze_until`, `snooze_trigger`, `snooze_activity_after`, `dismiss_reason`. Does **not**
  touch `edited_copy` or `OutreachEdit` rows.

**200** `QueueItem` · **409** `{"code":"invalid_transition","detail":"Nothing to undo — this action is already pending."}` · **409** `{"code":"undo_window_expired","detail":"The 5-minute undo window has passed."}`

#### `GET /api/queue/done/`

**200**:

```json
{
  "date": "2026-07-28",
  "timezone": "UTC",
  "summary": {
    "approved": 9, "snoozed": 3, "dismissed": 2, "total": 14,
    "queue_cleared": true,
    "pipeline_value_usd": 28400000,
    "elapsed_seconds": 1147,
    "first_action_at": "2026-07-28T08:41:12Z",
    "last_action_at": "2026-07-28T09:00:19Z"
  },
  "items": [ /* QueueItem[] -- status_changed_at DESC */ ]
}
```

- `pipeline_value_usd` = sum of `lead.estimated_book_size_usd` over today's **approved** items only.
- `elapsed_seconds` = `last_action_at - first_action_at`. `null` when fewer than 2 items.
- `queue_cleared` = `true` iff `counts.remaining == 0` **and** `summary.total > 0`. This single flag
  selects MUS-41's celebration state; `summary.total == 0` selects "nothing done yet". **The FE must
  not infer this from array lengths.**

#### `unsnooze_due` management command — MUS-39

`python manage.py unsnooze_due [--dry-run]`

```python
now = timezone.now()
# 1. time-based
n1 = OutreachAction.objects.filter(
    status=OutreachAction.STATUS_SNOOZED, snooze_until__lte=now
).update(status=OutreachAction.STATUS_PENDING, status_changed_at=now,
         snooze_until=None, snooze_trigger="", snooze_activity_after=None)
# 2. activity-based
woke = Event.objects.filter(lead=OuterRef("lead"), timestamp__gt=OuterRef("snooze_activity_after"))
n2 = OutreachAction.objects.filter(
    status=OutreachAction.STATUS_SNOOZED,
    snooze_trigger=OutreachAction.TRIGGER_ON_ACTIVITY,
    snooze_activity_after__isnull=False,
).filter(Exists(woke)).update(status=..., status_changed_at=now, ...)
```

Idempotent because both filters require `status="snoozed"`, which the update clears. Two conditional
`UPDATE`s, constant query count. Prints `unsnoozed 4 (3 due, 1 on activity)`.

### 5.3 Complete error-code registry

| Code | Status | Raised by |
|---|---|---|
| `invalid_email` | 400 | request-link |
| `invalid_token` | 400 | consume |
| `expired_token` | 400 | consume |
| `empty_copy` | 400 | edit |
| `invalid_snooze` | 400 | snooze |
| `invalid_reason` | 400 | dismiss |
| `validation_error` | 400 | any serializer failure (fallback) |
| `not_authenticated` | 401 | any authenticated endpoint, no session |
| `csrf_failed` | 403 | any POST without a valid `X-CSRFToken` |
| `not_found` | 404 | any `{id}` route |
| `method_not_allowed` | 405 | any |
| `invalid_transition` | 409 | approve / snooze / dismiss / undo / edit |
| `unverified_claims` | 409 | approve |
| `undo_window_expired` | 409 | undo |
| `rate_limited` | 429 | request-link, consume, verify |

---

## 6. TypeScript types

### 6.1 `frontend/src/api/types.ts` — append-only, pinned section order

Each ticket owns exactly one section, delimited by these exact banner comments. Append at end of
file, sections in ticket-number order. Nothing above `// ===== MUS-37` may be edited.

```ts
// ===== MUS-37: magic-link auth =====================================

export interface AuthMe {
  authenticated: boolean;
  email: string | null;
}

export interface AuthRequestLinkInput {
  email: string;
}

export interface AuthRequestLinkResult {
  status: 'sent';
  expires_in: number;          // seconds, e.g. 900
  resend_after: number;        // seconds, e.g. 30
  dev_link: string | null;     // non-null ONLY in DEBUG + console delivery
}

export interface AuthConsumeInput {
  token: string;
}

export interface AuthConsumeResult {
  authenticated: true;
  email: string;
  session_expires_at: string;  // ISO 8601
}

/** Every non-2xx body in this API. `detail` is always present. */
export interface ApiErrorBody {
  code: ApiErrorCode;
  detail: string;
  retry_after?: number;
}

export type ApiErrorCode =
  | 'invalid_email'
  | 'invalid_token'
  | 'expired_token'
  | 'empty_copy'
  | 'invalid_snooze'
  | 'invalid_reason'
  | 'validation_error'
  | 'not_authenticated'
  | 'csrf_failed'
  | 'not_found'
  | 'method_not_allowed'
  | 'invalid_transition'
  | 'unverified_claims'
  | 'undo_window_expired'
  | 'rate_limited';

// ===== MUS-39 / MUS-42: triage queue ===============================

export type QueueStatus = 'pending' | 'approved' | 'snoozed' | 'dismissed';

export type SnoozeTrigger =
  | 'tomorrow'
  | 'in_3_days'
  | 'next_week'
  | 'custom'
  | 'on_activity';

export type DismissReason =
  | 'not_a_fit'
  | 'bad_timing'
  | 'wrong_contact'
  | 'already_handled'
  | 'copy_unusable'
  | 'other'
  | '';

// ---- rule trace (schema v1, MUS-42) ----

export type TraceOperator =
  | '>=' | '<=' | '>' | '<' | '==' | '!='
  | 'in' | 'contains' | 'exists' | 'absent';

export type TraceUnit =
  | 'days' | 'usd' | 'count' | 'date' | 'text' | 'bool' | 'none';

export type TraceSource = 'lead' | 'events' | 'notes' | 'derived';

export interface TraceCondition {
  kind: 'condition';
  id: string;
  field: string;
  label: string;
  operator: TraceOperator;
  threshold: unknown;
  value: unknown;
  unit: TraceUnit;
  passed: boolean;
  weight: number;
  source: TraceSource;
  /** Server-rendered mono line, e.g. "trial_ends_in <= 6d → 4d". Render VERBATIM. */
  display: string;
}

export interface TraceGroup {
  kind: 'group';
  id: string;
  label: string;
  operator: 'all_of' | 'any_of';
  passed: boolean;
  weight: number;
  display: string;
  conditions: TraceCondition[];   // exactly one level of nesting
}

export type TraceSignal = TraceCondition | TraceGroup;

export interface TracePriorityBand {
  priority: 1 | 2 | 3;
  min_score: number;
}

export interface RuleTrace {
  version: 1;
  today: string;                  // ISO date the trace was evaluated at
  generated_at: string;
  priority: {
    value: Priority;
    score: number;
    bands: TracePriorityBand[];
    signals: TraceSignal[];
  };
  action: {
    value: string;
    rule_id: string;
    rule_label: string;
    matched_rule_index: number;
    conditions: TraceCondition[];
    rejected_rules: {
      rule_id: string;
      rule_label: string;
      matched: false;
      conditions: TraceCondition[];
    }[];
  };
}

// ---- verification spans (schema v1, MUS-42) ----

export type ClaimKind =
  | 'amount'
  | 'deals_count'
  | 'quotes_count'
  | 'producers_count'
  | 'years_count'
  | 'iso_date'
  | 'contact_name'
  | 'goal_reference'
  | 'future_date'
  | 'unauthorized_offer'
  | 'omission'
  | 'unsupported_year';

export interface VerificationClaim {
  id: string;
  kind: ClaimKind;
  /** Unicode CODE POINT offsets into `VerificationReport.copy`. Null for omissions. */
  start: number | null;
  end: number | null;
  text: string;
  /** true = green underline, false = red underline, null = no underline. */
  verified: boolean | null;
  field: string;
  expected: unknown;
  claimed: unknown;
  message: string;
  counts_toward_summary: boolean;
}

export interface VerificationReport {
  version: 1;
  level: 'off' | 'standard' | 'strict';
  today: string;
  /** The EXACT string the offsets index into. Render THIS, never a local copy. */
  copy: string;
  copy_length: number;
  /** false => convert offsets with Array.from() before slicing. See CONTRACT 9.1. */
  is_astral_safe: boolean;
  verified_count: number;
  unverified_count: number;
  checked_count: number;
  /** e.g. "4 of 4 claims verified". Render VERBATIM. */
  summary: string;
  can_approve: boolean;
  claims: VerificationClaim[];
}

// ---- queue items ----

export interface QueueLeadEvent {
  type: string;
  timestamp: string;
  summary: string;
}

export interface QueueLead {
  id: string;
  agency_name: string;
  contact_name: string;
  contact_email: string;
  state: string;
  stage: string;
  num_producers: number;
  estimated_book_size_usd: number;
  quotes_created: number;
  quotes_submitted: number;
  deals_closed: number;
  signed_up_date: string | null;
  last_login_date: string | null;
  last_contacted_date: string | null;
  recent_events: QueueLeadEvent[];   // max 5, newest first
}

export interface QueueItem {
  id: number;
  status: QueueStatus;
  status_changed_at: string | null;
  priority: Priority;
  action_type: string;
  action_label: string;
  reason: string;
  needs_human: boolean;
  further_action: string;
  created_at: string;
  dedupe_key: string;
  lead: QueueLead;
  suggested_copy: string;   // IMMUTABLE
  edited_copy: string;      // "" when never edited
  effective_copy: string;   // edited_copy || suggested_copy -- use THIS
  is_edited: boolean;
  rule_trace: RuleTrace;
  verification: VerificationReport;
  can_approve: boolean;
  snooze: {
    until: string | null;
    trigger: SnoozeTrigger | '';
    activity_after: string | null;
  };
  dismiss_reason: DismissReason;
  undo: { available: boolean; expires_at: string | null };
}

export interface QueueCounts {
  total_today: number;
  done_today: number;
  remaining: number;
  approved_today: number;
  snoozed_today: number;
  dismissed_today: number;
}

export interface QueueResponse {
  date: string;         // server-computed "today". NEVER use new Date() instead.
  timezone: string;
  counts: QueueCounts;
  items: QueueItem[];
}

export interface DoneSummary {
  approved: number;
  snoozed: number;
  dismissed: number;
  total: number;
  /** true => MUS-41 celebration state; total === 0 => "nothing done yet" state. */
  queue_cleared: boolean;
  pipeline_value_usd: number;
  elapsed_seconds: number | null;
  first_action_at: string | null;
  last_action_at: string | null;
}

export interface DoneResponse {
  date: string;
  timezone: string;
  summary: DoneSummary;
  items: QueueItem[];
}

export interface EditCopyInput {
  copy: string | null;   // null = revert to suggested_copy
}

export interface VerifyCopyInput {
  copy: string;
}

export interface SnoozeInput {
  trigger: SnoozeTrigger;
  until: string | null;   // required (future ISO 8601) iff trigger === 'custom'
}

export interface DismissInput {
  reason: DismissReason;
}
```

### 6.2 `frontend/src/api/endpoints.ts` — append-only, same section banners

```ts
// ===== MUS-37 / MUS-38: magic-link auth ============================

export const fetchAuthMe = () => getJson<AuthMe>('/api/auth/me/');

export const requestLoginLink = (body: AuthRequestLinkInput) =>
  postJson<AuthRequestLinkResult>('/api/auth/request-link/', body);

export const consumeLoginToken = (body: AuthConsumeInput) =>
  postJson<AuthConsumeResult>('/api/auth/consume/', body);

export const logout = () => postJson<void>('/api/auth/logout/', {});

// ===== MUS-39 / MUS-40 / MUS-41: triage queue ======================

export const fetchQueue = () => getJson<QueueResponse>('/api/queue/');

export const fetchQueueItem = (id: number) =>
  getJson<QueueItem>(`/api/queue/${id}/`);

export const fetchDone = () => getJson<DoneResponse>('/api/queue/done/');

export const editQueueCopy = (id: number, body: EditCopyInput) =>
  postJson<QueueItem>(`/api/queue/${id}/edit/`, body);

export const verifyQueueCopy = (id: number, body: VerifyCopyInput) =>
  postJson<VerificationReport>(`/api/queue/${id}/verify/`, body);

export const approveQueueItem = (id: number) =>
  postJson<QueueItem>(`/api/queue/${id}/approve/`, {});

export const snoozeQueueItem = (id: number, body: SnoozeInput) =>
  postJson<QueueItem>(`/api/queue/${id}/snooze/`, body);

export const dismissQueueItem = (id: number, body: DismissInput) =>
  postJson<QueueItem>(`/api/queue/${id}/dismiss/`, body);

export const undoQueueItem = (id: number) =>
  postJson<QueueItem>(`/api/queue/${id}/undo/`, {});
```

Import lines are added to the single existing `import type { … } from './types';` block, kept
alphabetical.

### 6.3 `frontend/src/api/client.ts` — MUS-38 sole owner, pinned diff

```ts
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;          // NEW -- '' when the body had no `code`

  constructor(message: string, status: number, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}
```

`toError` sets `code` from `record.code` when it is a string. Add:

```ts
/**
 * Called by client.ts on any 401 from a non-auth endpoint. MUS-38 installs the
 * real handler (redirect to /signin preserving `location.pathname + search`).
 */
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}
```

and in `getJson`/`postJson`/`putJson`, before `throw`:

```ts
if (response.status === 401 && !url.startsWith('/api/auth/')) {
  unauthorizedHandler?.();
}
```

MUS-40 and MUS-41 must not modify `client.ts`. If they need something from it, they get it from
MUS-38's merged version.

---

## 7. Design token names — exhaustive

**File:** `frontend/src/styles/tokens.css`. **Owner:** MUS-36, exclusively. Imported exactly once, as
the first line of `frontend/src/styles.css`:

```css
@import './styles/tokens.css';
```

`vite.config.ts` folds all CSS into a single `assets/index.css`, so an imported stylesheet is fine;
a second CSS *entry* would collide.

`styles.css`'s existing rules stay put (they back the four legacy pages), but MUS-36 migrates every
hex literal in them to tokens. New surfaces (`/signin`, `/auth/consume`, `/inbox`, `/done`) use
**only** the tokens below plus the primitives in §7.4. No hex literal may appear outside
`tokens.css`.

Definitions live in three blocks: `:root` (light), `[data-theme='dark']` (dark overrides), and
`@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme])`. **A branch that invents a
name not on this list is broken.**

### 7.1 Complete list

**Type families (3, exactly)**

```
--font-mono     /* machine output: rule trace, offsets, counts, keys, timestamps */
--font-serif    /* generated prose: the draft body and subject, and ONLY those */
--font-sans     /* chrome: nav, labels, buttons, everything else */
```

> **The MUS-36 Linear ticket calls this `--font-voice`. The shipped token is `--font-serif`.**
> The contract name wins; there is no `--font-voice` alias and referencing one is a build error.
> MUS-38/40/41 read the ticket as well as this contract — this is the one place they disagree.

Never mix. A number in the rule trace is mono; the same number inside the email body is serif. This
is the single most distinctive thing in the design and it degrades instantly if applied loosely.

**Type scale**

```
--text-2xs  --text-xs  --text-sm  --text-base  --text-md  --text-lg  --text-xl  --text-2xl
--leading-tight  --leading-snug  --leading-normal  --leading-relaxed
--tracking-tight  --tracking-normal  --tracking-wide  --tracking-caps
--weight-regular  --weight-medium  --weight-semibold  --weight-bold
```

**Surfaces & text**

```
--color-bg  --color-bg-subtle
--color-surface  --color-surface-raised  --color-surface-sunken  --color-surface-hover
--color-overlay-scrim
--color-border  --color-border-subtle  --color-border-strong
--color-text  --color-text-muted  --color-text-subtle  --color-text-inverse
```

Contrast comes from the canvas-to-card gap, not decoration. Resist gradients and glows.

**Accent (one, exactly)**

```
--color-accent  --color-accent-hover  --color-accent-active
--color-accent-subtle  --color-accent-border  --color-accent-contrast
--color-focus-ring
```

Used for the active queue row and the single primary button. Nothing else.

**Priority ramp (P1 red / P2 amber / P3 neutral)**

```
--color-p1-fg  --color-p1-bg  --color-p1-border
--color-p2-fg  --color-p2-bg  --color-p2-border
--color-p3-fg  --color-p3-bg  --color-p3-border
```

**No other element in the app may be red or amber.** That is what makes a coloured pixel legible as
urgency at a glance. Note the existing inconsistency MUS-36 must resolve: `.badge.p2` is `#fd7e14`
but `.priority-badge.p2` is `#ffc107`.

**Verification ramp — underlines on claims ONLY. Never a background, never text colour.**

```
--color-verified  --color-verified-soft
--color-unverified  --color-unverified-soft
--underline-thickness  --underline-offset
--underline-style-verified      /* e.g. solid */
--underline-style-unverified    /* e.g. wavy  */
```

**Triage status**

```
--color-status-pending-fg   --color-status-pending-bg
--color-status-approved-fg  --color-status-approved-bg
--color-status-snoozed-fg   --color-status-snoozed-bg
--color-status-dismissed-fg --color-status-dismissed-bg
```

**Feedback aliases (aliases only — no fourth ramp)**

```
--color-danger   --color-danger-bg
--color-warning  --color-warning-bg
--color-success  --color-success-bg
```

**Spacing**

```
--space-0  --space-1  --space-2  --space-3  --space-4  --space-5  --space-6  --space-7  --space-8
```

**Radius / borders**

```
--radius-none  --radius-sm  --radius-md  --radius-lg  --radius-full
--border-width-hair  --border-width-thick
```

**Elevation**

```
--shadow-none  --shadow-sm  --shadow-md  --shadow-lg  --shadow-focus
```

**Motion**

```
--duration-instant  --duration-fast  --duration-base  --duration-slow
--ease-standard  --ease-out  --ease-in  --ease-spring
```

Keyboard triage means transitions must be *fast* — anything over ~120ms on row advance feels laggy
when someone is holding a key.

**Z-index**

```
--z-base  --z-sticky  --z-popover  --z-overlay  --z-toast
```

**Layout**

```
--rail-width          /* 180px -- the MUS-40 queue rail */
--header-height
--content-gutter
--card-max-width
--prose-measure       /* ch measure for the serif draft body */
```

**Focus**

```
--focus-ring-width  --focus-ring-offset
```

### 7.2 Reduced motion — mandatory, in `tokens.css`

```css
@media (prefers-reduced-motion: reduce) {
  :root {
    --duration-instant: 0ms; --duration-fast: 0ms;
    --duration-base: 0ms;    --duration-slow: 0ms;
  }
}
```

### 7.3 No-flash theme boot — MUS-36 sole owner

Inserted into `project/app/templates/app/spa_base.html` as the **last child of `<head>`**, before the
stylesheet link, verbatim:

```html
<script>
(function () {
  try {
    var t = localStorage.getItem('theme');
    if (t !== 'light' && t !== 'dark') {
      t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
})();
</script>
```

Precedence, pinned: **explicit `localStorage.theme` > `prefers-color-scheme` > light.** The React
toggle writes `localStorage.theme` and sets `data-theme` on `<html>`; it must not remove the
attribute. Because `spa_base.html` is shared, this script is MUS-36's only edit to it, and no other
ticket may touch that file.

### 7.4 UI primitives — MUS-36 sole owner

`frontend/src/components/ui/` — five components, exact names, exact prop shapes. MUS-38/40/41 consume
these and may not fork them.

```
frontend/src/components/ui/Button.tsx
  <Button variant="primary"|"secondary"|"ghost"|"danger"
          size="sm"|"md" loading? disabled? type? onClick? children>

frontend/src/components/ui/Badge.tsx
  <Badge tone="p1"|"p2"|"p3"|"accent"|"neutral"|"verified"|"unverified"
         |"pending"|"approved"|"snoozed"|"dismissed"  children>

frontend/src/components/ui/Card.tsx
  <Card elevation="flat"|"raised" padding="sm"|"md"|"lg" as? children>

frontend/src/components/ui/KeyHint.tsx
  <KeyHint keys={['J']} />        // renders <kbd> in --font-mono

frontend/src/components/ui/Input.tsx
  <Input label id type? value onChange placeholder? error? autoFocus? />

frontend/src/components/ui/index.ts   // barrel: export * from each of the above

frontend/src/components/ui/ThemeToggle.tsx   // SHIPPED: a sixth file. The only
  // consumer of useTheme, and every shell needs it. Exported from the barrel and
  // rendered from PageHeader.tsx, which MUS-36 also owns as a two-line wrapper
  // around the existing <Nav>. MUS-38 owns Nav.tsx; the hunks are disjoint.
```

Import path for consumers: `import { Button, Badge, Card, KeyHint, Input } from '../components/ui';`

Watch: element selectors in `styles.css` are unscoped (`button`, `input`, `select`). The primitives
must not fight them.

---

## 8. File ownership matrix

### 8.1 Exclusive ownership — no other ticket may touch these

| Ticket | Files (sole owner) |
|---|---|
| **MUS-36** | `frontend/src/styles/tokens.css`, `frontend/src/styles.css`, `frontend/src/components/ui/*` (6 files), `frontend/src/hooks/useTheme.ts`, `.gitattributes` (new), the `<head>` theme script in `spa_base.html` |
| **MUS-37** | `project/app/views_auth.py`, `project/app/serializers_auth.py`, `project/app/services/login_links.py`, `project/app/throttling.py`, `project/app/exceptions.py`, `project/app/tests_auth.py`, `project/app/tests_auth_utils.py`, `project/app/migrations/0004_logintoken.py`, `project/app/authentication.py`, `.github/workflows/ci.yml`, `SECURITY.md`, `docker-compose.yml` |
| **MUS-42** | the bodies of `determine_priority` / `determine_action` / `explain` and `_cond` in `project/app/services/outreach.py`; the `_check_*` / `verify_copy` / `verify_spans` / `_claim` functions in `project/app/services/verify.py`; `project/app/tests_trace.py`, `project/app/tests_verify_spans.py`, `project/app/fixtures/reason_parity.json` |
| **MUS-39** | `project/app/views_queue.py`, `project/app/serializers_queue.py`, `project/app/services/dedupe.py`, `project/app/management/commands/unsnooze_due.py`, `project/app/tests_queue.py`, `project/app/migrations/0005_triage_queue.py`, and the `plan_outreach()` persistence hook in `services/outreach.py` |
| **MUS-38** | `frontend/src/pages/SignInPage.tsx`, `frontend/src/pages/ConsumePage.tsx`, `frontend/src/components/RequireAuth.tsx`, `frontend/src/hooks/useAuth.ts`, `frontend/src/api/client.ts`, `frontend/src/main.tsx`, `project/urls.py`, `project/app/views_frontend.py`, `project/app/templates/app/{signin,auth_consume,inbox,done}.html` |
| **MUS-40** | `frontend/src/pages/InboxPage.tsx`, `frontend/src/components/inbox/*`, `frontend/src/hooks/useHotkeys.ts`, `frontend/src/hooks/useQueue.ts` |
| **MUS-41** | `frontend/src/pages/DonePage.tsx`, `frontend/src/components/done/*` |

**MUS-42 and MUS-39 both touch `services/outreach.py` but never the same functions.** MUS-42 owns the
two rule functions and `explain()`; MUS-39 owns only the `plan_outreach()` body. Because MUS-42
merges first, MUS-39 rebases onto it and the hunks are disjoint.

New backend view/serializer modules exist specifically so that `views.py` and `serializers.py` are
**never touched** by MUS-37 or MUS-39. That is not a style preference; it is the whole
conflict-avoidance strategy, and it is the same trick `CONTRACT.md` used with `views_frontend.py`.

### 8.2 Shared files and their resolution rules

#### The committed React bundle — `project/app/static/frontend/assets/index.js` + `index.css`

Four FE branches would each regenerate both files wholesale. Every pair of FE merges would conflict
on 100% of both files, and neither `--ours` nor `--theirs` is ever correct.

**Rule: no ticket agent commits the bundle. Ever.**

- `frontend/` source changes only. `git status` on your branch must show no changes under
  `project/app/static/frontend/`.
- The **integrator** runs `cd frontend && npm ci && npm run build` once on the feature branch after
  the last FE ticket merges, and commits the result as a single
  `chore(build): rebuild frontend bundle (MUS-35)` commit.
- This deviates from the README convention ("commit the bundle before opening a PR"), deliberately
  and only within this feature branch. Mini PRs stay source-only and reviewable.

MUS-36 still adds `.gitattributes` at repo root, to protect the eventual master merge:

```
project/app/static/frontend/assets/index.js   -diff -merge linguist-generated
project/app/static/frontend/assets/index.css  -diff -merge linguist-generated
```

`-merge` makes git report a plain conflict without splicing `<<<<<<<` markers into minified JS —
which would otherwise ship a syntactically broken bundle that nothing catches until a browser loads
it.

The **integrator** adds a `frontend` CI job in the same rebuild commit: `npm ci`, `npm run typecheck`,
`npm run build`, then `git diff --exit-code -- project/app/static/frontend/`. A stale bundle then
fails CI on master forever after. Without it, the last-merged FE feature silently does not exist in
the served app while all its source sits in the repo — exactly the "correct contract,
integration-only bug" class this document exists to prevent.

#### `frontend/src/main.tsx` — pinned target file

MUS-38 lands this whole file, creating `InboxPage`/`DonePage` as one-line placeholder components in
their own files, which MUS-40/41 then replace wholesale. Every later FE branch therefore touches
**zero** lines of `main.tsx`.

```tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { RequireAuth } from './components/RequireAuth';
import { ConsumePage } from './pages/ConsumePage';
import { DashboardPage } from './pages/DashboardPage';
import { DonePage } from './pages/DonePage';
import { InboxPage } from './pages/InboxPage';
import { PlannerPage } from './pages/PlannerPage';
import { ReportsPage } from './pages/ReportsPage';
import { SettingsPage } from './pages/SettingsPage';
import { SignInPage } from './pages/SignInPage';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root element not found');

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route path="/auth/consume" element={<ConsumePage />} />
        <Route path="/" element={<RequireAuth><PlannerPage /></RequireAuth>} />
        <Route path="/inbox" element={<RequireAuth><InboxPage /></RequireAuth>} />
        <Route path="/done" element={<RequireAuth><DonePage /></RequireAuth>} />
        <Route path="/reports/" element={<RequireAuth><ReportsPage /></RequireAuth>} />
        <Route path="/next-actions/" element={<RequireAuth><DashboardPage /></RequireAuth>} />
        <Route path="/settings/" element={<RequireAuth><SettingsPage /></RequireAuth>} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
```

Resolution rule if it conflicts anyway: **discard both sides, paste the block above.**

Note the trailing-slash asymmetry (`/inbox` and `/done` have none, the legacy routes do). Deliberate,
and it must match `project/urls.py` exactly.

`Nav.tsx` duplicates the route list in its own `LINKS` const — MUS-38 updates it in the same commit.

#### `frontend/src/api/types.ts` and `frontend/src/api/endpoints.ts`

Append-only, with the banner comments in §6. Two branches appending different sections at EOF is
trivially resolvable: **keep both hunks, in ticket-number order.** Never edit another ticket's
section. Never reorder.

#### `project/urls.py` — pinned target file, owned by MUS-38

```python
from django.contrib import admin
from django.urls import include, path

from project.app.views_frontend import (
    auth_consume,
    done,
    inbox,
    index,
    next_actions,
    reports,
    settings,
    signin,
)

urlpatterns = [
    path("", index),
    path("signin", signin),
    path("auth/consume", auth_consume),
    path("inbox", inbox),
    path("done", done),
    path("reports/", reports),
    path("next-actions/", next_actions),
    path("settings/", settings),
    path("admin/", admin.site.urls),
    path("api/", include("project.app.urls")),
]
```

MUS-38 lands it complete (including `inbox` and `done`) so MUS-40/41 never touch it. Resolution rule:
**discard both sides, paste the block above.**

#### `project/app/views_frontend.py` — pinned target additions, owned by MUS-38

Appended at EOF, in this order:

```python
@ensure_csrf_cookie
def signin(request):
    """Render the sign-in page (magic-link request). Public: no data on it."""
    return render(request, "app/signin.html")


@ensure_csrf_cookie
def auth_consume(request):
    """Render the token-consumption page. Public; the token is in the query string."""
    return render(request, "app/auth_consume.html")


@ensure_csrf_cookie
def inbox(request):
    """Render the triage inbox shell. Access control is the client-side guard."""
    return render(request, "app/inbox.html")


@ensure_csrf_cookie
def done(request):
    """Render the 'done today' shell. Access control is the client-side guard."""
    return render(request, "app/done.html")
```

Plus four 2-line templates extending `spa_base.html`: `app/signin.html` (`Sign in`),
`app/auth_consume.html` (`Signing you in`), `app/inbox.html` (`Triage Inbox`), `app/done.html`
(`Done Today`).

**None of these views get `@login_required`.** They render an empty `#root`. Adding auth here breaks
`tests_frontend.py` and gives an unauthenticated user a Django 302 instead of MUS-38's designed
sign-in redirect.

#### `project/app/tests_frontend.py`

**Never edit `FrontendTestCase`.** MUS-38 appends a new class at EOF:

```python
class AuthShellTests(TestCase):
    def test_signin_shell_renders(self): ...
    def test_auth_consume_shell_renders(self): ...
    def test_inbox_shell_renders(self): ...
    def test_done_shell_renders(self): ...
```

MUS-40/41 append their own classes after it if they want more.

#### `project/app/urls.py` — pinned target file

```python
from django.urls import path

from project.app.views import (
    LeadListView,
    LLMCatalogView,
    LLMConfigTestView,
    LLMConfigView,
    OutreachListView,
    OutreachReportView,
    OutreachRunView,
    ReviewDecisionListCreateView,
    ReviewQueueView,
)
from project.app.views_auth import (
    AuthConsumeView,
    AuthLogoutView,
    AuthMeView,
    AuthRequestLinkView,
)
from project.app.views_queue import (
    QueueApproveView,
    QueueDetailView,
    QueueDismissView,
    QueueDoneView,
    QueueEditView,
    QueueListView,
    QueueSnoozeView,
    QueueUndoView,
    QueueVerifyView,
)

urlpatterns = [
    # --- auth (MUS-37) ---
    path("auth/request-link/", AuthRequestLinkView.as_view(), name="auth-request-link"),
    path("auth/consume/", AuthConsumeView.as_view(), name="auth-consume"),
    path("auth/logout/", AuthLogoutView.as_view(), name="auth-logout"),
    path("auth/me/", AuthMeView.as_view(), name="auth-me"),
    # --- triage queue (MUS-39) ---
    path("queue/", QueueListView.as_view(), name="queue-list"),
    path("queue/done/", QueueDoneView.as_view(), name="queue-done"),
    path("queue/<int:pk>/", QueueDetailView.as_view(), name="queue-detail"),
    path("queue/<int:pk>/edit/", QueueEditView.as_view(), name="queue-edit"),
    path("queue/<int:pk>/verify/", QueueVerifyView.as_view(), name="queue-verify"),
    path("queue/<int:pk>/approve/", QueueApproveView.as_view(), name="queue-approve"),
    path("queue/<int:pk>/snooze/", QueueSnoozeView.as_view(), name="queue-snooze"),
    path("queue/<int:pk>/dismiss/", QueueDismissView.as_view(), name="queue-dismiss"),
    path("queue/<int:pk>/undo/", QueueUndoView.as_view(), name="queue-undo"),
    # --- existing ---
    path("outreach/run/", OutreachRunView.as_view(), name="outreach-run"),
    path("outreach/", OutreachListView.as_view(), name="outreach-list"),
    path("leads/", LeadListView.as_view(), name="lead-list"),
    path("reports/", OutreachReportView.as_view(), name="outreach-reports"),
    path("review-queue/", ReviewQueueView.as_view(), name="review-queue"),
    path("review-decisions/", ReviewDecisionListCreateView.as_view(), name="review-decisions"),
    path("llm/catalog/", LLMCatalogView.as_view(), name="llm-catalog"),
    path("llm/config/", LLMConfigView.as_view(), name="llm-config"),
    path("llm/config/test/", LLMConfigTestView.as_view(), name="llm-config-test"),
]
```

`queue/done/` **must** precede `queue/<int:pk>/`. With `<int:pk>` it happens to be safe, but ordering
it defensively costs nothing and removes a whole class of "why does /done 404" debugging.

MUS-37 lands the auth block and leaves the `views_queue` import block **commented out with a
`# MUS-39:` marker**; MUS-39 uncomments it. Resolution rule: **discard both sides, paste the block
above.**

#### `project/app/models.py`, `project/settings.py`

Append-at-EOF in ticket order (§2.1, §2.7). Resolution rule: **keep both hunks, MUS-37's above
MUS-39's.**

#### `.env.example`

Append at EOF, ticket order, each block under its own banner. MUS-37 additionally **removes** the
`LLM_ADMIN_USERNAME` / `LLM_ADMIN_PASSWORD` block.

```
# --- Magic-link auth (MUS-37) -------------------------------------------------
# Comma-separated allowlist. An email not on this list gets an identical
# response from POST /api/auth/request-link/ but no link is ever minted.
LOGIN_ALLOWED_EMAILS=

# console (default; prints the link to the server log, and returns dev_link in
# the API response when DJANGO_DEBUG=True) | email (uses Django's email backend)
LOGIN_LINK_DELIVERY=console
LOGIN_TOKEN_TTL_SECONDS=900
LOGIN_LINK_BASE_URL=http://127.0.0.1:8000
LOGIN_RATE_LIMIT_EMAIL=5/hour
LOGIN_RATE_LIMIT_IP=20/hour
LOGIN_RESEND_COOLDOWN_SECONDS=30

# --- Triage queue (MUS-39) ----------------------------------------------------
TRIAGE_UNDO_WINDOW_SECONDS=300
TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS=14
TRIAGE_TIMEZONE=UTC
```

#### `README.md`

- **Line 3 (the coverage badge) is untouchable.** The `coverage-badge` CI job `sed`s it on every push
  to master; a conflict there produces a badge line with merge markers committed by a bot.
- Each ticket appends **one** `###` subsection under an existing `##` heading:
  - MUS-36 → new `## Design system` section, immediately before `## Frontend development`.
  - MUS-37 → `### Signing in` under `## Quickstart`, and removes the Basic-auth paragraph.
  - MUS-39 → `### Triage queue` under `## Architecture, 30 seconds`, plus a line about `unsnooze_due`
    under `## Quickstart`.
  - MUS-38/40/41/42 → no README edits.
- Resolution rule: **keep both, in ticket order.**

#### `project/app/authentication.py` — MUS-37 sole owner

After MUS-37 the file contains exactly one class:

```python
class SessionAuthenticationWith401(SessionAuthentication):
    """SessionAuthentication that produces a 401, not a 403, when anonymous.

    DRF chooses 401 vs 403 by asking the *first* authenticator for an
    ``authenticate_header``. ``SessionAuthentication`` returns None, so a
    request with no session gets 403 -- which would mean MUS-38's 401 handling
    never fires and the route guard never redirects. Returning a scheme here
    is the whole fix.
    """

    def authenticate_header(self, request):
        return 'Session realm="api"'
```

`LLMAdminUser` and `LLMAdminBasicAuthentication` are deleted (§5.1.4).

#### Existing test files that MUS-37 must edit

Turning on the global `IsAuthenticated` breaks **49** existing `self.client.*` calls. MUS-37 owns
this migration and does it with the smallest possible diff:

1. New file `project/app/tests_auth_utils.py` (MUS-37 sole owner). **Name and API frozen now so
   MUS-39 can write against it before MUS-37 merges:**

   ```python
   class AuthenticatedAPITestCase(TestCase):
       """TestCase whose ``self.client`` is already signed in as an allowlisted user."""

       TEST_EMAIL = "tester@example.com"

       def setUp(self):
           super().setUp()
           self.user = get_user_model().objects.create(username=self.TEST_EMAIL,
                                                       email=self.TEST_EMAIL)
           self.user.set_unusable_password()
           self.user.save()
           self.client.force_login(self.user)
   ```

2. In `tests_api.py` and `tests_llm.py`, change only the base class of each `TestCase` that uses
   `self.client`. No test bodies change, except the MUS-32 Basic-auth tests, which lose their
   `HTTP_AUTHORIZATION` header.
3. `GET /api/llm/catalog/` becomes authenticated. Its docstring in `views.py` says "no auth required" —
   MUS-37 updates that comment and removes `authentication_classes` from the two LLM config views.
   These are the **only** lines MUS-37 may change in `views.py`.
4. `SECURITY.md` says every endpoint is intentionally `AllowAny` and documents the Basic-auth scope.
   MUS-37 rewrites that section. Sole owner.

**MUS-39 and MUS-42 put every one of their tests in new files.** MUS-39's inherit
`AuthenticatedAPITestCase`. Neither ever edits `tests_api.py`.

---

## 9. Under-specified risks — pinned

`CONTRACT.md`'s predecessor was correct and still produced an integration-only bug
(`POST /api/llm/config/test/` ignored the request body, breaking the paste→Test→Save flow), because
it named things without pinning their edges. These are this contract's edges. Each is an invariant
plus the test that proves it.

### 9.1 Span offsets: three ways to get them wrong

**(a) Code points vs UTF-16 code units.** Python `re` yields Unicode code-point indices. JavaScript
`String.prototype.slice` uses UTF-16 code units. Any astral character (an emoji in a HubSpot note
that the model echoes) shifts every subsequent offset by one in JS and by zero in Python — every
underline after it lands on the wrong words, for that one lead, only in production data.

> **Invariant:** offsets are Unicode code-point indices. The report carries `is_astral_safe`
> (`len(copy) == len(copy.encode('utf-16-le')) // 2`). When `false`, the FE must slice via
> `Array.from(report.copy)` rather than `report.copy.slice()`.
> **Test (MUS-42):** a copy containing `"🎉"` before a grounded figure; assert
> `is_astral_safe === false` and that `copy[start:end] == claim.text` in Python.
> **Test (MUS-40):** render that fixture and assert the highlighted substring equals `claim.text`.

**(b) Trailing whitespace in the match.** `_CURRENCY_RE` matches `"$1,400,000 "` (span 220–231), not
`"$1,400,000"` (220–230), because the optional magnitude suffix is `\b`-terminated. An untrimmed span
underlines into the next word.

> **Invariant:** every span is trimmed of leading and trailing whitespace before being recorded;
> `claim.text == copy[start:end]` and `claim.text == claim.text.strip()`.
> **Test (MUS-42):** asserted for every claim in every fixture.

**(c) Line endings.** An LLM may emit `\r\n`; a `<textarea>` may submit `\r\n`; Python counts `\r\n`
as two characters.

> **Invariant:** the server normalizes `\r\n` and `\r` to `\n` **before** storing `suggested_copy` /
> `edited_copy` and before computing any offset. `VerificationReport.copy` is the normalized string,
> and the FE renders **that**, not its own local state.
> **Test (MUS-39):** POST `/api/queue/{id}/edit/` with `\r\n` copy; assert the stored copy and
> `verification.copy` contain no `\r` and that offsets still resolve.

### 9.2 Which copy the spans describe

Three strings are in play: `suggested_copy` (immutable), `edited_copy`, and whatever is in the
textarea right now. It is trivially easy to render spans computed against one over the text of
another.

> **Invariant:** `verification.copy === effective_copy` always, enforced in the serializer, asserted
> by a MUS-39 test. During live editing the FE renders spans **only** from the response of
> `POST /api/queue/{id}/verify/`, which echoes the exact copy it verified; if
> `response.copy !== currentTextareaValue`, the FE discards the response as stale (an out-of-order
> debounced request) rather than rendering it.

### 9.3 Who decides `4 of 4` and who decides "approve is blocked"

Two independent counts of "a claim" will diverge — the FE will count `claims.length`, the server will
count checked claims, and a lead whose copy contains one goal reference will read `4 of 5 claims
verified` in the UI and `4 of 4` in the API.

> **Invariant:** the FE renders `verification.summary` **verbatim** and never counts `claims`.
> `can_approve` comes from the server; the FE disables the affordance from it, and the server
> independently returns 409 `unverified_claims`.
> **Test (MUS-42/39):** the mixed fixture in §4.6 asserts `summary == "2 of 4 claims verified"` and
> that approve returns 409.

### 9.4 401 vs 403 — the bug that makes the whole route guard dead code

DRF returns **403**, not 401, for an unauthenticated request when the first authenticator's
`authenticate_header()` returns `None` — which is exactly what `SessionAuthentication` does. MUS-38
would ship 401 handling that never fires, and every guard test would pass because MUS-38's tests
would mock the 401.

> **Invariant:** `DEFAULT_AUTHENTICATION_CLASSES` is `SessionAuthenticationWith401`, which returns a
> header, so anonymous requests get **401**.
> **Test (MUS-37):** `self.assertEqual(self.client.get("/api/queue/").status_code, 401)` with a fresh
> unauthenticated client, for at least one endpoint from each of MUS-37's and MUS-39's surfaces. Not
> 403. This assertion is worth more than the rest of MUS-37's test suite combined.
> **Test (MUS-38):** an integration test that a real 401 response (not a mock) triggers
> `setUnauthorizedHandler`.

### 9.5 "Today" — three clocks, one answer

`USE_TZ=True`, `TIME_ZONE="UTC"`, and the reviewer's browser is not in UTC. `03 / 14 today`,
`/done`'s reverse-chron list, `queue_cleared`, and `snooze until tomorrow 09:00` are four independent
chances to disagree about which day it is. A reviewer in UTC−7 clearing the queue at 6pm local sees
`0 / 0 today` if the FE computes the day.

> **Invariant:** the server computes `date` in `settings.TRIAGE_TIMEZONE` and returns it on
> `/api/queue/` and `/api/queue/done/`. **The frontend never calls `new Date()` to determine a day
> boundary** — only to format an already-server-decided ISO timestamp for display.
> **Test (MUS-39):** with `TRIAGE_TIMEZONE="America/Denver"` and a frozen clock at
> `2026-07-29T04:00:00Z`, assert `date == "2026-07-28"` and that an action taken at
> `2026-07-29T02:00:00Z` counts toward that date.

### 9.6 Undo: server clock, not client clock

> **Invariant:** `undo.expires_at` is an absolute server timestamp; `undo.available` is
> server-computed. The FE renders a countdown from `expires_at` but never gates the request on it —
> it always sends the undo and handles 409 `undo_window_expired`. Clock skew between browser and
> server is real and unbounded.

### 9.7 Undo-of-dismiss must revoke the suppression

The highest-consequence silent bug available in MUS-39: undo restores the row to `pending` and the UI
is correct, but `DismissedOutreachKey` still suppresses. Everything looks fine until the *next*
`plan_outreach()` run, days later, quietly drops the lead.

> **Invariant:** `undo` from `dismissed` sets `revoked_at` on the matching `DismissedOutreachKey` in
> the same `transaction.atomic()` block.
> **Test (MUS-39):** dismiss → undo → run `plan_outreach()` → assert an action for that
> lead/action_type exists. Not "assert status == pending" — that test passes with the bug present.

### 9.8 `plan_outreach()` re-runs and queue duplicates

`OutreachRunView` can be POSTed twice. Today that creates two `OutreachAction` rows per lead, and
`GET /api/outreach/` de-dupes by "most recent per lead" in Python. The triage queue has no such
filter by default, so a second run would double the inbox.

> **Invariant:** `plan_outreach()` skips any lead whose `dedupe_key` already has an **open**
> (`pending` or `snoozed`) action (§2.6, rule 2). `GET /api/queue/` additionally filters to
> `status="pending"`, which is a second line of defence, not the primary one.
> **Test (MUS-39):** run `plan_outreach()` twice; assert the queue length is identical and that
> `OutreachAction.objects.count()` did not double.

### 9.9 The rule trace is a snapshot, not a live computation

`rule_trace` contains `days_since_last_contact > 21d → 28d`. Recomputing it at read time would show
`35d` a week later, silently contradicting the `reason` prose stored alongside it and making two
identical-looking traces on `/inbox` and `/done` disagree.

> **Invariant:** `rule_trace` is written once at `plan_outreach()` time and never recomputed. The FE
> displays `rule_trace.today` next to the trace when it differs from `QueueResponse.date`, so a stale
> recommendation announces itself.
> **Test (MUS-39):** create an action, advance the clock 10 days, `GET /api/queue/`, assert
> `rule_trace` is byte-identical.

### 9.10 `display` strings are server-rendered

Three FE surfaces would each format `operator/threshold/value` into a mono line, and they would drift
on the first `null` value or the first `usd` unit.

> **Invariant:** the FE renders `TraceCondition.display` verbatim. It may use `passed`, `unit` and
> `id` for styling and keys; it may not use `operator`, `threshold` or `value` to build text.
> **Test (MUS-40):** a snapshot test over the §3.5 fixture asserting the rendered mono block equals
> the concatenation of the `display` fields.

### 9.11 `edited_copy` empty-string vs null

`TextField(blank=True, default="")` serializes as `""`; a nullable field serializes as `null`;
TypeScript `string | null` invites `??` where `||` was meant, and vice versa.

> **Invariant:** `edited_copy` is `""` (never `null`) when unedited. `effective_copy` and `is_edited`
> are both server-computed. The FE never writes `edited_copy || suggested_copy`.

### 9.12 CSRF across the login boundary

`django.contrib.auth.login()` calls `rotate_token(request)`, which mints a new `csrftoken` cookie on
the consume response. A client that captured the token before consume and reuses it will 403 on its
first authenticated POST.

> **Invariant:** `client.ts` reads the `csrftoken` cookie **per request** (it already does; do not
> "optimise" it into a module-level constant). `/signin` and `/auth/consume` are `@ensure_csrf_cookie`
> Django shells so a cookie exists before the first POST, including under `npm run dev` via the
> existing `/__csrf` proxy.
> **Test (MUS-37):** consume, then POST to `/api/queue/{id}/approve/` with the *post-consume* cookie,
> assert 200; with the *pre-consume* cookie, assert 403 `csrf_failed`.

### 9.13 Keyboard shortcuts vs text inputs

MUS-40 binds `J/K/A/E/S/X/?` globally. MUS-38's `/signin` has an email field; MUS-40's own inline
editor is a textarea; MUS-41's `/done` may add a filter box. Typing "snooze" into any of them fires
S, X and E.

> **Invariant:** `frontend/src/hooks/useHotkeys.ts` (MUS-40 sole owner, but a shared dependency) is a
> no-op whenever `document.activeElement` matches `input, textarea, select, [contenteditable="true"]`,
> **except** for `Escape` and `Cmd/Ctrl+Enter`, which are explicitly allow-listed because the inline
> editor needs them. `?` additionally requires no modifier keys. MUS-38 and MUS-41 must use this hook
> rather than adding their own `keydown` listeners.

### 9.14 `<120ms` row advance depends on the payload, not the FE

MUS-40 owes a sub-120ms advance with no spinner. That is only achievable if `GET /api/queue/` returns
*fully-rendered* items (trace, verification, copy, lead detail) in one request — which it does,
deliberately. If MUS-39 "optimises" the list endpoint into a summary shape, MUS-40's requirement
becomes unbuildable, and MUS-40 will not find out until it has already built the rail.

> **Invariant:** `GET /api/queue/` returns the complete `QueueItem` for every item. Advancing rows
> performs **zero** network requests. `GET /api/queue/{id}/` exists only for refresh-after-mutation
> and deep links.
> **Test (MUS-39):** assert `GET /api/queue/` items contain non-empty `rule_trace`, `verification`
> and `effective_copy`.

### 9.15 Coverage gate is shared, and MUS-39 is the exposure

`fail_under = 90` fails CI for whoever merges next, not for whoever wrote the untested code. MUS-39
adds the most lines by a wide margin (9 views, 2 models, a management command).

> **Invariant:** each backend branch runs `coverage run manage.py test project.app && coverage report`
> locally and does not open a PR below 90.

### 9.16 The trace transform is where the golden eval dies

The `_cond()` rewrite touches every branch of the two functions the entire product rests on. A
transposed `>=`/`>` inside `_cond` changes classifications on threshold-boundary golden cases — which
is exactly what those cases exist to catch, and exactly what a distracted reviewer regenerates the
baseline to "fix".

> **Invariant:** `evals/baselines/rules.json` is **not regenerated** by MUS-42. If
> `python evals/run_rules_eval.py` exits non-zero, the transform is wrong.
> **Test:** §3.6 items 2, 3 and 4. Item 2 (trace-on vs trace-off parity across all 41 records) is the
> one that catches the transposed operator.

### 9.17 Snooze `on_activity` could hide a lead forever

"When they do something" for a lead that never does anything is indistinguishable from a dismiss,
except nobody chose it.

> **Invariant:** `on_activity` always sets `snooze_until = now + TRIAGE_SNOOZE_ON_ACTIVITY_BACKSTOP_DAYS`
> (14d) as a backstop, so `snooze_until` is non-null for **every** snoozed row and both
> `unsnooze_due` branches and the FE ordering have no NULL case.
> **Test (MUS-39):** snooze `on_activity`, advance 15 days with no events, run `unsnooze_due`, assert
> the item is `pending`.

### 9.18 `dev_link` and the identical-response guarantee genuinely conflict

MUS-37's ticket asks for both. They cannot both hold in DEBUG+console.

> **Resolution, pinned:** the identical-response guarantee is scoped to `DEBUG=False` **or**
> `LOGIN_LINK_DELIVERY == "email"`. In DEBUG+console, `dev_link` is `null` for non-allowlisted emails
> and a URL for allowlisted ones. Documented in `.env.example` and `SECURITY.md`.
> **Test (MUS-37):** with `DEBUG=False`, assert `json.dumps(body, sort_keys=True)` is identical for an
> allowlisted and a non-allowlisted email, and that both are 200.

### 9.19 MUS-42 and MUS-39 share `services/outreach.py`

MUS-42 rewrites `determine_priority`/`determine_action` and adds `explain()`. MUS-39 adds dedupe and
persistence to `plan_outreach()`. Both edit the same file, in the same wave.

> **Invariant:** MUS-42 does not touch `plan_outreach()`. MUS-39 does not touch the two rule functions,
> `_cond`, or `explain`. MUS-42 merges first; MUS-39 rebases onto it. If a conflict surfaces, the hunks
> are disjoint by construction — **keep both.**

---

## 10. Definition of done, per branch

Every branch, before opening a PR:

```bash
ruff check . && ruff format --check .
mypy project/app/services/
python manage.py makemigrations --check --dry-run     # "No changes detected"
coverage run manage.py test project.app && coverage report   # >= 90
python evals/run_rules_eval.py                        # exit 0, baseline UNCHANGED
```

Frontend branches additionally:

```bash
cd frontend && npm ci && npm run typecheck && npm run build
git status --short project/app/static/frontend/       # MUST be empty -- see 8.2
```

Every branch confirms in its PR description, one line per applicable item:
*"This branch touches shared files X, Y; resolved per CONTRACT §8.2."*

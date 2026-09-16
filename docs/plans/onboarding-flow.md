# Plan: self-serve onboarding (register -> five questions -> signed in)

Status: proposed, no code written. Planned against `origin/master` at b374260.

## What is being built

A public wizard at `/onboarding` that takes a new user from an email address to a
signed-in session, collecting a seller profile on the way:

1. **Email** — the address that becomes the account.
2. **What are you selling?** — Product or Service, plus a name and a description.
3. **Payment model** — One-time or Subscription.
4. **Do you offer anything else?** — Delivery / Troubleshooting & technical support /
   Complementary perks / Forward-deployed solutions (multi-select, none allowed).
5. **How do you interact with your customers?** — Website / Gmail / HubSpot / Social media
   (multi-select, at least one). Integrations come later; this step only records the choice.

Pressing **Create account** on step 5 creates the Django user and the profile in one
transaction and establishes the session. No email verification (demo).

## Ground truth the plan depends on

- Auth today is allowlist-only magic link: `services/login_links.is_allowed()` gates
  `POST /api/auth/request-link/`; users are created lazily in `views/auth.py`
  (`AuthConsumeView._user_for`) keyed on `username = normalized email`, unusable password.
  There is no signup path anywhere; README and docker-compose say so explicitly.
- `GET /api/auth/me/` returns exactly `{"authenticated", "email"}` and a test asserts that
  dict verbatim (tests_auth.py:413). The response shape is therefore frozen for this work.
- The app is single-tenant: `Lead` has no owner, `LLMConfiguration` is a pk=1 singleton.
  Onboarding creates a per-user profile; it does not introduce tenancy.
- Frontend: React Router routes in `frontend/src/main.tsx`; every route needs a Django
  shell view in `views/frontend.py`, a template under `templates/app/`, and an entry in
  `project/urls.py` (no SPA catch-all). Newer routes carry no trailing slash.
  Logged-out chrome is `components/AuthShell.tsx`; primitives are `Badge, Button, Card,
  Input, KeyHint, ThemeToggle` (no textarea, no radio/checkbox primitive).
- Frontend tests are `node --test "tests/**/*.test.ts"` on Node 22 importing `.ts` directly
  (see `frontend/tests/auth-destination.test.ts`). There is no jsdom or component-testing
  library, so React components are verified by typecheck + build + a browser walkthrough.
- `docs/areas.toml` referenced by CLAUDE.md does not exist on master; area names below are
  the ones CLAUDE.md uses in prose (`auth`, `dispatch-gate`, `llm-seam`).
- `client.ts` exempts `/api/auth/*` from the global 401 redirect. The new endpoints live
  under `/api/onboarding/` and never return 401 on the public path, so no exemption change.

## Security position (read this first)

- **No verification means anyone can claim any *unused* address.** Accepted for the demo,
  and made explicit by a flag (below) that defaults off in code.
- **An address that already has a user must never be logged in by this flow.** Registering
  with an existing email is a 409 `email_taken` with no session and no writes. Without
  this, "register as alice@x" would be a one-request account takeover. A test pins it.
- Concurrency guard is the database, not a read-then-check: `auth_user.username` is already
  unique; the service catches `IntegrityError` inside the atomic block and maps it to
  `email_taken`. Two racing registrations for one address yield exactly one 201.
- Offering name/description are user-supplied free text that will eventually be
  prompt-bound. This ticket stores them raw (trimmed, length-capped) and **does not feed
  them into any prompt**. Whoever wires them into copy generation must pass them through
  `services/sanitize.sanitize_untrusted` + `wrap_untrusted` at the prompt boundary and
  decide how the verifier corpus treats them (see Follow-ups). Named non-goal here so the
  fail-closed verifier is not stressed by this change.
- Public endpoint gets its own per-IP throttle scope, mirroring `auth_request_ip`.
- CSRF: the onboarding shell view is `@ensure_csrf_cookie` like every other shell; the
  client already re-reads the rotated cookie after `login()`.

## Backend

### Settings (`project/settings.py`, `.env.example`, `docker-compose.yml`)

| Variable | Default in code | .env.example | Purpose |
|---|---|---|---|
| `ONBOARDING_SELF_SIGNUP_ENABLED` | off (same truthy parse as `OUTREACH_AGENT_ENABLED`) | `True` with a comment | Gates registration and lets registered users request magic links. |
| `ONBOARDING_RATE_LIMIT_IP` | `10/hour` | `10/hour` | Throttle for `POST /api/onboarding/register/`. |

Add `"onboarding_register_ip": ONBOARDING_RATE_LIMIT_IP` to `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`.
Both variables get docker-compose passthroughs (`${VAR:-}` blank, per the existing convention).

Flag-off behaviour is byte-identical to today: `is_allowed()` short-circuits before any
query, the register endpoint returns 403 `signup_disabled` and writes nothing.

### Entities (`project/app/models/onboarding.py`, exported from `models/__init__.py`)

```
SellerProfile
  user                  OneToOne(AUTH_USER_MODEL, CASCADE, related_name="seller_profile")
  offering_kind         CharField(16, choices: product | service)
  offering_name         CharField(120)
  offering_description  TextField()            # serializer caps at 2000 chars
  payment_model         CharField(16, choices: one_time | subscription)
  offers_delivery       BooleanField(default=False)
  offers_support        BooleanField(default=False)   # troubleshooting & technical support
  offers_perks          BooleanField(default=False)   # complementary perks
  offers_forward_deployed BooleanField(default=False) # forward-deployed solutions
  created_at            DateTimeField(auto_now_add=True)

SellerChannel
  profile     ForeignKey(SellerProfile, CASCADE, related_name="channels")
  channel     CharField(16, choices: website | gmail | hubspot | social_media)
  created_at  DateTimeField(auto_now_add=True)
  Meta: UniqueConstraint(profile, channel, name="sellerchannel_one_per_channel")
```

Why this split: the four "anything else" options are fixed descriptive attributes with no
lifecycle, so booleans are the honest, queryable shape. Channels become integrations later
(credentials, status, last sync), so each is a row now and the integration ticket adds
columns instead of migrating a JSON list. `created_at` on the profile is the completion
time because the row is only ever created at completion.

Migration `0010_seller_profile`: two `CreateModel`s, no data operations, brand-new tables
(no hot-table lock concern). Must be clean under `makemigrations --check --dry-run`.
Register both models in `admin.py` (channels as a tabular inline).

### Serializer (`project/app/serializers/onboarding.py`)

`OnboardingRegisterSerializer` (input): `email` (EmailField 254), `offering_kind`,
`offering_name` (1..120, trimmed), `offering_description` (1..2000, trimmed),
`payment_model`, `extras` (ListField of choice slugs `delivery|support|perks|forward_deployed`,
may be empty, duplicates collapsed), `channels` (ListField of channel slugs, min 1, duplicates
collapsed). `SellerProfileSerializer` (output): the profile fields plus `extras` and
`channels` as slug lists, `created_at` via the shared `iso` helper.

On validation failure the view raises `ContractError("invalid_onboarding", <flattened
sentence>, extra={"fields": serializer.errors})` so the wizard can jump to the offending step.

### Service (`project/app/services/onboarding.py`)

```
register_seller(data: RegistrationInput) -> RegistrationResult   # frozen dataclasses
```
One `transaction.atomic` block: create user (`username=email=normalized`, unusable
password), create `SellerProfile`, `bulk_create` the `SellerChannel` rows. `IntegrityError`
on the user insert -> `EmailTakenError`. Also `get_profile(user) -> SellerProfile | None`
with `prefetch_related("channels")`. The service never touches the request or session;
`django_login` stays in the view, exactly as `AuthConsumeView` does.

`services/login_links.is_allowed(email)` becomes:
`normalized in LOGIN_ALLOWED_EMAILS or (settings.ONBOARDING_SELF_SIGNUP_ENABLED and User.objects.filter(username=normalized).exists())`.
This is what lets a registered user come back via the existing magic-link page. The
re-check at consume time (`AuthConsumeView`) gets the same answer for free.

### Routes (`project/app/urls.py`, `views/onboarding.py`)

| Method | Path | Auth | Throttle | Responses |
|---|---|---|---|---|
| POST | `/api/onboarding/register/` | AllowAny | `onboarding_register_ip` | 201 `{authenticated: true, email, session_expires_at, profile}`; 400 `invalid_onboarding` (+`fields`); 403 `signup_disabled`; 409 `email_taken`; 429 |
| GET | `/api/onboarding/profile/` | IsAuthenticated | default | 200 `SellerProfileSerializer`; 404 `no_profile` (allowlisted users who never onboarded) |

`register` flow: flag check -> serializer -> service -> `django_login(request, user)` ->
201. `email_taken` is deliberately distinguishable (it is the "sign in instead" branch);
with self-signup on, address enumeration through this endpoint is accepted and documented.

`/api/auth/me/` is left untouched (frozen shape). The wizard learns "already signed in"
from `authenticated: true` alone.

### Backend tests (`project/app/tests/tests_onboarding.py`, names as behavioural sentences)

- registering creates the user, the profile, one channel row per selected channel, and a
  session (`/api/auth/me/` is authenticated afterwards); response carries the profile.
- email is normalised (`" Alice@X.COM "` -> `alice@x.com`) before the uniqueness check.
- registering an address that already has a user returns 409 `email_taken`, creates
  nothing, and **does not establish a session** (takeover guard).
- two concurrent registrations for one address produce exactly one 201 (TransactionTestCase,
  copy the executor + `OperationalError` handling from the tests_auth race test).
- each validation failure returns 400 with a `fields` map and leaves zero rows (atomicity).
- duplicate slugs in `extras`/`channels` collapse; unknown slugs are rejected; empty
  `channels` is rejected; empty `extras` is accepted.
- with the flag off: 403 `signup_disabled`, zero rows; `is_allowed()` for a registered,
  non-allowlisted address is False and issues zero queries (`assertNumQueries(0)`).
- with the flag on: a registered address may request a magic link; an unknown address still
  gets the identical `sent` response and no token.
- per-IP throttle returns 429 with the contract envelope (clear the throttle cache in
  `setUp`, as tests_auth.py does).
- `GET /api/onboarding/profile/`: 200 for an onboarded user, 404 `no_profile` for an
  `AuthenticatedAPITestCase` user, 401 anonymous.
- `tests_frontend.py`: new methods `test_onboarding_shell_renders`,
  `test_onboarding_shell_is_public`, `test_onboarding_trailing_slash_is_not_routed`
  (additive; the existing tuples are not edited).

Gates: `ruff`, `mypy project/app/services/` (the new service is typed), migrations check,
full suite on SQLite; Postgres parity via `DATABASE_URL` before merge.

## Frontend

### Route and shell

- `main.tsx`: `<Route path="/onboarding" element={<OnboardingPage />} />` (public, no
  `RequireAuth`).
- `views/frontend.py` `onboarding` + `templates/app/onboarding.html` + `project/urls.py`
  `path("onboarding", onboarding)`. No trailing slash (newer-route convention).
- `SignInPage` step 01 gains one line under the form: "New here? Create an account" ->
  `/onboarding`. `Nav` is unchanged (the wizard is logged-out chrome).

### API surface (`api/types.ts`, `api/endpoints.ts`)

```
OfferingKind = 'product' | 'service'
PaymentModel = 'one_time' | 'subscription'
OfferingExtra = 'delivery' | 'support' | 'perks' | 'forward_deployed'
CustomerChannel = 'website' | 'gmail' | 'hubspot' | 'social_media'
OnboardingRegisterInput { email, offering_kind, offering_name, offering_description,
                          payment_model, extras: OfferingExtra[], channels: CustomerChannel[] }
SellerProfile { ...same fields..., created_at }
OnboardingRegisterResult { authenticated: true; email; session_expires_at; profile: SellerProfile }
ApiErrorCode gains 'invalid_onboarding' | 'signup_disabled' | 'email_taken' | 'no_profile'
```
`registerSeller(body)` -> `POST /api/onboarding/register/`; `fetchSellerProfile()` ->
`GET /api/onboarding/profile/`. One line each, per the endpoints registry convention.

### Pure modules (unit-tested under `node --test`)

- `util/onboarding.ts`: the slug lists with display labels (mirrors `util/labels.ts`),
  `STEPS` order, per-step `validateStep(step, draft) -> FieldErrors`, and
  `stepForField(field)` so a server `fields` map lands the user on the right step.
- `hooks/onboardingDraft.ts`: `loadDraft() / saveDraft(draft) / clearDraft()` over
  `sessionStorage` key `onboarding:draft`, modelled on `authDestination.ts` (try/catch
  around storage, shape-checked on read so a stale or hand-edited value is discarded).
  Holds answers only; there is never a credential to store.

Tests: `frontend/tests/onboarding-validate.test.ts` (each step's rules, `stepForField`
coverage for every server field) and `frontend/tests/onboarding-draft.test.ts` (round trip,
garbage in -> empty draft, `clearDraft` after success).

### Page and components

- `pages/OnboardingPage.tsx`: owns `step`, `draft`, `pending`, `fieldErrors`, `formError`.
  On mount calls `fetchAuthMe()`; if authenticated, `navigate('/leads/', {replace:true})`.
  Renders inside `AuthShell` with a "Step n of 5" progress line and Back / Continue
  buttons; step 5's primary button reads **Create account**. Draft is saved on every
  change and cleared on success. On success: `navigate(takeDestination(), {replace:true})`
  (defaults to `/leads/`). Error handling mirrors `SignInPage`: the server's sentence is
  shown; `email_taken` renders on step 1 with a "Sign in instead" link; `signup_disabled`
  replaces the form with a short explanation; `invalid_onboarding` jumps to
  `stepForField(firstKey)` and paints field errors.
- `components/onboarding/`:
  - `EmailStep` (reuses `Input`; sets `autocomplete/name/inputmode` on the DOM node the
    way `SignInPage` does, because `Input`'s props are frozen).
  - `OfferingStep`: kind chooser + name `Input` + description textarea.
  - `PaymentStep`, `ExtrasStep`, `ChannelsStep`: built on one `ChoiceGroup` component.
  - `ChoiceGroup`: `mode="single" | "multi"`, renders option cards with proper
    `role="radiogroup"`/`role="group"`, native `<input type=radio|checkbox>` inside the
    label so keyboard and screen readers work without custom key handling.
- `components/ui/Textarea.tsx`: new primitive shaped like `Input` (label, id, error slot),
  exported from `ui/index.ts`, styled from the same tokens.
- Styles: extend `pages/auth.css` (the wizard is logged-out chrome) with `.onboarding-*`
  rules; no colours outside `styles/tokens.css`.

### Frontend verification

`cd frontend && npm ci && npm run typecheck && npm test && npm run build`, commit the
rebuilt `project/app/static/frontend/` (never hand-edited), then a Playwright walkthrough
(webapp-testing skill) against `runserver` with the flag on: complete all five steps, land
on `/leads/` signed in, refresh mid-wizard and keep answers, register the same address
twice and see `email_taken` with no session.

## Delivery order

1. **PR 1 — backend** (settings, models + migration 0010, serializer, service, views,
   urls, admin, `is_allowed` change, tests, `.env.example`, docker-compose, README note
   replacing "there is no signup flow"). Touches auth and adds a migration, so it needs a
   human before merge per CLAUDE.md.
2. **PR 2 — frontend** (types, endpoints, pure modules + tests, page, components,
   Textarea primitive, shell view/template/url + `tests_frontend` additions, SignInPage
   link, rebuilt bundle). Depends on PR 1's contract; can be developed against it on the
   same branch.

## Assumptions to confirm (work proceeds under these)

- "Product/Service + Name + Description" means a Product-or-Service toggle plus one name
  and one description, not separate product and service records.
- Step 4 (extras) may be empty; step 5 (channels) requires at least one.
- The flag defaults **off** in `settings.py` and **on** in `.env.example`, so the demo works
  from `cp .env.example .env` while the code default stays allowlist-only.
- Existing allowlisted users are untouched: they are never pushed through the wizard, and
  `/api/onboarding/profile/` simply 404s for them.
- Five steps with the submit on step 5; no separate review screen.

## Follow-ups (named non-goals)

- **Email verification**: swap the final action for `login_links.issue_login_link` +
  `deliver_login_link` and render `SignInPage`'s Sent state; the profile is created
  pending and attached at consume. The service/view split above keeps this a one-view change.
- **Channel integrations**: add status/credential columns to `SellerChannel`; the unique
  constraint already guarantees one row per channel per seller.
- **Feeding the offering into copy generation**: sanitize + fence at the prompt boundary and
  choose explicitly whether the verifier corpus gains the offering fields (planning rule on
  new fact sources upstream of the fail-closed gate).
- Early "is this email free?" check on step 1, a Settings card showing the profile, and
  onboarding for pre-existing allowlisted users.

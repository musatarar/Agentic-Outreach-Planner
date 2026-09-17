# Plan: self-serve onboarding (register -> five questions -> signed in)

Status: proposed, no code written. Base: `origin/master` @ 9405a1d.
Revised 2026-09-17 after three things landed on master: `Lead.tenant` (#127, an opaque
64-char string, blank on every row, nothing filters on it yet), the strip-to-core reset
(#119: migrations restart at 0001, the settings/queue/trace surfaces are gone, per-page
shell tests moved into tests_auth.py) and the user-owned rules catalog (#122, the repo's
first per-user model and the package layout copied below). Rescoped to the plan-feature
skill's shape: what the ask requires is in "Change"; everything inferred is under "Extras"
for the user to accept or drop. Once confirmed, the record is a GitHub issue and this file
can go.

## Ask

Register with email, answer what you sell (product/service, name, description), payment
model (one-time/subscription), extras (delivery / troubleshooting & technical support /
complementary perks / forward-deployed solutions), customer channels (website / Gmail /
HubSpot / social media), then create the account and log the user in. No email
verification for the demo. Leads are tenant-scoped now, so the account must be a tenant.

## Done when

1. Submitting the five answers with an unused address creates the user and its seller
   profile, the browser is signed in (`GET /api/auth/me/` answers authenticated) and lands
   on `/leads/`.
2. The profile carries a tenant identifier, unique per account and in the shape
   `Lead.tenant` stores (at most 64 chars), so leads can be attributed to the account when
   scoping lands.
3. Submitting with an address that already has a user answers 409 `email_taken`, writes
   nothing and does not sign in (without verification this is the account-takeover guard).
4. A registered user can come back through the existing magic-link sign-in page.

## Change — backend

- `project/app/onboarding/models.py` — `SellerProfile`: `user` OneToOne(AUTH_USER_MODEL,
  CASCADE, related_name `seller_profile`); `tenant` CharField(64, unique, constraint
  `sellerprofile_tenant_unique`) minted server-side with `secrets.token_urlsafe(16)`, never
  client-supplied; `offering_kind` (product|service); `offering_name` (120);
  `offering_description` (Text, MaxLengthValidator 2000); `payment_model`
  (one_time|subscription); `extras` and `channels` JSONField lists of slugs
  (delivery|support|perks|forward_deployed, may be empty; website|gmail|hubspot|social_media,
  at least one), validated in `clean()` the way `OutreachRule.clean()` validates
  `conditions`; `created_at` — serves 1, 2
- `project/app/models/__init__.py` — export `SellerProfile` — 1
- `project/app/migrations/0003_seller_profile.py` — one CreateModel, additive, no data
  operations, brand-new table (not a hot table) — 1
- `project/app/onboarding/services.py` — `register_seller(email, fields)`: normalise via
  `login_links.normalize_email`; one `transaction.atomic`: create the user exactly as
  `AuthConsumeView._user_for` does (username = email, unusable password), create the
  profile with a minted tenant; `IntegrityError` on the username unique -> `EmailTaken`.
  No request or session in services — 1, 2, 3
- `project/app/onboarding/routes.py` — `OnboardingRegisterSerializer` (input) and
  `RegisterView`: POST, AllowAny, `throttle_scope = "onboarding_register_ip"`;
  serializer -> service -> `django_login` -> 201 `{authenticated, email,
  session_expires_at}`; 400 `invalid_onboarding` with `extra={"fields": errors}`;
  409 `email_taken`. Same layout as `rules/routes.py` — 1, 3
- `project/app/urls.py` — `*onboarding_routes.urlpatterns` — 1
- `project/settings.py` — `"onboarding_register_ip": "10/hour"` in
  `DEFAULT_THROTTLE_RATES` (a literal, like `auth_consume_ip`; no new env var) — 1
- `project/app/services/login_links.py` `is_allowed()` — allowlisted OR a user with that
  username exists — 4
- `project/app/views/frontend.py`, `templates/app/onboarding.html`, `project/urls.py` —
  public shell at `/onboarding`, no trailing slash — 1
- `.env.example:28`, `README.md:68`, `docker-compose.yml:63` — drop "There is no signup
  flow." — 4

## Change — frontend

- `frontend/src/api/types.ts`, `endpoints.ts` — `OnboardingRegisterInput/Result`,
  `registerSeller()`; `ApiErrorCode` gains `invalid_onboarding | email_taken` — 1
- `frontend/src/util/onboarding.ts` — slug lists with labels, `STEPS`, `validateStep()`,
  `stepForField()` (pure, so `node --test` covers it) — 1
- `frontend/src/pages/OnboardingPage.tsx` + `components/onboarding/` (`ChoiceGroup` for the
  single/multi-select steps, one component per step) — five steps inside `AuthShell`,
  Back/Continue, "Create account" on step 5; already signed in -> `/leads/`; success ->
  `navigate(takeDestination())`; `email_taken` renders on step 1 with a "Sign in instead"
  link; `invalid_onboarding` jumps to `stepForField(first)`. The description is a plain
  `<textarea>` styled in `auth.css` — 1
- `frontend/src/main.tsx` — `<Route path="/onboarding">` (public) — 1
- `frontend/src/pages/SignInPage.tsx` — one "New here? Create an account" link — 1
- rebuild `project/app/static/frontend/` and commit it — 1

## Tests

- `tests_onboarding.py`: registers, signs in, profile row exists, tenant is 22 chars and
  differs across two registrations (1, 2); email is normalised before the uniqueness check;
  existing address -> 409, zero rows, no session (3); two concurrent registrations -> exactly
  one 201 (TransactionTestCase, the tests_auth race pattern); each validation failure -> 400
  with `fields`, zero rows; unknown or duplicate slugs rejected, empty channels rejected,
  empty extras accepted; throttle -> 429 (clear the cache in setUp); a registered address
  gets a sent link, an unknown address the identical response and no token (4); the
  `/onboarding` shell is public and sets `csrftoken`.
- `frontend/tests/onboarding-validate.test.ts`: each step's rules; `stepForField` for every
  server field name.
- mechanical edits to existing tests: none.

## Human review

Migration 0003; auth and throttle code (the register endpoint, `is_allowed`, the new
scope). Neither is avoidable: the ask is an auth path and a new table.

## Extras (confirm; every item defaults to out)

1. Flag `ONBOARDING_SELF_SIGNUP_ENABLED` (off in code, on in `.env.example`) — self-signup
   means the allowlist is no longer the whole gate — cost: setting, `.env.example` line,
   compose passthrough, a flag-flip review. Recommended in.
2. Persist the wizard draft in sessionStorage (`hooks/onboardingDraft.ts` + test) — a
   refresh mid-wizard keeps the answers — cost: one module, one test.
3. `GET /api/onboarding/profile/` — symmetry; nothing reads it yet. Out.
4. Scope `LeadListView`, `LeadComposeView` and `plan_outreach` by
   `request.user.seller_profile.tenant` — #127 deferred this to "the actions work";
   allowlisted users have no profile and would see nothing. Later issue, with that work.
5. Attach demo leads to a tenant (`ingest_data --tenant`, or on registration) — a fresh
   account sees every lead today and none once 4 lands. Later, with 4.
6. Seed the rules catalog for the new user (`seed_rules_catalog --owner`) — a fresh account
   has no action types, so the planner routes every lead to a human. Later issue.
7. `SellerChannel` rows instead of the JSON list — "integrations later"; that ticket owns
   the migration. Out.
8. Admin registration for `SellerProfile`. Out.
9. Email verification — the final step issues a magic link instead of logging in and
   reuses the Sent state. Later issue.

## Gates

CLAUDE.md commands (ruff, `mypy project/app/services/` — note `onboarding/services.py` sits
outside that path, as `rules/services.py` does; keep it typed anyway), `makemigrations
--check --dry-run`, full suite, Postgres parity via `DATABASE_URL`; frontend
`npm ci && npm run typecheck && npm test && npm run build` plus the bundle diff; a
Playwright walkthrough of the five steps with a second registration of the same address.

# Security notes

## Authentication

As of MUS-32, three endpoints require HTTP Basic Auth:

- `GET /api/llm/catalog/` — actually **unauthenticated** (read-only reference
  data: the list of supported providers/models, no secrets).
- `GET|PUT /api/llm/config/` — **Basic Auth required.** Reads/writes the
  active LLM provider/model/key selection; `PUT` is the only place a
  provider API key can be written.
- `POST /api/llm/config/test/` — **Basic Auth required.** Fires one live
  completion against the currently configured provider/key to verify it
  works.

Credentials are a single username/password pair from `LLM_ADMIN_USERNAME` /
`LLM_ADMIN_PASSWORD` (env vars, see `.env.example`), compared with
`hmac.compare_digest` (constant-time) in
`project/app/authentication.py::LLMAdminBasicAuthentication`. There is no
user model, no sessions, and no per-user accounts backing this — it's one
shared credential pair gating write access to stored provider API keys.

Every other endpoint in this API (`/api/leads/`, `/api/outreach/*`,
`/api/reports/`, `/api/review-queue/`, `/api/review-decisions/`) remains
**unauthenticated (`AllowAny`)**. This is a known, existing gap — anyone who
can reach the server can read/write lead and outreach data — and is
explicitly **out of scope** for this change. There is no global
`REST_FRAMEWORK` auth default in `project/settings.py`; Basic Auth is applied
only to the two `/api/llm/*` views listed above, deliberately, because those
are the only endpoints that touch a stored secret (the provider API key).

## Secrets at rest

Provider API keys saved via `PUT /api/llm/config/` are encrypted before
being written to the database (`LLMConfiguration.encrypted_api_key`, a
`BinaryField`) using Fernet symmetric encryption
(`project/app/services/crypto.py`). The encryption key,
`LLM_KEY_ENCRYPTION_KEY`, is a dedicated env var — **not** derived from
`DJANGO_SECRET_KEY` — so rotating one never silently invalidates the other.

- If `LLM_KEY_ENCRYPTION_KEY` is unset but a row with a stored key already
  exists in the database, a Django system check (`project/app/checks.py`)
  fails loudly at boot (`manage.py check`/`runserver`/etc.), rather than
  waiting for the first LLM call to blow up with a decryption error.
- The key is never returned by the API: `GET`/`PUT /api/llm/config/`
  responses expose only `has_key` (bool), `key_last_four` (last 4 chars of
  the plaintext, stored alongside the ciphertext), and `key_source`
  (`"database"` | `"environment"` | `"none"`) — never the key itself, never
  the ciphertext blob.
- The Django admin (`project/app/admin.py::LLMConfigurationAdmin`) excludes
  `encrypted_api_key` from every list/detail view — the stored key can't be
  read (encrypted or otherwise) from the admin UI.
- `POST /api/llm/config/test/` never echoes the key or the raw
  provider-SDK exception text back to the caller; failures are mapped to one
  of four `error_kind` values (`auth`, `rate_limit`, `unknown_model`,
  `network`) with a generic, safe message.

## Known gaps (tracked separately, not fixed here)

- The rest of the API (leads, outreach actions, review queue/decisions) has
  no authentication or authorization at all.
- There's no rate limiting on any endpoint, including the two Basic
  Auth-protected ones.
- Prompt-injection / input-sanitization hardening for LLM-generated outreach
  copy is tracked under a separate, unmerged effort (MUS-23/MUS-24). A future
  merge of that work into this file is expected to conflict here — that's
  fine; reconcile by keeping both sections.

/**
 * Thin fetch wrapper for the DRF API. The one behaviour that must not regress
 * from the old vanilla-JS pages: every POST carries the `csrftoken` cookie as
 * an `X-CSRFToken` header, or Django rejects it with a 403.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string; // '' when the body had no `code`

  constructor(message: string, status: number, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/**
 * Called by this module on any 401 from a non-auth endpoint. MUS-38's route
 * guard installs the real handler (redirect to /signin, preserving
 * `location.pathname + search`).
 *
 * It is a module-level slot rather than a thrown-and-caught concern because a
 * 401 can surface from any call site, and every one of them rendering its own
 * "something went wrong" is exactly the broken screen the ticket rules out.
 */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

/**
 * `/api/auth/me/` returns 401 by design so the guard can ask "am I signed in?"
 * without recursing into the redirect it is about to decide on. Consume and
 * request-link own their own error UI too. So the auth namespace is exempt.
 */
function notifyIfUnauthorized(url: string, status: number): void {
  if (status === 401 && !url.startsWith('/api/auth/')) {
    unauthorizedHandler?.();
  }
}

function getCookie(name: string): string | null {
  if (!document.cookie) return null;
  for (const raw of document.cookie.split(';')) {
    const cookie = raw.trim();
    if (cookie.startsWith(`${name}=`)) {
      return decodeURIComponent(cookie.slice(name.length + 1));
    }
  }
  return null;
}

/**
 * Django only sets the csrftoken cookie from its @ensure_csrf_cookie HTML
 * views. Under `npm run dev` the shell is served by Vite, so that view never
 * runs and the first POST would 403. Fetching /__csrf (proxied to Django's
 * index — see vite.config.ts) mints the cookie. Against the built bundle the
 * Django shell has already set it, so this is a no-op.
 */
async function ensureCsrfToken(): Promise<string | null> {
  const existing = getCookie('csrftoken');
  if (existing) return existing;
  try {
    await fetch('/__csrf', { credentials: 'same-origin' });
  } catch {
    // Fall through — the POST below will surface the real failure.
  }
  return getCookie('csrftoken');
}

/**
 * Prefer DRF's `detail`, then the first field error, then a bare status.
 *
 * `code` is the machine slug from CONTRACT §5.3 and is what callers branch on;
 * `detail` is only ever shown to a human. Pre-MUS-37 endpoints have no `code`,
 * hence the '' default rather than a required field.
 */
async function toError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    const code = typeof record.code === 'string' ? record.code : '';
    if (typeof record.detail === 'string') {
      return new ApiError(record.detail, response.status, code);
    }
    const first = Object.entries(record)[0];
    if (first) {
      const [field, value] = first;
      const text = Array.isArray(value) ? String(value[0]) : String(value);
      return new ApiError(`${field}: ${text}`, response.status, code);
    }
  }
  return new ApiError(`HTTP ${response.status}`, response.status);
}

export async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) {
    notifyIfUnauthorized(url, response.status);
    throw await toError(response);
  }
  return (await response.json()) as T;
}

export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const csrfToken = await ensureCsrfToken();
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(csrfToken ? { 'X-CSRFToken': csrfToken } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    notifyIfUnauthorized(url, response.status);
    throw await toError(response);
  }
  // POST /api/auth/logout/ answers 204 with no body (CONTRACT §5.1), and
  // response.json() on an empty body throws. Every other POST returns JSON.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function putJson<T>(url: string, body: unknown): Promise<T> {
  const csrfToken = await ensureCsrfToken();
  const response = await fetch(url, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(csrfToken ? { 'X-CSRFToken': csrfToken } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    notifyIfUnauthorized(url, response.status);
    throw await toError(response);
  }
  return (await response.json()) as T;
}

/** Uniform message for anything thrown out of the calls above. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

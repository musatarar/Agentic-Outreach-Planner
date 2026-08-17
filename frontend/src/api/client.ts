/**
 * Thin fetch wrapper for the DRF API. Every POST/PUT carries the `csrftoken`
 * cookie as an `X-CSRFToken` header, or Django rejects it with a 403.
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
 * Called on any 401 from a non-auth endpoint; the route guard installs the
 * real handler (redirect to /signin, preserving path + search).
 */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

// The auth namespace is exempt: /me/ 401s by design and the auth pages own
// their own error UI.
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
 * Under Vite dev Django never mints the csrftoken cookie; fetching /__csrf
 * (proxied to Django's index) does. No-op against the built bundle.
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
 * `code` is the machine slug callers branch on; '' when the body has none.
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
  // 204 (logout) has no body; response.json() on an empty body throws.
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

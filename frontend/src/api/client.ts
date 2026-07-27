/**
 * Thin fetch wrapper for the DRF API. The one behaviour that must not regress
 * from the old vanilla-JS pages: every POST carries the `csrftoken` cookie as
 * an `X-CSRFToken` header, or Django rejects it with a 403.
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
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

/** Prefer DRF's `detail`, then the first field error, then a bare status. */
async function toError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    if (typeof record.detail === 'string') {
      return new ApiError(record.detail, response.status);
    }
    const first = Object.entries(record)[0];
    if (first) {
      const [field, value] = first;
      const text = Array.isArray(value) ? String(value[0]) : String(value);
      return new ApiError(`${field}: ${text}`, response.status);
    }
  }
  return new ApiError(`HTTP ${response.status}`, response.status);
}

export async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) throw await toError(response);
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
  if (!response.ok) throw await toError(response);
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
  if (!response.ok) throw await toError(response);
  return (await response.json()) as T;
}

/** Uniform message for anything thrown out of the calls above. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

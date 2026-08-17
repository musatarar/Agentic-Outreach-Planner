/**
 * Where to land after signing in. A *path*, never a credential. sessionStorage
 * rather than router state because the magic link arrives as a fresh page load
 * (often a new tab), which `location.state` does not survive.
 */

const DESTINATION_KEY = 'auth:destination';
const DEFAULT_DESTINATION = '/inbox';

/**
 * Same-origin absolute paths only. `//evil.example` is protocol-relative and
 * would be an open redirect, so `startsWith('/')` alone is not enough.
 */
function isSafePath(value: string): boolean {
  return value.startsWith('/') && !value.startsWith('//');
}

export function rememberDestination(path: string): void {
  if (!isSafePath(path) || path === '/signin') return;
  try {
    sessionStorage.setItem(DESTINATION_KEY, path);
  } catch {
    // Storage disabled: signing in still works, it just lands on the default.
  }
}

/** Read once and clear, so a later sign-in cannot replay a stale destination. */
export function takeDestination(): string {
  try {
    const stored = sessionStorage.getItem(DESTINATION_KEY);
    sessionStorage.removeItem(DESTINATION_KEY);
    // Re-checked on read: any script on the origin can write storage.
    if (stored && isSafePath(stored)) return stored;
  } catch {
    // Fall through to the default.
  }
  return DEFAULT_DESTINATION;
}

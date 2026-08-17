/**
 * Where to land after signing in. A *path*, never a credential.
 *
 * The session cookie is the only credential in this app and the server sets it;
 * nothing auth-related is ever written to browser storage. sessionStorage holds
 * the destination rather than router state because the magic link arrives as a
 * fresh page load — often from a mail client, in a new tab — and
 * `location.state` does not survive that.
 *
 * Deliberately free of React and of any API import, so it stays a pair of pure
 * functions over one storage key and can be tested as such.
 */

const DESTINATION_KEY = 'auth:destination';
// The leads table is the top of the funnel — signing in lands you on the book
// you are working, not on the queue of drafts a previous run produced.
const DEFAULT_DESTINATION = '/leads/';

/**
 * Same-origin absolute paths only. `startsWith('/')` on its own is not enough:
 * `//evil.example` is a protocol-relative URL and would be an open redirect.
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
    // Checked on the way out as well as in: storage is writable by any script
    // on the origin, so the value read back is not necessarily the one stored.
    if (stored && isSafePath(stored)) return stored;
  } catch {
    // Fall through to the default.
  }
  return DEFAULT_DESTINATION;
}

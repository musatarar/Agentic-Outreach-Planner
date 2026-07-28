import { useState } from 'react';
import { logout } from '../api/endpoints';
import { Button } from './ui';

/**
 * Ends the session and leaves.
 *
 * The redirect is a full page load rather than a router navigation on purpose:
 * `logout()` flushes the session cookie and rotates the CSRF token server-side,
 * and a hard load re-runs Django's `@ensure_csrf_cookie` sign-in shell so the
 * next request starts from a clean, matching pair. It also drops every piece of
 * in-memory state belonging to the account that just left, which is the one
 * thing a sign-out button must not get subtly wrong.
 *
 * A failed logout still leaves. The server is the authority on whether the
 * session died, and stranding someone on a page they asked to leave — because
 * the network blipped — is the worse outcome.
 */
export function SignOutButton() {
  const [pending, setPending] = useState(false);

  return (
    <Button
      variant="ghost"
      size="sm"
      loading={pending}
      onClick={() => {
        if (pending) return;
        setPending(true);
        void logout().finally(() => {
          window.location.assign('/signin');
        });
      }}
    >
      Sign out
    </Button>
  );
}

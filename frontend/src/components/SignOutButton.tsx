import { useState } from 'react';
import { logout } from '../api/endpoints';
import { Button } from './ui';

/**
 * Ends the session and leaves. The redirect is a full page load, not a router
 * navigation: logout rotates the CSRF token, and a hard load re-runs Django's
 * `@ensure_csrf_cookie` shell and drops all in-memory state. A failed logout
 * still leaves.
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

import { useEffect, useState } from 'react';
import { fetchAuthMe } from '../api/endpoints';

export type AuthStatus = 'checking' | 'authenticated' | 'anonymous';

/**
 * Asks the server whether this browser has a session.
 *
 * `GET /api/auth/me/` is `AllowAny` and returns 401 itself (CONTRACT §5.1), and
 * `client.ts` exempts the `/api/auth/` namespace from the global 401 handler —
 * so asking the question cannot trigger the redirect it is about to decide on.
 *
 * The session cookie is the credential and the browser sends it; there is
 * nothing for this hook to store, and nothing it could store safely.
 */
export function useAuth() {
  const [status, setStatus] = useState<AuthStatus>('checking');
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAuthMe()
      .then((me) => {
        if (cancelled) return;
        setEmail(me.email);
        setStatus(me.authenticated ? 'authenticated' : 'anonymous');
      })
      .catch(() => {
        // 401 is the expected answer for a signed-out visitor, and a network
        // failure is indistinguishable from it here. Treating both as
        // "anonymous" fails towards the sign-in screen rather than towards a
        // half-rendered app the user cannot act on.
        if (!cancelled) setStatus('anonymous');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { status, email };
}

import { useEffect, useState } from 'react';
import { fetchAuthMe } from '../api/endpoints';

export type AuthStatus = 'checking' | 'authenticated' | 'anonymous';

/**
 * Asks the server whether this browser has a session. `client.ts` exempts
 * `/api/auth/` from the global 401 handler, so asking cannot trigger the
 * redirect it is about to decide on.
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
        // 401 and a network failure are indistinguishable here; both fail
        // towards the sign-in screen.
        if (!cancelled) setStatus('anonymous');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { status, email };
}

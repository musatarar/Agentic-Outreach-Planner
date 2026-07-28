import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { setUnauthorizedHandler } from '../api/client';
import { rememberDestination } from '../hooks/authDestination';
import { useAuth } from '../hooks/useAuth';
// Shares the logged-out chrome's stylesheet for the session-check splash.
import '../pages/auth.css';

/**
 * Route guard for every authenticated surface.
 *
 * Two ways in to the same redirect:
 *
 *  1. The up-front session check. `useAuth` asks the server once on mount;
 *     `anonymous` sends you to /signin before any page code runs.
 *  2. A 401 mid-session — the cookie expired while the tab sat open. Any API
 *     call from anywhere raises it, so `client.ts` routes them all through one
 *     handler installed here rather than leaving each call site to render its
 *     own broken screen.
 *
 * Both remember where you were headed, so signing in resumes the journey
 * instead of dumping you on the default page.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const destination = `${location.pathname}${location.search}`;

  useEffect(() => {
    setUnauthorizedHandler(() => {
      // Read from `window.location` rather than the closed-over value: the 401
      // may fire long after this effect ran, from a page the user has since
      // navigated to within the guarded area.
      rememberDestination(`${window.location.pathname}${window.location.search}`);
      navigate('/signin', { replace: true });
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate]);

  // Redirect from an effect rather than by rendering <Navigate>, so remembering
  // the destination is guaranteed to happen before the navigation, not merely
  // to be scheduled alongside it.
  useEffect(() => {
    if (status !== 'anonymous') return;
    rememberDestination(destination);
    navigate('/signin', { replace: true, state: { from: destination } });
  }, [status, destination, navigate]);

  if (status === 'authenticated') return <>{children}</>;

  // Covers both the session check and the frame between deciding to redirect
  // and the router acting on it. Quiet on purpose: this is usually one frame,
  // and a card or spinner flashing on every page load is worse than nothing.
  return (
    <div className="auth-shell">
      <main className="auth-shell__main">
        <p className="auth-status" role="status">
          {status === 'checking' ? 'Checking your session…' : 'Taking you to sign in…'}
        </p>
      </main>
    </div>
  );
}

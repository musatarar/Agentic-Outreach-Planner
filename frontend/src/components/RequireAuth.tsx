import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { setUnauthorizedHandler } from '../api/client';
import { rememberDestination } from '../hooks/authDestination';
import { useAuth } from '../hooks/useAuth';
// Shares the logged-out chrome's stylesheet for the session-check splash.
import '../pages/auth.css';

/**
 * Route guard for every authenticated surface. Redirects to /signin on the
 * mount-time session check and on any mid-session 401 (via the one handler
 * installed into client.ts); both remember the destination so signing in
 * resumes the journey.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const destination = `${location.pathname}${location.search}`;

  useEffect(() => {
    setUnauthorizedHandler(() => {
      // Read `window.location`, not the closed-over value: the 401 may fire
      // long after this effect ran, from a page navigated to since.
      rememberDestination(`${window.location.pathname}${window.location.search}`);
      navigate('/signin', { replace: true });
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate]);

  // Redirect from an effect, not <Navigate>, so the destination is remembered
  // strictly before the navigation.
  useEffect(() => {
    if (status !== 'anonymous') return;
    rememberDestination(destination);
    navigate('/signin', { replace: true, state: { from: destination } });
  }, [status, destination, navigate]);

  if (status === 'authenticated') return <>{children}</>;

  // Covers the session check and the redirect frame; quiet on purpose — this
  // is usually one frame.
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

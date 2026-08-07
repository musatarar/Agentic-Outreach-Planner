import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ApiError } from '../api/client';
import { consumeLoginToken } from '../api/endpoints';
import { AuthShell } from '../components/AuthShell';
import { Button } from '../components/ui';
import { takeDestination } from '../hooks/authDestination';
import { SignInPage } from './SignInPage';
import './auth.css';

/** The two codes the backend uses to describe a dead link. */
const TOKEN_CODES = new Set(['expired_token', 'invalid_token']);

type Phase = 'working' | 'failed';

export function ConsumePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token') ?? '';

  const [phase, setPhase] = useState<Phase>('working');
  const [code, setCode] = useState('');
  const [detail, setDetail] = useState('');

  // A login token is single-use, so consuming it twice burns it. StrictMode
  // double-invokes effects in development and the ref survives that simulated
  // remount, which is exactly what this guard needs it to do.
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;

    if (!token) {
      setCode('invalid_token');
      setPhase('failed');
      return;
    }

    consumeLoginToken({ token })
      .then(() => {
        // Soft navigation is safe across the login boundary: client.ts reads
        // the csrftoken cookie per request, so it picks up the rotated one
        // Django set on this response.
        navigate(takeDestination(), { replace: true });
      })
      .catch((error: unknown) => {
        setCode(error instanceof ApiError ? error.code : '');
        setDetail(
          error instanceof Error
            ? error.message
            : 'Something went wrong signing you in. Try requesting a new link.',
        );
        setPhase('failed');
      });
  }, [token, navigate]);

  if (phase === 'failed' && TOKEN_CODES.has(code)) {
    // State 03. It is a state of /signin, not a page of its own, because the
    // only thing to do from a dead link is ask for another one.
    return <SignInPage initialState="expired" expiredCode={code} />;
  }

  if (phase === 'failed') {
    // Rate limiting, or the network. Showing "your link expired" here would be
    // a plain lie, so the server's own sentence is shown instead.
    return (
      <AuthShell title="Could not sign you in">
        <h1 className="auth-title">Could not sign you in</h1>
        <p className="auth-lede">{detail}</p>
        <div className="auth-actions">
          <Button variant="primary" onClick={() => navigate('/signin', { replace: true })}>
            Back to sign in
          </Button>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Signing you in">
      <h1 className="auth-title">Signing you in…</h1>
      <p className="auth-lede" role="status">
        One moment. This tab will move on by itself.
      </p>
    </AuthShell>
  );
}

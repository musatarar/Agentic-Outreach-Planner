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

  // A login token is single-use, and StrictMode double-invokes effects in
  // dev; the ref survives that simulated remount, so it guards the consume.
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
        // Safe across the login boundary: client.ts reads the csrftoken
        // cookie per request, so it picks up the rotated one.
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
    // A state of /signin rather than a page of its own.
    return <SignInPage initialState="expired" expiredCode={code} />;
  }

  if (phase === 'failed') {
    // Rate limiting or the network — show the server's own sentence, not
    // "your link expired".
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

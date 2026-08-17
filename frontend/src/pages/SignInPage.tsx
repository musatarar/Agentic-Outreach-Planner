import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import { requestLoginLink } from '../api/endpoints';
import type { AuthRequestLinkResult } from '../api/types';
import { AuthShell } from '../components/AuthShell';
import { Badge, Button, Input } from '../components/ui';
import './auth.css';

/** `expired` is entered only from ConsumePage after the backend rejects a token. */
type State = 'enter' | 'sent' | 'expired';

export interface SignInPageProps {
  initialState?: 'enter' | 'expired';
  /** `expired_token` or `invalid_token`. */
  expiredCode?: string;
}

function minutesFrom(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return `${minutes} minute${minutes === 1 ? '' : 's'}`;
}

/**
 * Deliberately permissive — the server is the authority (`invalid_email`);
 * this only catches empty and obviously malformed input.
 */
function looksLikeEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

/** 01 — Enter. One field, one button, no alternatives. */
function EnterState({
  email,
  onEmailChange,
  onSubmit,
  pending,
  fieldError,
  formError,
}: {
  email: string;
  onEmailChange: (value: string) => void;
  onSubmit: () => void;
  pending: boolean;
  fieldError: string;
  formError: string;
}) {
  const formRef = useRef<HTMLFormElement>(null);

  // The Input primitive's props are frozen and carry no `autoComplete`/`name`,
  // so set them on the DOM node — where the browser reads them anyway.
  useEffect(() => {
    const field = formRef.current?.querySelector('input');
    if (!field) return;
    field.setAttribute('autocomplete', 'email');
    field.setAttribute('name', 'email');
    field.setAttribute('inputmode', 'email');
  }, []);

  return (
    <>
      <h1 className="auth-title">Sign in</h1>
      <p className="auth-lede">We&rsquo;ll email you a link. No password to remember.</p>

      {/* noValidate: the browser's validation bubble cannot be themed; the
          same check renders through the Input's error slot instead. */}
      <form
        className="auth-form"
        noValidate
        ref={formRef}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <Input
          label="Email"
          id="signin-email"
          type="email"
          value={email}
          onChange={(event) => onEmailChange(event.target.value)}
          placeholder="you@agency.com"
          error={fieldError || undefined}
          autoFocus
        />

        {formError && (
          <p className="auth-alert" role="alert">
            {formError}
          </p>
        )}

        <div className="auth-actions">
          {/* loading implies disabled, so no double submit. */}
          <Button variant="primary" type="submit" loading={pending}>
            {pending ? 'Sending…' : 'Email me a link'}
          </Button>
        </div>
      </form>
    </>
  );
}

/** 02 — Sent. Where the link went, how long it lasts, and how to try again. */
function SentState({
  email,
  result,
  cooldown,
  resending,
  resendError,
  onResend,
  onUseAnother,
}: {
  email: string;
  result: AuthRequestLinkResult;
  cooldown: number;
  resending: boolean;
  resendError: string;
  onResend: () => void;
  onUseAnother: () => void;
}) {
  return (
    <>
      <h1 className="auth-title">Check your email</h1>
      <p className="auth-lede">
        A sign-in link is on its way to <span className="auth-address">{email}</span>. It expires in{' '}
        {minutesFrom(result.expires_in)} and works once.
      </p>

      {/* Rendered only when the API returns one; never constructed here. */}
      {result.dev_link && (
        <div className="auth-devlink">
          <div className="auth-devlink__head">
            <Badge tone="accent">dev mode</Badge>
            <span>No mail server needed — open the link directly.</span>
          </div>
          <a className="auth-devlink__url" href={result.dev_link}>
            {result.dev_link}
          </a>
          <p className="auth-note">
            Shown because the server is running with DEBUG and console link delivery. It is never
            returned in production.
          </p>
        </div>
      )}

      <hr className="auth-divider" />

      {resendError && (
        <p className="auth-alert" role="alert">
          {resendError}
        </p>
      )}

      <div className="auth-actions">
        <Button variant="secondary" onClick={onResend} disabled={cooldown > 0} loading={resending}>
          {cooldown > 0 ? (
            <>
              Resend in <span className="auth-countdown">{cooldown}s</span>
            </>
          ) : (
            'Send another link'
          )}
        </Button>
        <Button variant="ghost" onClick={onUseAnother}>
          Use a different address
        </Button>
      </div>
    </>
  );
}

/** 03 — Expired. Explains the two rules, then puts you back at state 01. */
function ExpiredState({ code, onRestart }: { code: string; onRestart: () => void }) {
  // The backend only distinguishes expired vs invalid; a used link is invalid.
  const lede =
    code === 'expired_token'
      ? 'Sign-in links last 15 minutes, and this one is past that.'
      : 'Sign-in links last 15 minutes and work once. This one has already been used, or it was never valid.';

  return (
    <>
      <h1 className="auth-title">That link won&rsquo;t work</h1>
      <p className="auth-lede">{lede}</p>
      <div className="auth-actions">
        <Button variant="primary" onClick={onRestart}>
          Request a new link
        </Button>
      </div>
    </>
  );
}

export function SignInPage({ initialState = 'enter', expiredCode = '' }: SignInPageProps) {
  const [state, setState] = useState<State>(initialState);
  const [email, setEmail] = useState('');
  const [result, setResult] = useState<AuthRequestLinkResult | null>(null);
  const [pending, setPending] = useState(false);
  const [fieldError, setFieldError] = useState('');
  const [formError, setFormError] = useState('');
  const [cooldown, setCooldown] = useState(0);

  // Cleared on unmount and restart, so a resend cannot leave two intervals racing.
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setInterval(() => {
      setCooldown((remaining) => (remaining <= 1 ? 0 : remaining - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldown > 0]);

  const send = useCallback(
    async (address: string) => {
      if (!looksLikeEmail(address)) {
        setFieldError('Enter a valid email address.');
        setFormError('');
        return;
      }
      setFieldError('');
      setFormError('');
      setPending(true);
      try {
        const response = await requestLoginLink({ email: address.trim() });
        setResult(response);
        setCooldown(response.resend_after);
        setState('sent');
      } catch (error) {
        // The server's own sentence is shown; the code only decides where it lands.
        const code = error instanceof ApiError ? error.code : '';
        const detail =
          error instanceof Error
            ? error.message
            : 'Something went wrong. Check your connection and try again.';
        if (code === 'invalid_email') {
          setFieldError(detail);
        } else {
          setFormError(detail);
        }
      } finally {
        setPending(false);
      }
    },
    [],
  );

  const restart = useCallback(() => {
    setState('enter');
    setResult(null);
    setCooldown(0);
    setFieldError('');
    setFormError('');
  }, []);

  if (state === 'expired') {
    return (
      <AuthShell title="Link expired">
        <ExpiredState code={expiredCode} onRestart={restart} />
      </AuthShell>
    );
  }

  if (state === 'sent' && result) {
    return (
      <AuthShell title="Check your email">
        <SentState
          email={email.trim()}
          result={result}
          cooldown={cooldown}
          resending={pending}
          // No field in this state, so both error buckets share the banner.
          resendError={formError || fieldError}
          onResend={() => {
            if (pending || cooldown > 0) return;
            void send(email);
          }}
          onUseAnother={restart}
        />
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Sign in">
      <EnterState
        email={email}
        onEmailChange={(value) => {
          setEmail(value);
          if (fieldError) setFieldError('');
        }}
        onSubmit={() => {
          if (pending) return;
          void send(email);
        }}
        pending={pending}
        fieldError={fieldError}
        formError={formError}
      />
    </AuthShell>
  );
}

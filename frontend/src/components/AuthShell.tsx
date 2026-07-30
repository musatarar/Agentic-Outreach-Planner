import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { BrandMark } from './BrandMark';
import { Card } from './ui';
import { ThemeToggle } from './ui';
import { BRAND_NAME, documentTitle } from '../util/brand';

interface Props {
  /** Drives document.title, which Django's shell template set on first paint. */
  title: string;
  children: ReactNode;
}

/**
 * The logged-out chrome: a wordmark, the theme control, and one centred card.
 *
 * Deliberately not `PageHeader`. That header renders `Nav`, and every link in
 * it goes somewhere a signed-out visitor cannot reach — offering six dead ends
 * on the screen whose entire job is one email field is worse than offering
 * none. The theme toggle stays, because the choice should survive being
 * signed out.
 */
export function AuthShell({ title, children }: Props) {
  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);

  return (
    <div className="auth-shell">
      <div className="auth-shell__bar">
        <span className="auth-wordmark">
          <BrandMark size={16} />
          {BRAND_NAME}
        </span>
        <ThemeToggle />
      </div>
      <main className="auth-shell__main">
        <div className="auth-panel">
          <Card elevation="raised" padding="lg">
            {children}
          </Card>
        </div>
      </main>
    </div>
  );
}

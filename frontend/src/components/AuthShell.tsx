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
 * Not `PageHeader` — that renders `Nav`, whose links all go somewhere a
 * signed-out visitor cannot reach.
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

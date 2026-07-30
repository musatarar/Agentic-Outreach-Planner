import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { Nav } from './Nav';
import { BrandMark } from './BrandMark';
import { ThemeToggle } from './ui';
import { BRAND_NAME, documentTitle } from '../util/brand';

interface Props {
  current: string;
  title: string;
  subtitle: string;
  children?: ReactNode;
}

export function PageHeader({ current, title, subtitle, children }: Props) {
  // Django's shell templates set the right <title> on first paint; keep it in
  // sync when React handles navigation client-side.
  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);

  return (
    <header>
      {/* The theme control lives in the shell, so it is reachable from every
          page rather than buried in settings. The lock-up sits on its own row
          above the nav: dropping it inline would have made the six-link row
          wrap on a laptop width, and the mark is chrome, not navigation. */}
      <div className="brand-bar">
        <span className="brand-lockup">
          <BrandMark />
          <span className="brand-name">{BRAND_NAME}</span>
        </span>
        <ThemeToggle />
      </div>
      <Nav current={current} />
      <h1>{title}</h1>
      <p className="subtitle">{subtitle}</p>
      {children}
    </header>
  );
}

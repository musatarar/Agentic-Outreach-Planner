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
  // Django sets <title> on first paint; keep it in sync on client-side nav.
  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);

  return (
    <header>
      {/* The lock-up gets its own row: inline, it wrapped the nav at laptop
          widths. */}
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

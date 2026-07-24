import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { Nav } from './Nav';

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
    document.title = title;
  }, [title]);

  return (
    <header>
      <Nav current={current} />
      <h1>{title}</h1>
      <p className="subtitle">{subtitle}</p>
      {children}
    </header>
  );
}

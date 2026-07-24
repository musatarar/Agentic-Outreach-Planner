import type { ReactNode } from 'react';

export function ErrorMessage({ children }: { children: ReactNode }) {
  return <div className="error-message">{children}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
